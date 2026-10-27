"""Decide and apply what a source edit changes in an existing wiki run.

``decide_update`` is read-only: it diffs the old and new parsed Markdown,
maps every changed block to the pages that own it, and picks one tier.
``apply_update`` writes that decision into the run state so the existing
resumable pipeline regenerates only the pages it dropped.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from .storage import (
    normalize_source,
    read_json,
    sha256_text,
    split_source_lines,
    write_json_atomic,
    write_text_atomic,
)

Hunk = tuple[int, int, int, int]  # old_start (1-based), old_len, new_start (1-based), new_len

# The only numeric policy. Change these only with evidence from real runs.
PATCH_MAX_CHURN = 0.20  # page churn at or below this is patched by the model
FULL_MIN_REGEN_SHARE = 0.50  # more regenerated pages than this share -> full rebuild
MAX_PAGE_GROWTH = 2.0  # a page larger than this x max(target, old size) -> full rebuild

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_LONG_LINE = 4096


def source_lines(text: str) -> list[str]:
    """The numbered source lines every plan range refers to."""

    return split_source_lines(normalize_source(text))


def diff_lines(text: str) -> list[str]:
    """Source lines with every image spelling reduced to its content identity."""

    from graph.common.images import canonicalize_images

    return split_source_lines(canonicalize_images(normalize_source(text)))


def _diff_input(lines: Sequence[str]) -> str:
    # Long lines are almost always base64 image payloads. A digest keeps line
    # equality exact while keeping git's output small.
    rows = []
    for line in lines:
        line = line.replace("\x00", "�")
        if len(line) > _LONG_LINE:
            line = "�sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
        rows.append(line + "\n")
    return "".join(rows)


def line_hunks(old: Sequence[str], new: Sequence[str]) -> list[Hunk]:
    """Minimal line diff (Myers, via git) as 1-based hunks.

    Fuzzy matching and git's histogram diff both align repeated regions
    badly: a few one-line edits can come back as "delete 2,000 lines
    here, insert them later". ``git diff --minimal`` returns the smallest edit
    script instead and is fast. git is already required by publisher history.
    """

    if list(old) == list(new):
        return []
    with tempfile.TemporaryDirectory(prefix="wiki-diff-") as tmp:
        old_path, new_path = Path(tmp) / "old.md", Path(tmp) / "new.md"
        old_path.write_text(_diff_input(old), encoding="utf-8")
        new_path.write_text(_diff_input(new), encoding="utf-8")
        result = subprocess.run(
            [
                "git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--text",
                "--minimal", "-U0", "--", str(old_path), str(new_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed: {result.stderr.strip()[:500]}")
    hunks: list[Hunk] = []
    for match in _HUNK_RE.finditer(result.stdout):
        old_start, old_len = int(match.group(1)), int(match.group(2) or 1)
        new_start, new_len = int(match.group(3)), int(match.group(4) or 1)
        # With a zero count git names the line *before* the change.
        hunks.append((old_start + (old_len == 0), old_len, new_start + (new_len == 0), new_len))
    if not hunks:
        raise RuntimeError("git diff reported differences but no hunks")
    return hunks


def split_at_cuts(hunks: Sequence[Hunk], cuts: Sequence[int]) -> list[Hunk]:
    """Split every changed block that crosses a page cut.

    ``cuts`` are old line numbers that end a page (every page except the
    last). The old lines before a cut become a pure deletion in the earlier
    page; all new lines stay with the last piece, i.e. the later page.
    """

    result: list[Hunk] = []
    for old_start, old_len, new_start, new_len in hunks:
        first, end = old_start - 1, old_start - 1 + old_len  # 0-based [first, end)
        cursor = first
        for cut in cuts:
            if first < cut < end:
                result.append((cursor + 1, cut - cursor, new_start, 0))
                cursor = cut
        result.append((cursor + 1, end - cursor, new_start, new_len))
    return result


class BoundaryMap:
    """Map a cut between old lines to the matching cut between new lines.

    A cut is the number of lines before it (0 = document start). Pure
    insertions at a cut join the page before the cut, so text appended to the
    end of a section stays in that section; insertions at the document start
    join the first page.
    """

    def __init__(self, hunks: Sequence[Hunk], old_count: int, new_count: int) -> None:
        self.changes = [
            (old_start - 1, old_start - 1 + old_len, new_start - 1, new_start - 1 + new_len)
            for old_start, old_len, new_start, new_len in hunks
        ]
        self.old_count = old_count
        self.new_count = new_count

    def map(self, cut: int) -> int | None:
        if cut == 0:
            return 0
        if cut == self.old_count:
            return self.new_count
        old_cursor = new_cursor = 0
        for old_start, old_end, new_start, new_end in self.changes:
            if cut < old_start:
                break
            if old_start == old_end == cut:
                return new_end
            if cut == old_start:
                return new_start
            if cut == old_end:
                return new_end
            if old_start < cut < old_end:
                return None
            old_cursor, new_cursor = old_end, new_end
        return new_cursor + cut - old_cursor

    def map_range(self, first: int, last: int) -> list[int] | None:
        start, end = self.map(first - 1), self.map(last)
        if start is None or end is None or start >= end:
            return None
        return [start + 1, end]


@dataclass
class UpdateDecision:
    tier: int  # 0 unchanged, 1 patch only, 2 some pages regenerated, 3 full rebuild
    reason: str
    hunks: list[Hunk] = field(default_factory=list)
    old_count: int = 0
    new_count: int = 0
    patch: dict[str, set[int]] = field(default_factory=dict)  # filename -> hunk indexes
    owned: dict[str, set[int]] = field(default_factory=dict)  # filename -> hunks it owns
    regenerate: set[str] = field(default_factory=set)
    retitle: dict[str, dict[str, Any]] = field(default_factory=dict)  # filename -> title/chapter/path
    ranges: dict[str, list[list[int]]] = field(default_factory=dict)  # filename -> new owner ranges
    research_stale: set[str] = field(default_factory=set)
    churn: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "hunks": len(self.hunks),
            "patch_pages": len(self.patch),
            "regenerate_pages": len(self.regenerate),
            "retitle_pages": len(self.retitle),
        }


def _full(reason: str) -> UpdateDecision:
    return UpdateDecision(tier=3, reason=reason)


def _overlaps(ranges: Sequence[Sequence[int]], start: int, end: int) -> bool:
    return any(int(first) <= end and start <= int(last) for first, last in ranges)


def _nonblank(lines: Sequence[str], start: int, count: int) -> int:
    return sum(1 for line in lines[start - 1 : start - 1 + count] if line.strip())


def _owner_pages(hunk: Hunk, pages: Sequence[tuple[int, int]]) -> list[int]:
    old_start, old_len, _new_start, _new_len = hunk
    if old_len == 0:
        cut = old_start - 1
        if cut == 0:
            return [0]
        return [index for index, (first, last) in enumerate(pages) if first <= cut <= last][:1]
    last_line = old_start + old_len - 1
    return [index for index, (first, last) in enumerate(pages) if first <= last_line and old_start <= last]


def _structural_shape(lines: Sequence[str], *, kind: str, planner: SimpleNamespace) -> list[dict[str, Any]] | None:
    """Pages the deterministic heading planner would build, or None."""

    if kind not in {"docx", "pdf", "md"}:
        return None
    from graph.formats import docx, md, pdf

    from .document_map import validate_seed_plan
    from .markdown_blocks import build_block_index
    from .pipeline import _plan_pages

    planners = {"docx": docx.plan, "pdf": pdf.plan, "md": md.plan}
    plan = planners[kind](lines, config=planner)
    if plan is None:
        return None
    seed, _error = validate_seed_plan(
        plan,
        source_line_count=len(lines),
        block_index=build_block_index(list(lines)),
        lines=lines,
        page_target_lines=int(planner.page_target_lines),
    )
    if seed is None:
        return None
    return [
        {"title": page.title, "chapter": page.chapter, "path": list(page.path), "range": list(page.owner_ranges[0])}
        for page in _plan_pages(seed)
    ]


def decide_update(
    run_root: Path,
    old_text: str,
    new_text: str,
    *,
    kind: str,
    structure_target_lines: int = 250,
    structure_min_lines: int = 40,
    page_target_lines: int = 100,
    pdf_use_headings: bool = False,
) -> UpdateDecision:
    """Pick one tier for this edit without writing anything."""

    plan = read_json(Path(run_root) / "state" / "plan.json", default={})
    old, new = diff_lines(old_text), diff_lines(new_text)
    if old == new:
        return UpdateDecision(tier=0, reason="unchanged", old_count=len(old), new_count=len(new))
    pages_json = list(plan.get("pages", []))
    if not pages_json or int(plan.get("source_line_count", -1)) != len(old):
        return _full("plan-mismatch")
    pages = [(int(page["owner_ranges"][0][0]), int(page["owner_ranges"][-1][1])) for page in pages_json]
    names = [str(page["filename"]) for page in pages_json]

    hunks = split_at_cuts(line_hunks(old, new), [last for _first, last in pages[:-1]])
    boundaries = BoundaryMap(hunks, len(old), len(new))
    new_ranges: list[list[int]] = []
    for first, last in pages:
        mapped = boundaries.map_range(first, last)
        if mapped is None:
            return _full("page-emptied" if boundaries.map(first - 1) is not None and boundaries.map(last) is not None else "unmappable")
        new_ranges.append(mapped)

    from .markdown_blocks import build_block_index

    new_source = source_lines(new_text)
    blocks = build_block_index(new_source)
    if any(not blocks.cut_is_safe(start) for start, _end in new_ranges[1:]):
        return _full("unsafe-boundary")

    decision = UpdateDecision(
        tier=1, reason="patch", hunks=hunks, old_count=len(old), new_count=len(new),
        ranges={name: [mapped] for name, mapped in zip(names, new_ranges)},
    )
    owned: dict[int, set[int]] = {}
    referenced: dict[int, set[int]] = {}
    for index, hunk in enumerate(hunks):
        # Canonical image wrappers can add/remove blank physical lines.  Keep
        # those hunks in BoundaryMap so ranges move correctly, but never ask an
        # LLM to edit a page when no meaningful source content changed.
        if not (
            _nonblank(old, hunk[0], hunk[1])
            or _nonblank(new, hunk[2], hunk[3])
        ):
            continue
        owners = _owner_pages(hunk, pages)
        for page in owners:
            owned.setdefault(page, set()).add(index)
        span_end = hunk[0] + max(hunk[1], 1) - 1
        for page, item in enumerate(pages_json):
            if page not in owners and _overlaps(item.get("reference_ranges", []), hunk[0], span_end):
                referenced.setdefault(page, set()).add(index)
                decision.research_stale.add(names[page])

    growth_target = max(structure_target_lines, page_target_lines)
    for page, indexes in owned.items():
        (first, last), (new_first, new_last) = pages[page], new_ranges[page]
        if new_last - new_first + 1 > MAX_PAGE_GROWTH * max(growth_target, last - first + 1):
            return _full("page-too-large")
        changed = sum(
            max(_nonblank(old, hunks[i][0], hunks[i][1]), _nonblank(new, hunks[i][2], hunks[i][3]))
            for i in indexes
        )
        size = max(_nonblank(old, first, last - first + 1), _nonblank(new, new_first, new_last - new_first + 1), 1)
        churn = changed / size
        decision.churn[names[page]] = round(churn, 4)
        decision.owned[names[page]] = set(indexes)
        if churn <= PATCH_MAX_CHURN:
            decision.patch[names[page]] = set(indexes) | referenced.get(page, set())
        else:
            decision.regenerate.add(names[page])
    for page, indexes in referenced.items():
        if page not in owned:
            decision.patch[names[page]] = set(indexes)
    if len(decision.regenerate) > FULL_MIN_REGEN_SHARE * len(pages):
        return _full("most-pages-changed")

    planner = SimpleNamespace(
        structure_target_lines=structure_target_lines,
        structure_min_lines=structure_min_lines,
        page_target_lines=page_target_lines,
        pdf_use_headings=pdf_use_headings,
    )
    old_shape = _structural_shape(source_lines(old_text), kind=kind, planner=planner)
    stored = [(str(item.get("title", "")), list(item.get("path", []))) for item in pages_json]
    if old_shape is not None and [(item["title"], item["path"]) for item in old_shape] == stored:
        # The stored plan came from the heading planner, so the new headings must agree.
        new_shape = _structural_shape(new_source, kind=kind, planner=planner)
        if new_shape is None:
            return _full("structure-changed")
        if [(item["title"], item["path"]) for item in new_shape] != stored:
            if [item["range"] for item in new_shape] != new_ranges:
                return _full("structure-changed")
            for name, item, (title, path) in zip(names, new_shape, stored):
                if (item["title"], item["path"]) != (title, path):
                    decision.retitle[name] = {"title": item["title"], "chapter": item["chapter"], "path": item["path"]}

    if decision.regenerate:
        decision.tier, decision.reason = 2, "regenerate"
    elif not decision.patch and decision.retitle:
        decision.reason = "retitle"
    elif not decision.patch:
        decision.tier, decision.reason = 0, "image-representation"
    return decision


def drop_pages(run_root: Path, plan_pages: Sequence[dict[str, Any]], filenames: set[str], *, research: set[str]) -> list[str]:
    """Delete generated output so the resumable pipeline rewrites these pages.

    Returns the dropped pages that carried pulled GROWI edits.
    """

    run_root = Path(run_root)
    human: list[str] = []
    for page in plan_pages:
        name = str(page["filename"])
        if name not in filenames:
            continue
        number = int(page["number"])
        state_path = run_root / "state" / "pages" / f"{number:03d}.json"
        if read_json(state_path, default={}).get("human_edited"):
            human.append(name)
        (run_root / "wiki" / name).unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        if name in research:
            shutil.rmtree(run_root / "work" / f"research-{number:03d}", ignore_errors=True)
    return sorted(human)


def _retitle(run_root: Path, plan_pages: list[dict[str, Any]], retitle: dict[str, dict[str, Any]]) -> None:
    wiki = Path(run_root) / "wiki"
    renames = {
        str(page["filename"]): (str(page["title"]), str(retitle[str(page["filename"])]["title"]))
        for page in plan_pages
        if str(page["filename"]) in retitle
    }
    for page in plan_pages:
        name = str(page["filename"])
        if name in retitle:
            page.update(retitle[name])
    by_name = {str(page["filename"]): page for page in plan_pages}
    for path in sorted(wiki.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        updated = text
        for name, (old_title, new_title) in renames.items():
            updated = updated.replace(f"[{old_title}]({name})", f"[{new_title}]({name})")
            if path.name == name and updated.startswith(f"# {old_title}\n"):
                updated = f"# {new_title}\n" + updated[len(f"# {old_title}\n"):]
        if updated == text:
            continue
        write_text_atomic(path, updated)
        page = by_name.get(path.name)
        if page is None:
            continue
        state_path = Path(run_root) / "state" / "pages" / f"{int(page['number']):03d}.json"
        state = read_json(state_path, default={})
        if state:
            state["content_sha256"] = sha256_text(updated)
            state["title"] = page["title"]
            write_json_atomic(state_path, state)


def apply_update(run_root: Path, decision: UpdateDecision, new_source_text: str) -> list[str]:
    """Write a tier 0-2 decision into the run state.

    Remaps every page range, reference range and cached research fact, renames
    retitled pages, drops the pages to regenerate, and stamps the new source
    hash so ``run_pipeline`` resumes this plan. Returns dropped pages that had
    pulled GROWI edits.
    """

    if decision.tier not in (0, 1, 2):
        raise ValueError("apply_update only handles tiers 0-2")
    run_root = Path(run_root)
    plan_path = run_root / "state" / "plan.json"
    plan = read_json(plan_path)
    boundaries = BoundaryMap(decision.hunks, decision.old_count, decision.new_count)

    def remap(ranges: Sequence[Sequence[int]]) -> list[list[int]] | None:
        mapped = [boundaries.map_range(int(first), int(last)) for first, last in ranges]
        return None if any(item is None for item in mapped) else [item for item in mapped if item]

    for page in plan["pages"]:
        name = str(page["filename"])
        page["owner_ranges"] = decision.ranges.get(name, page["owner_ranges"])
        references = []
        for first, last in page.get("reference_ranges", []):
            mapped = boundaries.map_range(int(first), int(last))
            if mapped is None:
                decision.research_stale.add(name)
            else:
                references.append(mapped)
        page["reference_ranges"] = references

    for page in plan["pages"]:
        number = int(page["number"])
        if str(page["filename"]) in decision.research_stale:
            # Quoted text changed: this cache is stale even if the page is only patched.
            shutil.rmtree(run_root / "work" / f"research-{number:03d}", ignore_errors=True)
            continue
        cache_path = run_root / "work" / f"research-{number:03d}" / "references.json"
        cached = read_json(cache_path, default={})
        valid = isinstance(cached.get("useful_facts"), list)
        for fact in cached.get("useful_facts", []) if valid else []:
            mapped = boundaries.map_range(int(fact.get("source_start", 0)), int(fact.get("source_end", 0)))
            target = boundaries.map(int(fact.get("target_line", 0)) - 1)
            if mapped is None or target is None:
                valid = False
                break
            fact["source_start"], fact["source_end"] = mapped
            fact["target_line"] = target + 1
        if valid:
            write_json_atomic(cache_path, cached)
        elif cache_path.exists():
            shutil.rmtree(cache_path.parent, ignore_errors=True)

    if decision.retitle:
        _retitle(run_root, plan["pages"], decision.retitle)
    human = drop_pages(run_root, plan["pages"], decision.regenerate, research=decision.research_stale)

    new_hash = sha256_text(new_source_text)
    for page in plan["pages"]:
        state_path = run_root / "state" / "pages" / f"{int(page['number']):03d}.json"
        state = read_json(state_path, default={})
        if not state:
            continue
        state["source_ranges"] = page["owner_ranges"]
        state["reference_ranges"] = page["reference_ranges"]
        provenance = state.get("provenance")
        if isinstance(provenance, dict):
            provenance["owned_line_ranges"] = page["owner_ranges"]
            provenance["imported_line_ranges"] = page["reference_ranges"]
            records = []
            for record in provenance.get("imported_from_pages", []):
                mapped = remap(record.get("source_ranges", []))
                if mapped:
                    record["source_ranges"] = mapped
                    records.append(record)
            provenance["imported_from_pages"] = records
            source = provenance.get("source_document")
            if isinstance(source, dict):
                source["sha256"] = new_hash
        write_json_atomic(state_path, state)
    plan["source_sha256"] = new_hash
    plan["source_line_count"] = decision.new_count
    write_json_atomic(plan_path, plan)
    return human


__all__ = [
    "FULL_MIN_REGEN_SHARE", "MAX_PAGE_GROWTH", "PATCH_MAX_CHURN", "BoundaryMap", "Hunk",
    "UpdateDecision", "apply_update", "decide_update", "diff_lines", "drop_pages",
    "line_hunks", "source_lines", "split_at_cuts",
]
