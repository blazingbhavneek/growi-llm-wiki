"""The wiki pipeline: deterministic partition, section-wise lossless rewriting.

1. Overlapping 250-line windows are described without assigning ownership.
2. Regional and document planners compile one exact sequential seed partition.
3. Python picks references (adjacent + shared vocabulary); one structured
   compare call per page collects facts to import from all of them.
4. Python cuts each page into sections; the active policy chooses which source
   units remain opaque (standard keeps fences/tables/images, fast keeps only
   images); the model rewrites one section at a time before semantic review.
5. Python writes the title, one model-written intro, links and navigation.

There is no CLI agent and no tool-calling agent. Everything the model needs
is inside the prompt; every decision about what survives is made in Python.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from common.policy import policy_of
from .config import REWRITE_PROMPT_VERSION, SEED_PLAN_COMPILE_VERSION, SEED_PLAN_VERSION, WikiConfig
from .document_map import build_seed_plan
from .markdown_blocks import build_block_index
from .ids import document_id, slugify
from .images import ImageUnit, extract_image_units, restore_images
from .model import ModelPort, judge_model
from .page import (
    PLACEHOLDER_RE,
    REFERENCE_MARKER_RE,
    assign_facts,
    check_section,
    code_tokens,
    demote_h1,
    is_small_document,
    link_titles,
    normalize_draft,
    split_sections,
    strip_reader_references,
    word_tokens,
)
from .prompts import (
    intro_prompt,
    page_judge_prompt,
    reference_research_prompt,
    section_write_prompt,
)
from .schemas import CompiledSeedPlan
from .storage import (
    clean_workdir,
    hash_of,
    normalize_source,
    read_json,
    sha256_text,
    slice_text,
    split_source_lines,
    write_json_atomic,
    write_text_atomic,
)
from .wire import (
    PageJudgeResult,
    ReferenceFact,
    ReferenceResearchResult,
)
from .windows import observe_document


Progress = Callable[[dict[str, Any]], None] | None
StopCheck = Callable[[], bool] | None


PLAN_CACHE_VERSION = "wiki-planner-cache-1"
_PLANNING: dict[str, threading.Event] = {}
_PLANNING_LOCK = threading.Lock()


class PipelineError(RuntimeError):
    """A deterministic seed or publication invariant failed."""


class ResumeUnavailable(PipelineError):
    """The caller required the stored seed plan, but it no longer matches."""


# Keep room below NAME_MAX for numbered page names and filesystem details. The
# limit is measured in UTF-8 bytes, so a character-count limit is insufficient
# for Japanese titles.
_PAGE_FILENAME_MAX_BYTES = 200
_SHORT_PAGE_NAME_MAX_CHARS = 24


@dataclass
class SeedPage:
    number: int
    title: str
    chapter: str
    summary: str
    owner_ranges: list[tuple[int, int]]
    filename: str
    page_id: str
    reference_ranges: list[tuple[int, int]] = field(default_factory=list)
    path: list[str] = field(default_factory=list)


@dataclass
class RewriteResult:
    page: SeedPage
    markdown: str
    attempts: int
    judge_score: int | None = None
    missing_important_information: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    verbatim_sections: list[str] = field(default_factory=list)


@dataclass
class _SectionCandidate:
    markdown: str
    attempt: int
    score: int | None = None
    missing: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _SectionResult:
    markdown: str
    attempts: int
    score: int | None = None
    missing: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verbatim: bool = False


@dataclass
class _ReferenceEvidence:
    page: SeedPage
    facts: list[ReferenceFact] = field(default_factory=list)
    no_useful_information_reason: str = ""


def _emit(callback: Progress, stage: str, step: str, **details: Any) -> None:
    if callback:
        callback({"stage": stage, "step": step, **details})


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse adjacent or overlapping source spans into simple ranges."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(start), int(end)) for start, end in ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _ranges_text(ranges: Sequence[tuple[int, int]]) -> str:
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in _merge_ranges(ranges)
    ) or "none"


def _ranges_json(ranges: Sequence[tuple[int, int]]) -> list[list[int]]:
    return [[start, end] for start, end in _merge_ranges(ranges)]


def _range_intersections(
    left: Sequence[tuple[int, int]], right: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    intersections: list[tuple[int, int]] = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if start <= end:
                intersections.append((start, end))
    return _merge_ranges(intersections)


def _referenced_page_records(
    page: SeedPage, pages: Sequence[SeedPage]
) -> list[dict[str, Any]]:
    """Map imported source ranges back to the seed pages that own them."""

    records: list[dict[str, Any]] = []
    for candidate in pages:
        if candidate.number == page.number:
            continue
        ranges = _range_intersections(page.reference_ranges, candidate.owner_ranges)
        if not ranges:
            continue
        records.append(
            {
                "number": candidate.number,
                "title": candidate.title,
                "filename": candidate.filename,
                "source_ranges": _ranges_json(ranges),
            }
        )
    return records


def _page_provenance(
    page: SeedPage,
    pages: Sequence[SeedPage],
    *,
    source_path: Path | None,
    source_snapshot_path: Path | None,
    source_sha256: str,
) -> dict[str, Any]:
    """Build durable dependencies for linking and later source-diff updates."""

    return {
        "source_document": {
            "path": str(source_path.resolve()) if source_path else "",
            "snapshot_path": (
                str(source_snapshot_path.resolve()) if source_snapshot_path else ""
            ),
            "sha256": source_sha256,
        },
        "owned_line_ranges": _ranges_json(page.owner_ranges),
        "imported_line_ranges": _ranges_json(page.reference_ranges),
        "imported_from_pages": _referenced_page_records(page, pages),
    }


def _plan_pages(plan: CompiledSeedPlan, *, keep: Sequence[str] = ()) -> list[SeedPage]:
    """Give the already validated sequential ranges stable numbered filenames."""

    pages: list[SeedPage] = []
    used_names: set[str] = set()
    previous: dict[str, list[str]] = {}
    for name in keep:
        previous.setdefault(re.sub(r"^\d+-", "", Path(name).stem), []).append(name)
    for number, entry in enumerate(plan.pages, start=1):
        stem = slugify(entry.title or f"document-section-{number}", fallback=f"page-{number}")
        candidates = previous.get(stem, [])
        filename = candidates.pop(0) if candidates else f"{number:03d}-{stem}.md"
        if filename.casefold() in used_names:
            suffix = 2
            while f"{number:03d}-{stem}-{suffix}.md".casefold() in used_names:
                suffix += 1
            filename = f"{number:03d}-{stem}-{suffix}.md"
        used_names.add(filename.casefold())
        pages.append(
            SeedPage(
                number=number,
                title=(entry.title or f"Document section {number}").strip(),
                chapter=(entry.chapter or " › ".join(entry.path)).strip(),
                summary=entry.summary.strip(),
                owner_ranges=[(entry.source_start, entry.source_end)],
                filename=filename,
                page_id=f"page-{number:03d}",
                path=list(entry.path),
            )
        )
    return pages


def _filename_bytes(filename: str) -> int:
    return len(filename.encode("utf-8"))


def _truncate_filename(filename: str, *, max_bytes: int = _PAGE_FILENAME_MAX_BYTES) -> str:
    """Trim a filename by UTF-8 bytes while preserving its extension."""

    suffix = Path(filename).suffix or ".md"
    stem = filename[: -len(suffix)] if filename.endswith(suffix) else filename
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    while stem and len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return stem.rstrip("._- ") + suffix


def _short_name_text(value: str) -> str:
    """Extract a single model-provided name before slugifying it."""

    text = str(value or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text).strip()
    text = re.sub(r"^(?:ファイル名|ページ名|名前)\s*[:：]\s*", "", text)
    text = text.splitlines()[0].strip(" `\"'") if text else ""
    if text.casefold().endswith(".md"):
        text = text[:-3]
    return text.strip()


async def _repair_long_page_filenames(
    pages: Sequence[SeedPage],
    *,
    model: ModelPort | None,
    on_progress: Progress,
    stop_check: StopCheck,
) -> None:
    """Give overlong generated names one concise naming call before writing."""

    long_pages = [page for page in pages if _filename_bytes(page.filename) > _PAGE_FILENAME_MAX_BYTES]
    if not long_pages:
        return

    from langchain_core.messages import HumanMessage, SystemMessage

    for page in long_pages:
        if stop_check and stop_check():
            raise asyncio.CancelledError("page filename repair cancelled")
        original = page.filename
        candidate = ""
        if model is not None:
            try:
                response = await model.text(
                    [
                        SystemMessage(
                            content=(
                                "技術Wikiのページ名を短く整える作業者です。"
                                "説明や前置きは返さず、短い日本語名を1行だけ返してください。"
                            )
                        ),
                        HumanMessage(
                            content=(
                                f"ページ題名: {page.title}\n"
                                f"章: {page.chapter or 'なし'}\n"
                                f"内容要約: {page.summary}\n\n"
                                f"意味を保ったまま、ファイル名に使える簡潔な名前を"
                                f"{_SHORT_PAGE_NAME_MAX_CHARS}文字以内で返してください。"
                                "拡張子、番号、パス、記号、説明文は不要です。"
                            )
                        ),
                    ],
                    max_output_tokens=64,
                )
                candidate = _short_name_text(response)
            except Exception as exc:  # noqa: BLE001 - deterministic fallback follows
                _emit(on_progress, "seed", "filename_repair_model_error", page=page.title, error=str(exc)[:300])

        if candidate:
            stem = slugify(candidate, fallback=f"page-{page.number}")
            repaired = f"{page.number:03d}-{stem}.md"
        else:
            repaired = ""
        if not repaired or _filename_bytes(repaired) > _PAGE_FILENAME_MAX_BYTES:
            repaired = _truncate_filename(original)
        page.filename = repaired
        _emit(
            on_progress,
            "seed",
            "filename_repaired",
            page=page.title,
            original=original,
            filename=page.filename,
            model=bool(candidate),
        )


def _slice_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    return "\n\n".join(slice_text(list(lines), start, end) for start, end in ranges)


def _page_units(page: SeedPage, units: Sequence[ImageUnit]) -> list[ImageUnit]:
    return [
        unit
        for unit in units
        if any(start <= unit.source_start and unit.source_end <= end for start, end in page.owner_ranges)
    ]


def _prompt_safe(text: str, units: Sequence[ImageUnit]) -> str:
    result = text
    for unit in units:
        result = result.replace(unit.raw, unit.placeholder)
    return result


def _image_neighbors(lines: Sequence[str], unit: ImageUnit) -> tuple[str, str]:
    from graph.common.images import find_images

    def nearest(numbers) -> str:
        for number in numbers:
            text = lines[number - 1].strip()
            if text and not find_images(text) and not text.startswith(("<image-", "</image-")):
                return text
        return ""

    before = nearest(range(unit.source_start - 1, 0, -1))
    after = nearest(range(unit.source_end + 1, len(lines) + 1))
    return before, after


def _insert_image_near_context(
    markdown: str,
    placeholder: str,
    before: str,
    after: str,
) -> str:
    """Best-effort placement next to surviving original context."""

    # Insert on its own line: the neighbour may be mid-sentence in the draft.
    if before and before in markdown:
        position = markdown.find("\n", markdown.find(before) + len(before))
        position = len(markdown) if position < 0 else position
        return markdown[:position] + f"\n\n{placeholder}" + markdown[position:]
    if after and after in markdown:
        position = markdown.rfind("\n", 0, markdown.find(after)) + 1
        return markdown[:position] + f"{placeholder}\n\n" + markdown[position:]
    return markdown.rstrip() + f"\n\n## 原文の図・画像\n\n{placeholder}\n"


def _preserve_image_placeholders(
    markdown: str,
    units: Sequence[ImageUnit],
    lines: Sequence[str],
) -> str:
    """Keep every owned image once and near its original surrounding text."""

    result = markdown.rstrip()
    for unit in units:
        first = result.find(unit.placeholder)
        if first < 0:
            before, after = _image_neighbors(lines, unit)
            result = _insert_image_near_context(
                result, unit.placeholder, before, after
            )
            continue
        tail = result[first + len(unit.placeholder) :].replace(unit.placeholder, "")
        result = result[: first + len(unit.placeholder)] + tail
    return result.rstrip() + "\n"


def _image_context(units: Sequence[ImageUnit], lines: Sequence[str]) -> str:
    if not units:
        return "（この節にトークンはない）"
    entries: list[str] = []
    for unit in units:
        before, after = _image_neighbors(lines, unit)
        context = []
        if before:
            context.append(f"直前: {before}")
        if after:
            context.append(f"直後: {after}")
        if unit.description:
            context.append(f"説明: {unit.description}")
        entries.append(
            f"- `{unit.placeholder}` — 原文 {unit.source_start}-{unit.source_end}行"
            + ("、" + "、".join(context) if context else "")
        )
    return "\n".join(entries)


def _seed_path(seed_root: Path, page: SeedPage) -> Path:
    return (seed_root / page.filename).resolve()


def _reference_ranges_from_markdown(
    markdown: str,
    owner_ranges: Sequence[tuple[int, int]],
    source_line_count: int,
) -> list[tuple[int, int]]:
    """Collect optional cross-page provenance markers without rejecting prose."""

    found: list[tuple[int, int]] = []
    for match in REFERENCE_MARKER_RE.finditer(markdown):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= source_line_count:
            continue
        if any(owner_start <= start and end <= owner_end for owner_start, owner_end in owner_ranges):
            continue
        found.append((start, end))
    return _merge_ranges(found)


def _write_reference_seeds(
    pages: Sequence[SeedPage],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    seed_root: Path,
) -> None:
    """Write stable, readable source snapshots for cross-page agent reads."""

    seed_root.mkdir(parents=True, exist_ok=True)
    for page in pages:
        write_text_atomic(
            _seed_path(seed_root, page),
            _numbered_source(lines, page.owner_ranges, _page_units(page, units)),
        )


def _numbered_source(
    lines: Sequence[str],
    ranges: Sequence[tuple[int, int]],
    units: Sequence[ImageUnit],
) -> str:
    """Render source with original line numbers while hiding image payloads."""

    units_by_start = {unit.source_start: unit for unit in units}
    rendered: list[str] = []
    for start, end in ranges:
        line_number = start
        while line_number <= end:
            unit = units_by_start.get(line_number)
            if unit is not None and unit.source_end <= end:
                rendered.append(
                    f"{unit.source_start}-{unit.source_end}: {unit.placeholder}"
                )
                line_number = unit.source_end + 1
                continue
            rendered.append(f"{line_number}: {lines[line_number - 1]}")
            line_number += 1
    return "\n".join(rendered) + "\n"


async def _structured_with_artifacts(
    *,
    schema: type[Any],
    prompt: Any,
    model: ModelPort,
    output_dir: Path,
    stem: str,
    attempts: int,
    max_output_tokens: int,
    stop_check: StopCheck,
    prompt_factory: Callable[[str | None], Any] | None = None,
    validator: Callable[[Any], str | None] | None = None,
    retry_temperature: float | None = None,
) -> tuple[Any | None, int, str]:
    """Run one small structured task and retain every prompt and result."""

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("reference research cancelled")
        current_prompt = (
            prompt_factory(last_error or None) if prompt_factory is not None else prompt
        )
        write_text_atomic(
            output_dir / f"{stem}-attempt-{attempt:02d}-prompt.md",
            current_prompt.render(),
        )
        try:
            raw = await model.structured(
                schema,
                current_prompt.messages(),
                max_output_tokens=max_output_tokens,
                temperature=retry_temperature,
            )
            result = raw if isinstance(raw, schema) else schema.model_validate(raw)
            validation_error = validator(result) if validator is not None else None
            if validation_error:
                last_error = validation_error[:1000]
                write_text_atomic(
                    output_dir / f"{stem}-attempt-{attempt:02d}-error.txt",
                    last_error + "\n",
                )
                continue
            write_json_atomic(output_dir / f"{stem}.json", result)
            return result, attempt, ""
        except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            write_text_atomic(
                output_dir / f"{stem}-attempt-{attempt:02d}-error.txt",
                last_error + "\n",
            )
    return None, max(1, attempts), last_error


def _valid_reference_facts(
    facts: Sequence[ReferenceFact], page: SeedPage, target: SeedPage
) -> list[ReferenceFact]:
    """Keep only cited facts that point inside the compared reference page."""

    valid: list[ReferenceFact] = []
    seen: set[tuple[str, int, int]] = set()
    for fact in facts:
        description = fact.description.strip()
        start, end = fact.source_start, fact.source_end
        if not description or not any(
            owner_start <= start <= end <= owner_end
            for owner_start, owner_end in page.owner_ranges
        ):
            continue
        key = (description, start, end)
        if key in seen:
            continue
        seen.add(key)
        if not any(s <= fact.target_line <= e for s, e in target.owner_ranges):
            fact.target_line = 0
        valid.append(fact)
    return valid


def _reference_validation_error(
    result: ReferenceResearchResult,
    references: Sequence[SeedPage],
) -> str | None:
    """Return actionable feedback before invalid cross-page facts are discarded."""

    allowed = [
        f"{page.title}: {_ranges_text(page.owner_ranges)}行"
        for page in references
    ]
    errors: list[str] = []
    if not result.useful_facts and not result.no_useful_information_reason.strip():
        errors.append("useful_factsが空の場合はno_useful_information_reasonを具体的に書くこと")
    for index, fact in enumerate(result.useful_facts, start=1):
        if not fact.description.strip():
            errors.append(f"useful_facts[{index}]にdescriptionがない")
        if not any(
            owner_start <= fact.source_start <= fact.source_end <= owner_end
            for page in references
            for owner_start, owner_end in page.owner_ranges
        ):
            errors.append(
                f"useful_facts[{index}]の出典範囲 {fact.source_start}-{fact.source_end} が参照ページ内にない"
            )
    if not errors:
        return None
    return (
        "参照ページの範囲外の事実や空の事実を返してはならない。"
        "修正したuseful_facts全体を返すこと。許可された参照範囲: "
        + ", ".join(allowed)
        + "\n- "
        + "\n- ".join(errors[:8])
    )


def _render_reference_research(
    target: SeedPage,
    evidence: Sequence[_ReferenceEvidence],
    *,
    seed_root: Path,
) -> str:
    """Create the compact, auditable evidence pack consumed by plan and writer."""

    rendered = [
        "# 参照調査結果",
        "",
        f"対象: {target.number:03d} {target.title}",
        "",
    ]
    useful_count = 0
    for item in evidence:
        rendered.extend(
            [
                f"## {item.page.number:03d} {item.page.title}",
                f"- 参照シード絶対パス: `{_seed_path(seed_root, item.page)}`",
                f"- 参照ページの原文範囲: {_ranges_text(item.page.owner_ranges)}行",
            ]
        )
        if not item.facts:
            rendered.extend(
                [
                    "- useful fact: なし",
                    "- 理由: "
                    + (item.no_useful_information_reason or "追加すべき事実は確認されなかった。"),
                    "",
                ]
            )
            continue
        for fact in item.facts:
            useful_count += 1
            rendered.extend(
                [
                    f"### useful fact {useful_count}",
                    f"- 追加する事実: {fact.description.strip()}",
                    f"- 必要な理由: {fact.reason.strip() or '対象記事を単独で理解しやすくするため。'}",
                    f"- 挿入場所: {fact.insertion_point.strip() or '関連する説明の直後'}",
                    f"- 出典: 原文 {fact.source_start}-{fact.source_end}行",
                    "",
                ]
            )
    if useful_count == 0:
        rendered.extend(
            [
                "## 結論",
                "比較した参照ページから対象記事へ追加すべき技術情報は確認されなかった。",
                "",
            ]
        )
    return "\n".join(rendered).rstrip() + "\n"


def _select_references(
    page: SeedPage,
    pages: Sequence[SeedPage],
    tokens: dict[int, set[str]],
    *,
    limit: int,
) -> list[SeedPage]:
    """Previous and next page always, plus the pages sharing the most vocabulary."""

    # ponytail: lexical ranking; add an LLM shortlist only if smoke runs miss related pages.
    others = [item for item in pages if item.number != page.number]
    if not others:
        return []
    counts: dict[str, int] = {}
    for vocabulary in tokens.values():
        for token in vocabulary:
            counts[token] = counts.get(token, 0) + 1
    common = {token for token, count in counts.items() if count > len(pages) / 2}
    mine = tokens.get(page.number, set()) - common
    adjacent = [item for item in others if abs(item.number - page.number) == 1]
    scored = sorted(
        (
            (len(mine & (tokens.get(item.number, set()) - common)), -item.number, item)
            for item in others
            if item not in adjacent
        ),
        key=lambda entry: (entry[0], entry[1]),
        reverse=True,
    )
    picks = [item for score, _, item in scored[: max(0, limit)] if score > 0]
    family = [item for item in others if item.path and item.path[:-1] == page.path[:-1]]
    unique = {item.number: item for item in adjacent + family[:limit] + picks}
    return sorted(list(unique.values())[: max(0, limit)], key=lambda item: item.number)


async def _research_references(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    tokens: dict[int, set[str]],
    model: ModelPort,
    config: WikiConfig,
    work_root: Path,
    seed_root: Path,
    stop_check: StopCheck,
    on_progress: Progress,
) -> tuple[list[_ReferenceEvidence], str]:
    """Python selects references; one structured compare call per page."""

    _emit(
        on_progress,
        "research",
        "page_start",
        page=page.title,
        current=0,
        total=len(pages),
    )
    selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
    if not selected:
        _emit(
            on_progress,
            "research",
            "page_done",
            page=page.title,
            total=len(pages),
            references=0,
        )
        return [], "# 参照調査結果\n\n他のWikiページはない。\n"

    research_dir = work_root / f"research-{page.number:03d}"
    cached = read_json(research_dir / "references.json", default={})
    if (
        isinstance(cached.get("useful_facts"), list)
        and "no_useful_information_reason" in cached
    ):
        result = ReferenceResearchResult.model_validate(cached)
        if _reference_validation_error(result, selected) is None:
            reason = result.no_useful_information_reason.strip()
            evidence = [
                _ReferenceEvidence(
                    page=candidate,
                    facts=_valid_reference_facts(result.useful_facts, candidate, page),
                    no_useful_information_reason=reason,
                )
                for candidate in selected
            ]
            research = _render_reference_research(page, evidence, seed_root=seed_root)
            write_text_atomic(research_dir / "reference-research.md", research)
            _emit(on_progress, "research", "resumed", page=page.title)
            _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), resumed=True)
            return evidence, research

    research_dir = clean_workdir(research_dir)
    target_summary = page.summary.strip() or page.title
    references = "\n\n".join(
        (
            f"--- 参照ページ {candidate.number:03d} {candidate.title}"
            f"（原文 {_ranges_text(candidate.owner_ranges)}行） ---\n"
            f"ページ要約: {candidate.summary}\n"
        )
        for candidate in selected
    )
    _emit(
        on_progress,
        "research",
        "selected",
        page=page.title,
        candidates=len(selected),
        references=[item.number for item in selected],
        context_characters=len(target_summary) + len(references),
        context_bytes=len((target_summary + references).encode("utf-8")),
    )
    prompt = reference_research_prompt(
        target_number=page.number,
        target_title=page.title,
        target_ranges=_ranges_text(page.owner_ranges),
        target_summary=target_summary,
        references=references,
        output_language=config.output_language,
    )
    result, attempts, error = await _structured_with_artifacts(
        schema=ReferenceResearchResult,
        prompt=prompt,
        model=model,
        output_dir=research_dir,
        stem="references",
        attempts=config.reference_attempts,
        max_output_tokens=config.reference_max_output_tokens,
        stop_check=stop_check,
        prompt_factory=lambda feedback: reference_research_prompt(
            target_number=page.number,
            target_title=page.title,
            target_ranges=_ranges_text(page.owner_ranges),
            target_summary=target_summary,
            references=references,
            output_language=config.output_language,
            last_error=feedback,
        ),
        validator=lambda candidate: _reference_validation_error(candidate, selected),
        retry_temperature=getattr(config, "retry_temperature", 0.7),
    )
    reason = (
        result.no_useful_information_reason.strip()
        if result is not None
        else f"調査呼び出し失敗: {error}"
    )
    evidence: list[_ReferenceEvidence] = []
    for current, candidate in enumerate(selected, start=1):
        # _valid_reference_facts keeps only facts inside this candidate's range.
        facts = _valid_reference_facts(result.useful_facts, candidate, page) if result else []
        evidence.append(
            _ReferenceEvidence(
                page=candidate, facts=facts, no_useful_information_reason=reason
            )
        )
        _emit(
            on_progress,
            "research",
            "reference_done",
            page=page.title,
            reference=candidate.title,
            current=current,
            total=len(selected),
            facts=len(facts),
            attempts=attempts,
            error=error,
        )

    research = _render_reference_research(page, evidence, seed_root=seed_root)
    write_text_atomic(research_dir / "reference-research.md", research)
    _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), references=len(selected))
    return evidence, research


def _judge_feedback(result: PageJudgeResult) -> list[str]:
    feedback: list[str] = []
    for omission in result.missing_important_information:
        location = (
            f"原文 {omission.source_start}-{omission.source_end}行"
            if omission.source_start > 0 and omission.source_end >= omission.source_start
            else "原文範囲未指定"
        )
        feedback.append(f"{location}: {omission.description.strip()}")
    return feedback


def _valid_reference_ranges(value: Sequence[Sequence[int]], source_line_count: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in value:
        if len(item) != 2:
            continue
        start, end = int(item[0]), int(item[1])
        if 1 <= start <= end <= source_line_count:
            result.append((start, end))
    return _merge_ranges(result)


def _load_seed_plan(
    path: Path,
    *,
    source_sha256: str,
    source_line_count: int,
    prompt_version: str,
    single_page_rewrite: bool = False,
    accept_stale_version: bool = False,
) -> list[SeedPage] | None:
    """Load the completed seed plan so a stopped run resumes at rewriting."""

    if not path.exists():
        return None
    try:
        raw = read_json(path)
        if raw.get("source_sha256") != source_sha256:
            return None
        if int(raw.get("source_line_count", 0)) != source_line_count:
            return None
        if raw.get("prompt_version") != prompt_version and not accept_stale_version:
            return None
        if bool(raw.get("single_page_rewrite", False)) != single_page_rewrite:
            return None
        pages = [
            SeedPage(
                number=int(item["number"]),
                title=str(item["title"]),
                chapter=str(item.get("chapter", "")),
                summary=str(item.get("summary", "")),
                owner_ranges=_merge_ranges(
                    [(int(start), int(end)) for start, end in item["owner_ranges"]]
                ),
                filename=str(item["filename"]),
                page_id=f"page-{int(item['number']):03d}",
                reference_ranges=_valid_reference_ranges(
                    item.get("reference_ranges", []), source_line_count
                ),
                path=[str(value) for value in item.get("path", [])],
            )
            for item in raw["pages"]
        ]
        if not pages or [page.number for page in pages] != list(range(1, len(pages) + 1)):
            return None
        if len({page.filename for page in pages}) != len(pages):
            return None
        if any(Path(page.filename).name != page.filename for page in pages):
            return None
        _verify_ranges(pages, source_line_count)
        return pages
    except (KeyError, OSError, TypeError, ValueError, PipelineError):
        return None


def _plan_cache_key(source_text: str, config: WikiConfig) -> str:
    """Key the shared plan by every input that can change seed ownership."""

    policy = policy_of(config)
    return hash_of({
        "version": PLAN_CACHE_VERSION,
        "source_sha256": sha256_text(source_text),
        "prompt_version": config.prompt_version,
        "seed_version": policy.cache_key(SEED_PLAN_VERSION),
        "compile_version": policy.cache_key(SEED_PLAN_COMPILE_VERSION),
        "source_kind": config.source_kind,
        "window_target_lines": config.window_target_lines,
        "window_overlap_lines": config.window_overlap_lines,
        "planner_attempts": config.planner_attempts,
        "regional_window_count": config.regional_window_count,
        "map_attempts": config.map_attempts,
        "planner_max_output_tokens": config.planner_max_output_tokens,
        "map_max_output_tokens": config.map_max_output_tokens,
        "page_target_lines": config.page_target_lines,
        "chat_base_url": config.chat_base_url,
        "chat_model": config.chat_model,
        **policy.model_cache_fields(config),
        "reasoning_effort": "low",
        "temperature": config.temperature,
        "retry_temperature": config.retry_temperature,
        "output_language": config.output_language,
        "structure_target_lines": config.structure_target_lines,
        "structure_min_lines": config.structure_min_lines,
        "slide_delimiter": config.slide_delimiter,
        "slide_title": config.slide_title,
        "pdf_use_headings": config.pdf_use_headings,
    })


def _plan_cache_path(source_text: str, config: WikiConfig) -> Path | None:
    if config.skip_excel_and_small and is_small_document(source_text):
        return None  # this document needs no foreground or ahead planner
    root = str(config.plan_cache_dir or "")
    return Path(root) / f"{_plan_cache_key(source_text, config)}.json" if root else None


def _load_plan_cache(path: Path | None, source_text: str, lines: list[str], config: WikiConfig) -> CompiledSeedPlan | None:
    if path is None or not path.exists():
        return None
    try:
        payload = read_json(path)
        if payload.get("schema_version") != 1 or payload.get("cache_key") != _plan_cache_key(source_text, config):
            return None
        plan = CompiledSeedPlan.model_validate(payload["plan"])
        from .document_map import validate_seed_plan

        checked, error = validate_seed_plan(
            plan,
            source_line_count=len(lines),
            block_index=build_block_index(lines),
            lines=lines,
            page_target_lines=config.page_target_lines,
        )
        return checked if error is None else None
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _store_plan_cache(path: Path | None, source_text: str, lines: list[str], config: WikiConfig,
                      plan: CompiledSeedPlan) -> None:
    if path is None:
        return
    write_json_atomic(path, {
        "schema_version": 1,
        "cache_key": _plan_cache_key(source_text, config),
        "source_sha256": sha256_text(source_text),
        "source_line_count": len(lines),
        "plan": plan,
    })


async def _compute_seed_plan(
    source_text: str,
    lines: list[str],
    *,
    config: WikiConfig,
    model: ModelPort | None,
    work_root: Path,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> tuple[CompiledSeedPlan, bool]:
    """Run the one canonical observation/seed planner implementation."""

    model = policy_of(config).planning_model(model)

    if config.skip_excel_and_small and is_small_document(source_text):
        title = next(
            (line[2:].strip() for line in lines if line.startswith("# ") and line[2:].strip()),
            config.document_slug or "Document",
        )
        return CompiledSeedPlan(summary=title, pages=[{
            "title": title, "summary": title, "source_start": 1, "source_end": len(lines),
        }]), True

    from ..formats import structural_seed_plan
    from .document_map import validate_seed_plan

    structural = await structural_seed_plan(
        lines,
        kind=config.source_kind,
        config=config,
        model=model,
        on_progress=on_progress,
        stop_check=stop_check,
    )
    seed_plan = None
    if structural is not None:
        seed_plan, error = validate_seed_plan(
            structural,
            source_line_count=len(lines),
            block_index=build_block_index(lines),
            lines=lines,
            page_target_lines=config.page_target_lines,
        )
        if seed_plan is None:
            _emit(on_progress, "seed", "structural_rejected", reason=error)
    policy = policy_of(config)
    if seed_plan is None:
        if policy.offline and model is None:
            seed_plan = policy.offline_seed_plan(lines)
        else:
            observations = await observe_document(
                source_text,
                model=model,
                config=config,
                document=document_id(normalize_source(source_text)),
                checkpoint_dir=work_root / "observations" / "checkpoints",
                live_output_dir=work_root / "observations" / "live",
                on_progress=on_progress,
                stop_check=stop_check,
            )
            seed_plan = await build_seed_plan(
                observations,
                lines=lines,
                model=model,
                config=config,
                checkpoint_dir=work_root / "planning",
                stop_check=stop_check,
                on_progress=on_progress,
            )
    return seed_plan, structural is not None


async def prepare_seed_plan_cache(
    source_text: str,
    *,
    config: WikiConfig,
    model: ModelPort | None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> bool:
    """Fill only the content-addressed planner cache for a future serial build."""

    lines = split_source_lines(normalize_source(source_text))
    target = _plan_cache_path(source_text, config)
    if target is None or _load_plan_cache(target, source_text, lines, config) is not None:
        return target is not None
    key = str(target.resolve())
    with _PLANNING_LOCK:
        running = _PLANNING.get(key)
        if running is None:
            running = threading.Event()
            _PLANNING[key] = running
            leader = True
        else:
            leader = False
    if not leader:
        await asyncio.to_thread(running.wait)
        return _load_plan_cache(target, source_text, lines, config) is not None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".planner-", dir=target.parent) as folder:
            plan, _structural = await _compute_seed_plan(
                source_text,
                lines,
                config=config,
                model=model,
                work_root=Path(folder),
                on_progress=on_progress,
                stop_check=stop_check,
            )
        _store_plan_cache(target, source_text, lines, config, plan)
        return True
    finally:
        with _PLANNING_LOCK:
            _PLANNING.pop(key, None)
        running.set()


async def _wait_for_plan_ahead(path: Path | None) -> None:
    if path is None:
        return
    with _PLANNING_LOCK:
        running = _PLANNING.get(str(path.resolve()))
    if running is not None:
        await asyncio.to_thread(running.wait)


def _page_state_path(state_root: Path, page: SeedPage) -> Path:
    return state_root / "pages" / f"{page.number:03d}.json"


def _write_page_state(
    path: Path,
    result: RewriteResult,
    *,
    rewrite_version: str,
    pages: Sequence[SeedPage] = (),
    source_path: Path | None = None,
    source_snapshot_path: Path | None = None,
    source_sha256: str = "",
) -> None:
    write_json_atomic(
        path,
        {
            "number": result.page.number,
            "title": result.page.title,
            "filename": result.page.filename,
            "status": "rewritten",
            "rewrite_version": rewrite_version,
            "source_ranges": _ranges_json(result.page.owner_ranges),
            "reference_ranges": _ranges_json(result.page.reference_ranges),
            "provenance": _page_provenance(
                result.page,
                pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            ),
            "content_sha256": sha256_text(result.markdown),
            "attempts": result.attempts,
            "judge_score": result.judge_score,
            "missing_important_information": result.missing_important_information,
            "defects": result.defects,
            "verbatim_sections": result.verbatim_sections,
        },
    )


def _resume_rewritten_page(
    output_path: Path,
    state_path: Path,
    page: SeedPage,
    *,
    rewrite_version: str,
    source_line_count: int,
    accept_stale_version: bool = False,
) -> RewriteResult | None:
    """Reuse a page whose sidecar state still matches the file on disk."""

    if not output_path.exists() or not state_path.exists():
        return None
    try:
        markdown = output_path.read_text(encoding="utf-8")
        state = read_json(state_path)
        if (
            (state.get("rewrite_version") != rewrite_version and not accept_stale_version)
            or state.get("filename") != page.filename
            or state.get("content_sha256") != sha256_text(markdown)
        ):
            return None
        page.reference_ranges = _valid_reference_ranges(
            state.get("reference_ranges", []), source_line_count
        )
        return RewriteResult(
            page=page,
            markdown=markdown,
            attempts=int(state.get("attempts", 0)),
            judge_score=state.get("judge_score"),
            missing_important_information=list(state.get("missing_important_information", [])),
            defects=list(state.get("defects", [])),
            verbatim_sections=list(state.get("verbatim_sections", [])),
        )
    except (OSError, TypeError, ValueError):
        return None


def _facts_text(
    facts: Sequence[ReferenceFact],
) -> str:
    if not facts:
        return "なし"
    rendered: list[str] = []
    for number, fact in enumerate(facts, start=1):
        rendered.append(
            f"## 追加事実 {number}\n"
            f"- 事実: {fact.description.strip()}\n"
            f"- 理由: {fact.reason.strip() or '単独で理解するために必要'}\n"
            f"- 出典: 原文 {fact.source_start}-{fact.source_end}行\n"
        )
    return "\n\n".join(rendered)


def _verbatim_section(
    lines: Sequence[str], start: int, end: int, units: Sequence[ImageUnit]
) -> str:
    """Lossless by construction: the exact source lines, images as placeholders."""

    return demote_h1(_prompt_safe(slice_text(list(lines), start, end), units)).rstrip() + "\n"


def _nav_footer(page: SeedPage, pages: Sequence[SeedPage]) -> str:
    previous = next((item for item in pages if item.number == page.number - 1), None)
    following = next((item for item in pages if item.number == page.number + 1), None)
    parts = []
    if previous:
        parts.append(f"前のページ: [{previous.title}]({previous.filename})")
    if following:
        parts.append(f"次のページ: [{following.title}]({following.filename})")
    if page.path[:-1]:
        parent = next((item for item in pages if item.path == page.path[:-1]), None)
        if parent is None:
            parent = next((item for item in pages if item.path[: len(page.path) - 1] == page.path[:-1]), None)
        if parent and parent.filename != page.filename:
            parts.insert(0, f"親: [{parent.title}]({parent.filename})")
    return ("\n---\n\n" + " ｜ ".join(parts) + "\n") if parts else ""


async def _write_section(
    page: SeedPage,
    start: int,
    end: int,
    *,
    index: int,
    count: int,
    facts: Sequence[ReferenceFact],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: WikiConfig,
    task_dir: Path,
    stop_check: StopCheck,
    on_progress: Progress,
    context: str = "",
) -> _SectionResult:
    """Write one section until Python's lossless checks and the judge are satisfied."""

    # ponytail: markers plus the section judge cover enrichment; add a dedicated judge if not.
    section_units = [
        unit for unit in units if start <= unit.source_start and unit.source_end <= end
    ]
    placeholders = [unit.placeholder for unit in section_units]
    numbered = _numbered_source(lines, [(start, end)], section_units)
    source_text = _prompt_safe(slice_text(list(lines), start, end), section_units)
    facts_text = _facts_text(facts)
    stem = f"section-{index:02d}"
    policy = policy_of(config)
    if policy.offline and model is None:
        return _SectionResult(
            markdown=_verbatim_section(lines, start, end, section_units),
            attempts=0,
            errors=["model unavailable"],
            verbatim=True,
        )
    candidates: list[_SectionCandidate] = []
    feedback: list[str] = []

    for attempt in range(1, max(1, config.write_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("page writing cancelled")
        prompt = section_write_prompt(
            page_title=page.title,
            page_summary=page.summary,
            index=index,
            count=count,
            source_start=start,
            source_end=end,
            numbered_section=numbered,
            facts_text=facts_text,
            image_context=_image_context(section_units, lines),
            output_language=config.output_language,
            feedback=feedback,
            context=context,
            code_identifiers=(
                []
                if config.source_kind in {"csv", "xlsx"}
                else sorted(policy.code_tokens(source_text))
            ),
            policy_rules=policy.wiki_prompt_rules("writer"),
            guidance=config.human_guidance.get(page.filename, ()),
        )
        rendered_prompt = prompt.render()
        cached_prompt = task_dir / f"{stem}-attempt-{attempt:02d}-prompt.md"
        cached_draft = task_dir / f"{stem}-attempt-{attempt:02d}.md"
        cached_judge = task_dir / f"{stem}-judge-{attempt:02d}.json"
        if (
            attempt == 1
            and cached_prompt.exists()
            and cached_draft.exists()
            and cached_prompt.read_text(encoding="utf-8") == rendered_prompt
        ):
            draft = cached_draft.read_text(encoding="utf-8")
            errors = check_section(
                draft,
                lines=lines,
                source_text=source_text,
                block_ranges=(),
                placeholders=placeholders,
                facts=facts,
                check_identifiers=config.source_kind not in {"csv", "xlsx"},
                tokens=policy.code_tokens,
            )
            judgment = read_json(cached_judge, default={})
            if (
                not errors
                and judgment
                and not judgment.get("missing_important_information")
                and not judgment.get("defects")
            ):
                _emit(
                    on_progress,
                    "write",
                    "section_resumed",
                    page=page.title,
                    section=index,
                )
                return _SectionResult(
                    markdown=draft,
                    attempts=0,
                    score=int(judgment.get("coverage_score", 0)),
                )
        write_text_atomic(
            cached_prompt, rendered_prompt
        )
        try:
            raw = await model.text(
                prompt.messages(), max_output_tokens=config.write_max_output_tokens
            )
        except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
            feedback = [f"前回の呼び出しが失敗した: {type(exc).__name__}: {exc}"[:500]]
            write_text_atomic(
                task_dir / f"{stem}-attempt-{attempt:02d}-error.txt", feedback[0] + "\n"
            )
            continue
        draft = normalize_draft(raw)
        # Gemma sometimes wraps the image token in inline code (`[[NEO-IMAGE:...]]`),
        # which survives restoration as a base64 blob inside backticks and renders
        # broken. Unwrap the token before any placeholder handling.
        draft = re.sub(r"`+(\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\])`+", r"\1", draft)
        draft = PLACEHOLDER_RE.sub(
            lambda match: match.group(0) if match.group(0) in placeholders else "",
            draft,
        )
        draft = _preserve_image_placeholders(draft, section_units, lines)
        write_text_atomic(task_dir / f"{stem}-attempt-{attempt:02d}.md", draft)
        errors = check_section(
            draft,
            lines=lines,
            source_text=source_text,
            block_ranges=(),
            placeholders=placeholders,
            facts=facts,
            check_identifiers=config.source_kind not in {"csv", "xlsx"},
            tokens=policy.code_tokens,
        )
        if errors:
            candidates.append(_SectionCandidate(draft, attempt, errors=errors))
            feedback = errors
            _emit(
                on_progress, "write", "section_retry",
                page=page.title, section=index, attempt=attempt,
                error="; ".join(errors)[:300],
            )
            continue
        judge_original = numbered
        if facts:
            judge_original += "\n\n--- 他ページから追加した事実（必ず反映） ---\n" + facts_text
        judgment, _judge_attempts, judge_error = await _structured_with_artifacts(
            schema=PageJudgeResult,
            prompt=page_judge_prompt(
                page_title=f"{page.title}（節 {index}/{count}）",
                owner_ranges=f"{start}-{end}",
                numbered_original=judge_original,
                candidate=draft,
                output_language=config.output_language,
                policy_rules=policy.wiki_prompt_rules("judge"),
            ),
            model=judge_model(model),
            output_dir=task_dir,
            stem=f"{stem}-judge-{attempt:02d}",
            attempts=config.judge_attempts,
            max_output_tokens=config.judge_max_output_tokens,
            stop_check=stop_check,
            retry_temperature=getattr(config, "retry_temperature", 0.7),
        )
        if judgment is None:
            # Under a strict judge a failed judge is not approval; the safe fallback
            # is the source section after the bounded repair budget.
            candidates.append(
                _SectionCandidate(
                    draft,
                    attempt,
                    errors=[judge_error or "judge unavailable"] if policy.strict_judge else [],
                )
            )
            _emit(on_progress, "write", "judge_unavailable",
                  page=page.title, section=index, attempt=attempt, error=judge_error)
            break
        missing = _judge_feedback(judgment)
        defects = [item.strip() for item in judgment.defects if item and item.strip()]
        candidates.append(
            _SectionCandidate(
                draft, attempt, score=judgment.coverage_score, missing=missing, defects=defects
            )
        )
        _emit(
            on_progress, "write", "section_judged",
            page=page.title, section=index, attempt=attempt,
            score=judgment.coverage_score, missing=len(missing), defects=len(defects),
        )
        if not missing and not defects:
            break
        feedback = missing + [f"品質: {item}" for item in defects]

    clean = [item for item in candidates if not item.errors]
    if clean:
        best = max(
            clean,
            key=lambda item: (
                not item.missing,
                not item.defects,
                item.score if item.score is not None else 0,
                item.attempt,
            ),
        )
        if policy.strict_judge and (best.missing or best.defects):
            _emit(
                on_progress,
                "write",
                "section_verbatim",
                page=page.title,
                section=index,
                error="judge reported missing information or defects",
            )
            return _SectionResult(
                markdown=_verbatim_section(lines, start, end, section_units),
                attempts=len(candidates),
                errors=best.missing + best.defects,
                verbatim=True,
            )
        return _SectionResult(
            markdown=best.markdown,
            attempts=len(candidates),
            score=best.score,
            missing=best.missing,
            defects=best.defects,
        )
    errors = candidates[-1].errors if candidates else list(feedback)
    _emit(on_progress, "write", "section_verbatim",
          page=page.title, section=index, error="; ".join(errors)[:300])
    return _SectionResult(
        markdown=_verbatim_section(lines, start, end, section_units),
        attempts=len(candidates),
        errors=errors,
        verbatim=True,
    )


async def _write_intro(
    page: SeedPage,
    body: str,
    *,
    model: ModelPort,
    config: WikiConfig,
    task_dir: Path,
    stop_check: StopCheck,
    context: str = "",
) -> str:
    """One lead paragraph. Anything it cannot justify from the body is dropped."""

    if not policy_of(config).intro:
        return ""

    if stop_check and stop_check():
        raise asyncio.CancelledError("page writing cancelled")
    prompt = intro_prompt(
        page_title=page.title,
        page_summary=page.summary,
        body=body,
        output_language=config.output_language,
        context=context,
        guidance=config.human_guidance.get(page.filename, ()),
    )
    rendered_prompt = prompt.render()
    cached_prompt = task_dir / "intro-prompt.md"
    cached_intro = task_dir / "intro.md"
    if (
        cached_prompt.exists()
        and cached_intro.exists()
        and cached_prompt.read_text(encoding="utf-8") == rendered_prompt
    ):
        return cached_intro.read_text(encoding="utf-8").strip()
    write_text_atomic(cached_prompt, rendered_prompt)
    try:
        raw = await model.text(
            prompt.messages(), max_output_tokens=config.intro_max_output_tokens
        )
    except Exception as exc:  # noqa: BLE001 - the summary is an acceptable intro
        write_text_atomic(task_dir / "intro-error.txt", f"{type(exc).__name__}: {exc}\n")
        return page.summary
    text = PLACEHOLDER_RE.sub("", normalize_draft(raw))
    text = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.startswith("#") and "](" not in line
    ).strip()
    if not text or code_tokens(text) - code_tokens(body):
        return page.summary
    write_text_atomic(task_dir / "intro.md", text + "\n")
    return text


async def _rewrite_page(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: list[str],
    units: Sequence[ImageUnit],
    tokens: dict[int, set[str]],
    model: ModelPort,
    config: WikiConfig,
    work_root: Path,
    seed_root: Path,
    source_line_count: int,
    stop_check: StopCheck,
    on_progress: Progress,
    parents: dict[str, str] | None = None,
) -> RewriteResult:
    from ..formats.context import context_block

    if len(page.owner_ranges) != 1:
        raise PipelineError(f"page {page.number} must own one contiguous range")
    start, end = page.owner_ranges[0]
    page_units = _page_units(page, units)
    task_dir = work_root / f"page-{page.number:03d}"
    task_dir.mkdir(parents=True, exist_ok=True)

    policy = policy_of(config)
    if not policy.research:
        evidence, _research = [], ""
    else:
        evidence, _research = await _research_references(
            page,
            pages=pages,
            tokens=tokens,
            model=model,
            config=config,
            work_root=work_root,
            seed_root=seed_root,
            stop_check=stop_check,
            on_progress=on_progress,
        )
    facts = [fact for item in evidence for fact in item.facts]
    _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
    # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
    if config.skip_excel_and_small and is_small_document("\n".join(lines)):
        sections = [(start, end)]
    else:
        sections = split_sections(
            lines, start, end,
            target=config.section_target_lines, min_lines=config.section_min_lines,
        )
    buckets = assign_facts(facts, sections)

    drafts: list[str] = []
    scores: list[int] = []
    missing: list[str] = []
    defects: list[str] = []
    verbatim: list[str] = []
    attempts = 0
    for index, ((s, e), section_facts) in enumerate(zip(sections, buckets), start=1):
        result = await _write_section(
            page, s, e, index=index, count=len(sections), facts=section_facts,
            lines=lines, units=units, model=model, config=config, task_dir=task_dir,
            stop_check=stop_check, on_progress=on_progress,
            context=context_block(page, pages, parents or {}),
        )
        drafts.append(policy.section(result.markdown).rstrip())
        attempts += result.attempts
        if result.score is not None:
            scores.append(result.score)
        missing.extend(f"原文 {s}-{e}行: {item}" for item in result.missing)
        defects.extend(f"原文 {s}-{e}行: {item}" for item in result.defects)
        if result.verbatim:
            verbatim.append(f"原文 {s}-{e}行: " + "; ".join(result.errors))

    body = "\n\n".join(drafts)
    intro = await _write_intro(
        page, body, model=model, config=config, task_dir=task_dir,
        stop_check=stop_check, context=context_block(page, pages, parents or {}),
    )
    markdown = f"# {page.title}\n\n{intro.rstrip()}\n\n{body}\n" if intro.strip() or policy.intro else f"# {page.title}\n\n{body}\n"
    masked, unmask = policy.mask_for_linking(markdown)
    markdown = unmask(link_titles(
        masked,
        [(item.title, item.filename) for item in pages if item.number != page.number],
    ))
    markdown += _nav_footer(page, pages)
    restored, unresolved = restore_images(markdown, page_units)
    if unresolved:
        raise PipelineError(f"page {page.number} has unresolved image placeholders: {unresolved}")
    page.reference_ranges = _merge_ranges([
        (fact.source_start, fact.source_end) for fact in facts
        if 1 <= fact.source_start <= fact.source_end <= source_line_count
        and not any(start <= fact.source_start and fact.source_end <= end for start, end in page.owner_ranges)
    ])
    restored = strip_reader_references(restored)
    return RewriteResult(
        page=page,
        markdown=restored,
        attempts=attempts,
        judge_score=min(scores) if scores else None,
        missing_important_information=missing,
        defects=defects,
        verbatim_sections=verbatim,
    )


async def _rewrite_all(
    pages: Sequence[SeedPage],
    *,
    lines: list[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: WikiConfig,
    work_root: Path,
    seed_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
    source_snapshot_path: Path,
    source_sha256: str,
    source_line_count: int,
    stop_check: StopCheck,
    on_progress: Progress,
    parents: dict[str, str] | None = None,
) -> list[RewriteResult]:
    semaphore = asyncio.Semaphore(max(1, config.rewrite_concurrency))
    results: list[RewriteResult] = []
    pending: list[SeedPage] = []
    rewrite_version = policy_of(config).cache_key(REWRITE_PROMPT_VERSION)
    for page in pages:
        output_path = wiki_root / page.filename
        state_path = _page_state_path(state_root, page)
        resumed = (
            _resume_rewritten_page(
                output_path, state_path, page,
                rewrite_version=rewrite_version,
                source_line_count=source_line_count,
                accept_stale_version=config.require_resume,
            )
            if config.resume
            else None
        )
        if resumed is not None:
            results.append(resumed)
            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages), resumed=True)
            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages), resumed=True)
            _emit(on_progress, "rewrite", "page_resumed",
                  current=len(results), total=len(pages), page=page.title)
        else:
            output_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            pending.append(page)

    handled = await policy_of(config).rewrite_pending_pages(
        pending=pending,
        results=results,
        pages=pages,
        lines=lines,
        units=units,
        model=model,
        config=config,
        work_root=work_root,
        wiki_root=wiki_root,
        state_root=state_root,
        source_path=source_path,
        source_snapshot_path=source_snapshot_path,
        source_sha256=source_sha256,
        source_line_count=source_line_count,
        rewrite_version=rewrite_version,
        stop_check=stop_check,
        on_progress=on_progress,
        parents=parents,
    )
    if handled:
        return sorted(results, key=lambda item: item.page.number)

    tokens = {
        item.number: word_tokens(
            _prompt_safe(_slice_ranges(lines, item.owner_ranges), _page_units(item, units))
        )
        for item in pages
    }

    async def one(page: SeedPage) -> RewriteResult:
        async with semaphore:
            result = await _rewrite_page(
                page, pages=pages, lines=lines, units=units, tokens=tokens,
                model=model, config=config, work_root=work_root, seed_root=seed_root,
                source_line_count=source_line_count,
                stop_check=stop_check, on_progress=on_progress,
                parents=parents,
            )
            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages))
            return result

    for completed, task in enumerate(
        asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
    ):
        result = await task
        results.append(result)
        write_text_atomic(wiki_root / result.page.filename, result.markdown)
        _write_page_state(
            _page_state_path(state_root, result.page), result,
            rewrite_version=rewrite_version, pages=pages,
            source_path=source_path, source_snapshot_path=source_snapshot_path,
            source_sha256=source_sha256,
        )
        _emit(
            on_progress, "rewrite", "page_done",
            current=completed, total=len(pages), page=result.page.title,
            attempts=result.attempts, score=result.judge_score,
            verbatim_sections=len(result.verbatim_sections),
        )
    return sorted(results, key=lambda item: item.page.number)


def _index_text(title: str, pages: Sequence[SeedPage]) -> str:
    lines = [f"# {title}", "", "ページは原文での登場順に並んでいます。", ""]
    seen: set[tuple[str, ...]] = set()
    for page in pages:
        parent = tuple(page.path[:-1])
        if page.path and parent not in seen:
            lines.extend([f"## {' › '.join(parent) or page.path[0]}", ""])
            seen.add(parent)
        lines.append(
            f"- [{page.title}]({page.filename}) — "
            f"{page.summary or '要約なし'}"
        )
    return "\n".join(lines) + "\n"


def _verify_ranges(pages: Sequence[SeedPage], source_line_count: int) -> None:
    expected = 1
    for page in sorted(pages, key=lambda item: item.number):
        for start, end in page.owner_ranges:
            if start != expected:
                raise PipelineError(
                    f"seed ownership gap/overlap before page {page.number}: "
                    f"expected line {expected}, got {start}"
                )
            if end < start or end > source_line_count:
                raise PipelineError(f"invalid owner range on page {page.number}: {start}-{end}")
            expected = end + 1
    if expected != source_line_count + 1:
        raise PipelineError(
            f"seed ownership stops at line {expected - 1}; source has {source_line_count} lines"
        )


def _manifest(
    *,
    source_path: Path,
    source_snapshot_path: Path,
    source_text: str,
    pages: Sequence[SeedPage],
    results: Sequence[RewriteResult],
) -> dict[str, Any]:
    by_number = {item.page.number: item for item in results}
    source_hash = sha256_text(source_text)
    return {
        "source": str(source_path.resolve()),
        "source_snapshot": str(source_snapshot_path.resolve()),
        "source_sha256": source_hash,
        "source_line_count": len(split_source_lines(normalize_source(source_text))),
        "pages": [
            {
                "number": page.number,
                "title": page.title,
                "chapter": page.chapter,
                "filename": page.filename,
                "owner_ranges": _ranges_json(page.owner_ranges),
                "reference_ranges": _ranges_json(page.reference_ranges),
                "provenance": _page_provenance(
                    page,
                    pages,
                    source_path=source_path,
                    source_snapshot_path=source_snapshot_path,
                    source_sha256=source_hash,
                ),
                "status": "rewritten",
                "attempts": by_number[page.number].attempts,
                "judge_score": by_number[page.number].judge_score,
                "missing_important_information": by_number[page.number].missing_important_information,
                "defects": by_number[page.number].defects,
                "verbatim_sections": by_number[page.number].verbatim_sections,
            }
            for page in pages
        ],
    }


async def run_pipeline(
    source_path: Path | str,
    *,
    config: WikiConfig | None = None,
    model: ModelPort | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> Path:
    """Build seed pages, then independently plan and rewrite each page."""

    source_path = Path(source_path).resolve()
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode("utf-8")
    normalized = normalize_source(source_text)
    lines = split_source_lines(normalized)
    config = config or WikiConfig()
    single_page_rewrite = config.skip_excel_and_small and is_small_document(source_text)
    if single_page_rewrite and not config.document_slug:
        config = config.model_copy(update={"document_slug": source_path.stem})
    policy = policy_of(config)
    # An offline policy may run without a model client (deterministic smoke runs);
    # every other caller keeps the historical implicit client construction.
    if not (model is None and policy.offline and not config.chat_base_url):
        if model is None:
            model = policy.model_port(config)
    slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
    _emit(on_progress, "planner", "start", source_lines=len(lines))
    run_root = (
        Path(config.run_dir).resolve()
        if config.run_dir
        else Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
    )
    source_root = run_root / "source"
    state_root = run_root / "state"
    work_root = run_root / "work"
    wiki_root = run_root / "wiki"
    for directory in (source_root, state_root, work_root):
        directory.mkdir(parents=True, exist_ok=True)
    wiki_root.mkdir(parents=True, exist_ok=True)
    source_snapshot_path = (source_root / "original.md").resolve()
    write_text_atomic(source_snapshot_path, source_text)

    source_hash = sha256_text(source_text)
    plan_path = state_root / "plan.json"
    pages = _load_seed_plan(
        plan_path,
        source_sha256=source_hash,
        source_line_count=len(lines),
        prompt_version=policy.cache_key(SEED_PLAN_VERSION),
        single_page_rewrite=single_page_rewrite,
        accept_stale_version=config.require_resume,
    ) if config.resume else None
    if pages is not None:
        block_index = build_block_index(lines)
        if any(
            start > 1 and not block_index.cut_is_safe(start)
            for page in pages for start, _end in page.owner_ranges
        ):
            pages = None
    if pages is None and config.require_resume:
        raise ResumeUnavailable(f"stored seed plan for {source_path.name} could not be resumed")
    resumed_seed_plan = pages is not None
    if pages is not None:
        _emit(on_progress, "seed", "resumed", pages=len(pages), source_lines=len(lines))
    else:
        _emit(on_progress, "seed", "start", source_lines=len(lines))
        if wiki_root.exists():
            shutil.rmtree(wiki_root)
        wiki_root.mkdir(parents=True, exist_ok=True)
        plan_cache = _plan_cache_path(source_text, config)
        await _wait_for_plan_ahead(plan_cache)
        seed_plan = _load_plan_cache(plan_cache, source_text, lines, config)
        if seed_plan is not None:
            plan_kind = "cached"
        else:
            seed_plan, structural = await _compute_seed_plan(
                source_text,
                lines,
                config=config,
                model=model,
                work_root=work_root,
                on_progress=on_progress,
                stop_check=stop_check,
            )
            _store_plan_cache(plan_cache, source_text, lines, config, seed_plan)
            plan_kind = "single_page" if single_page_rewrite else "structural" if structural else "llm"
        _emit(on_progress, "seed", plan_kind, pages=len(seed_plan.pages))
        pages = _plan_pages(seed_plan, keep=config.identity_hints)
        _verify_ranges(pages, len(lines))

    for page in pages:
        page.title = policy.title(page.title)

    # A generated title can be valid as text but too large as a UTF-8
    # filesystem component. Repair it before seeds, pages, or metadata use the
    # filename. The visible wiki title remains unchanged.
    await _repair_long_page_filenames(
        pages,
        model=policy.planning_model(model),
        on_progress=on_progress,
        stop_check=stop_check,
    )

    from ..formats.context import summarize_hierarchy

    parents: dict[str, str] = {}
    if any(page.path for page in pages):
        _emit(on_progress, "context", "start", parents=len({tuple(page.path[:-1]) for page in pages}))
        parents = await summarize_hierarchy(
            pages,
            lines,
            model=policy.planning_model(model),
            config=config,
            checkpoint=state_root / "context.json",
            stop_check=stop_check,
        )
        _emit(on_progress, "context", "done")
    units = policy.writer_units(lines, extract_image_units(lines))
    seed_root = (work_root / "seeds").resolve()
    if not resumed_seed_plan:
        if seed_root.exists():
            shutil.rmtree(seed_root)
        page_state_root = state_root / "pages"
        if page_state_root.exists():
            shutil.rmtree(page_state_root)
        for obsolete in (
            work_root / "chunks",
            work_root / "map",
            source_root / "slices",
        ):
            if obsolete.exists():
                shutil.rmtree(obsolete)
        for pattern in ("page-*", "research-*"):
            for old_attempt in work_root.glob(pattern):
                if old_attempt.is_dir():
                    shutil.rmtree(old_attempt)
    _write_reference_seeds(pages, lines, units, seed_root)
    plan_json = {
        "source": str(source_path),
        "source_snapshot": str(source_snapshot_path),
        "source_sha256": sha256_text(source_text),
        "source_line_count": len(lines),
        "prompt_version": policy.cache_key(SEED_PLAN_VERSION),
        "pages": [
            {
                "number": page.number,
                "title": page.title,
                "chapter": page.chapter,
                "path": page.path,
                "summary": page.summary,
                "filename": page.filename,
                "owner_ranges": _ranges_json(page.owner_ranges),
                "reference_ranges": _ranges_json(page.reference_ranges),
                "provenance": _page_provenance(
                    page,
                    pages,
                    source_path=source_path,
                    source_snapshot_path=source_snapshot_path,
                    source_sha256=source_hash,
                ),
            }
            for page in pages
        ],
    }
    if single_page_rewrite:
        plan_json["single_page_rewrite"] = True
    write_json_atomic(plan_path, plan_json)
    write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
    _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))
    _emit(on_progress, "planner", "done", current=len(pages), total=len(pages), pages=len(pages))

    results = await _rewrite_all(
        pages,
        lines=lines,
        units=units,
        model=model,
        config=config,
        work_root=work_root,
        seed_root=seed_root,
        wiki_root=wiki_root,
        state_root=state_root,
        source_path=source_path,
        source_snapshot_path=source_snapshot_path,
        source_sha256=source_hash,
        source_line_count=len(lines),
        stop_check=stop_check,
        on_progress=on_progress,
        parents=parents,
    )
    for item, page in zip(plan_json["pages"], pages):
        previous_title = str(item.get("title") or "")
        item["title"] = page.title
        if (
            isinstance(item.get("path"), list)
            and item["path"]
            and str(item["path"][-1]) == previous_title
        ):
            item["path"][-1] = page.title
        item["reference_ranges"] = _ranges_json(page.reference_ranges)
        item["provenance"] = _page_provenance(
            page,
            pages,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_hash,
        )
    write_json_atomic(plan_path, plan_json)
    write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
    manifest = _manifest(
        source_path=source_path,
        source_snapshot_path=source_snapshot_path,
        source_text=source_text,
        pages=pages,
        results=results,
    )
    write_json_atomic(state_root / "manifest.json", manifest)
    (wiki_root / "manifest.json").unlink(missing_ok=True)
    flagged = [item for item in results if item.verbatim_sections]
    if flagged:
        review = [
            "# 人手による確認が必要です",
            "",
            "書き換え時に情報が欠落したため、次の節は原文のまま公開されています。"
            "内容を確認してください。",
            "",
        ]
        for item in flagged:
            review.append(f"- `{item.page.filename}`")
            review.extend(f"  - {note}" for note in item.verbatim_sections)
        write_text_atomic(wiki_root / "_review.md", "\n".join(review) + "\n")
    else:
        (wiki_root / "_review.md").unlink(missing_ok=True)

    write_json_atomic(
        state_root / "run.json",
        {
            "source_sha256": sha256_text(source_text),
            "source_line_count": len(lines),
            "pages": len(pages),
            "rewritten": len(pages),
            "review_pages": len(flagged),
            "verbatim_sections": sum(len(item.verbatim_sections) for item in results),
            "published": True,
        },
    )
    _emit(on_progress, "publish", "done", output=str(wiki_root), review_pages=len(flagged))
    return run_root
