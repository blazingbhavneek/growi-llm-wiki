"""One writer entry point for chunks and wiki output."""

from __future__ import annotations

import shutil
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from graph.config import app_concurrency

StopCheck = Callable[[], bool] | None
Progress = Callable[[dict[str, Any]], None] | None


def _apply_model_patches(current: str, result: Any, expected: set[int]) -> str:
    patches = list(getattr(result, "patches", []))
    unchanged = {int(edit_id) for edit_id in getattr(result, "unchanged_edit_ids", [])}
    if any(not patch.edit_ids for patch in patches):
        raise ValueError("every patch needs at least one edit id")
    patched = {int(edit_id) for patch in patches for edit_id in patch.edit_ids}
    if patched & unchanged:
        raise ValueError(f"edit ids cannot be both patched and unchanged: {sorted(patched & unchanged)}")
    if patched | unchanged != expected:
        raise ValueError(
            f"edit_ids plus unchanged_edit_ids must cover exactly {sorted(expected)}; got {sorted(patched | unchanged)}"
        )
    if not patches:
        return current
    replacements: list[tuple[int, int, str]] = []
    for patch in patches:
        before = str(patch.before)
        found = current.count(before) if before else 0
        if found != 1:
            fix = ("copy it character for character from the current page" if not found
                   else "include more surrounding text so it matches only one place")
            raise ValueError(f"each patch.before must occur exactly once in the current page; "
                             f"{before[:120]!r} occurs {found} times: {fix}")
        start = current.index(before)
        replacements.append((start, start + len(before), str(patch.after)))
    replacements.sort()
    for left, right in zip(replacements, replacements[1:]):
        if left[1] > right[0]:
            raise ValueError(f"patch.before ranges overlap ({current[left[0]:left[1]][:80]!r} and "
                             f"{current[right[0]:right[1]][:80]!r}): merge them into one patch listing both edit ids")
    updated = current
    for start, end, after in reversed(replacements):
        updated = updated[:start] + after + updated[end:]
    return updated.rstrip() + "\n"
def _edit_blocks(
    old_lines: list[str],
    new_lines: list[str],
    hunks: list[tuple[int, int, int, int]],
    indexes: set[int],
) -> str:
    blocks: list[str] = []
    for index in sorted(indexes):
        old_start, old_len, new_start, new_len = hunks[index]
        kind = "ADD" if old_len == 0 else "DELETE" if new_len == 0 else "UPDATE"
        old = "\n".join(old_lines[old_start - 1 : old_start - 1 + old_len]) or "（なし）"
        new = "\n".join(new_lines[new_start - 1 : new_start - 1 + new_len]) or "（なし）"
        blocks.append(
            f"## EDIT {index + 1}: {kind}\n"
            f"旧版 {old_start}-{old_start + max(old_len, 1) - 1}行:\n{old}\n\n"
            f"新版 {new_start}-{new_start + max(new_len, 1) - 1}行:\n{new}"
        )
    return "\n\n".join(blocks)


class _EditCancelled(RuntimeError):
    pass


def _apply_incremental_edits(
    state_root: Path,
    old_text: str,
    new_text: str,
    decision: Any,
    *,
    settings: Any,
    llm: Any,
    stop_check: StopCheck = None,
    guidance: dict[str, list[str]] | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Let the model patch every tier-1 page. Returns (changed pages, failed pages)."""

    import asyncio
    from collections import Counter

    from graph.common.async_tools import run_async_blocking
    from graph.wiki.images import (
        extract_image_units,
        image_identity_counts,
        placeholders_in,
        restore_images,
        sanitize_lines,
        scrub_base64,
    )
    from graph.wiki.incremental import source_lines
    from common.policy import policy_of
    from graph.wiki.model import ChatModelPort
    from graph.wiki.page import check_section, code_tokens, verbatim_blocks
    from graph.wiki.prompts import incremental_page_edit_prompt
    from graph.wiki.storage import read_json, sha256_text, write_json_atomic, write_text_atomic
    from graph.wiki.wire import IncrementalPageEditResult

    state_root = Path(state_root)
    hunks = decision.hunks
    old_lines, new_lines = source_lines(old_text), source_lines(new_text)
    plan = read_json(state_root / "state" / "plan.json", default={})
    pages = {str(page["filename"]): page for page in plan.get("pages", [])}
    old_units = extract_image_units(old_lines)
    new_units = extract_image_units(new_lines)

    old_prompt_lines = sanitize_lines(old_lines, old_units)
    new_prompt_lines = sanitize_lines(new_lines, new_units)
    if hasattr(llm, "structured"):
        # Tests and embedded callers can supply the complete model port.  Do
        # not require the production endpoint settings in that path: besides
        # being unnecessary, doing so broke human-overlay tier integration
        # after the phase/configuration split.
        model = llm
    else:
        config = wiki_config(settings, run_dir=state_root)
        model = policy_of(config).model_port(config) if llm is None else ChatModelPort(config, llm=llm)
    attempts = max(5, int(getattr(settings, "wiki_write_attempts", 5)))
    semaphore = asyncio.Semaphore(
        max(1, int(getattr(settings, "wiki_rewrite_concurrency", getattr(settings, "concurrency", app_concurrency()))))
    )

    async def edit_page(filename: str, indexes: set[int]) -> str:
        if stop_check and stop_check():
            raise _EditCancelled("incremental wiki edit cancelled")
        page = pages.get(filename)
        path = state_root / "wiki" / filename
        if page is None or not path.exists():
            raise ValueError(f"incremental page is unavailable: {filename}")
        owned = set(decision.owned.get(filename, set())) & indexes
        original = path.read_text(encoding="utf-8")
        page_units = extract_image_units(original.splitlines())
        prompt_page = original
        for unit in page_units:
            prompt_page = prompt_page.replace(unit.raw, unit.placeholder)
        ranges = [(int(first), int(last)) for first, last in page.get("owner_ranges", [])]
        old_touched_units = [
            unit for unit in old_units
            if any(
                old_len > 0 and unit.source_start <= old_start + old_len - 1 and old_start <= unit.source_end
                for index in owned
                for old_start, old_len, _new_start, _new_len in [hunks[index]]
            )
        ]
        current_units = [
            unit for unit in new_units
            if any(first <= unit.source_start and unit.source_end <= last for first, last in ranges)
        ]
        current_source = "\n".join(
            f"{number}: {new_prompt_lines[number - 1]}"
            for first, last in ranges
            for number in range(first, last + 1)
        )
        image_units = {unit.placeholder: unit for unit in [*page_units, *new_units]}
        image_context = "\n".join(f"- {placeholder}: {unit.prompt_marker}" for placeholder, unit in image_units.items())
        edits = _edit_blocks(old_prompt_lines, new_prompt_lines, hunks, indexes)
        feedback: list[str] = []
        retry_temperature = getattr(
            getattr(model, "config", None), "retry_temperature", 0.7
        )
        for _attempt in range(attempts):
            prompt = incremental_page_edit_prompt(
                page_title=str(page.get("title") or Path(filename).stem),
                current_page=scrub_base64(prompt_page),
                current_source=scrub_base64(current_source),
                edits=scrub_base64(edits),
                image_context=image_context,
                output_language=str(getattr(settings, "wiki_output_language", "Japanese (日本語)")),
                feedback=feedback,
                reference_only=not owned,
                guidance=(guidance or {}).get(filename, ()),
            )
            try:
                async with semaphore:
                    try:
                        kwargs = {
                            "max_output_tokens": 8000,
                            "temperature": (
                                retry_temperature
                                if feedback
                                else getattr(getattr(model, "config", None), "temperature", 0.7)
                            ),
                        }
                        result = await model.structured(
                            IncrementalPageEditResult, prompt.messages(), **kwargs
                        )
                    except TypeError:
                        result = await model.structured(IncrementalPageEditResult, prompt.messages())
                result = result if isinstance(result, IncrementalPageEditResult) else IncrementalPageEditResult.model_validate(result)
                updated = _apply_model_patches(prompt_page, result, {index + 1 for index in indexes})
                unknown = [token for token in placeholders_in(updated) if f"[[NEO-IMAGE:{token}]]" not in image_units]
                if unknown:
                    raise ValueError(f"unknown image placeholders: {', '.join(unknown)}")
                final, unresolved = restore_images(updated, list(image_units.values()))
                if unresolved:
                    raise ValueError(f"unresolved image placeholders: {', '.join(unresolved)}")
                errors: list[str] = []
                touched_hashes = {unit.unit_sha256 for unit in old_touched_units}
                required = Counter(
                    unit.unit_sha256
                    for unit in page_units
                    if unit.unit_sha256 not in touched_hashes
                )
                if owned:
                    current = Counter(unit.unit_sha256 for unit in current_units)
                    for identity, count in current.items():
                        required[identity] = max(required[identity], count)
                final_counts = image_identity_counts(final)
                labels = {
                    unit.unit_sha256: unit.image_id
                    for unit in [*page_units, *current_units, *old_touched_units]
                }
                checked = set(required) | {
                    unit.unit_sha256 for unit in old_touched_units
                }
                for identity in checked:
                    expected = required[identity]
                    actual = final_counts.get(identity, 0)
                    if actual != expected:
                        errors.append(
                            f"image {labels.get(identity, identity[:12])} count must be "
                            f"{expected}, got {actual}"
                        )
                if owned:
                    for first, last in ranges:
                        errors.extend(check_section(
                            final, lines=new_lines, source_text="",
                            block_ranges=verbatim_blocks(new_lines, first, last),
                            placeholders=[], facts=[], check_identifiers=False,
                        ))
                changed_source = "\n".join(
                    line
                    for index in owned
                    for _old_start, _old_len, new_start, new_len in [hunks[index]]
                    for line in new_prompt_lines[new_start - 1 : new_start - 1 + new_len]
                )
                missing = sorted(code_tokens(changed_source) - code_tokens(final))
                if missing:
                    errors.append("missing identifiers from edited source: " + ", ".join(missing[:40]))
                if errors:
                    raise ValueError("; ".join(errors))
                return final if final == original else final.rstrip() + "\n"
            except (ValueError, TypeError) as exc:
                feedback = [str(exc)]
        raise RuntimeError(f"model could not apply incremental edits to {filename}: {feedback[-1]}")

    async def edit_all() -> dict[str, Any]:
        names = sorted(decision.patch)
        outcomes = await asyncio.gather(
            *(edit_page(name, set(decision.patch[name])) for name in names), return_exceptions=True
        )
        return dict(zip(names, outcomes))

    outcomes = run_async_blocking(edit_all())
    for outcome in outcomes.values():
        if isinstance(outcome, _EditCancelled) or (
            isinstance(outcome, BaseException) and not isinstance(outcome, Exception)
        ):
            raise outcome
    changed: set[str] = set()
    failed: dict[str, str] = {}
    for filename, outcome in outcomes.items():
        if isinstance(outcome, Exception):
            failed[filename] = f"{type(outcome).__name__}: {outcome}"[:500]
            continue
        path = state_root / "wiki" / filename
        if outcome == path.read_text(encoding="utf-8"):
            continue
        write_text_atomic(path, outcome)
        state_path = state_root / "state" / "pages" / f"{int(pages[filename]['number']):03d}.json"
        state = read_json(state_path, default={})
        if state:
            state["content_sha256"] = sha256_text(outcome)
            write_json_atomic(state_path, state)
        changed.add(filename)
    return changed, failed
@dataclass(frozen=True)
class WriteResult:
    target: Path
    touched: list[str]
    rebuild: str = "full"
    changed_pages: list[str] = field(default_factory=list)
    tier: int = 3
    reason: str = ""
    regenerated_pages: list[str] = field(default_factory=list)
    human_edits_overwritten: list[str] = field(default_factory=list)
def wiki_config(settings: Any, *, run_dir: Path, resume: bool = True, source_kind: str = "md", require_resume: bool = False,
                human_guidance: dict[str, list[str]] | None = None):
    from graph.wiki.config import WikiConfig
    from common.policy import policy_of

    concurrency = max(1, int(getattr(settings, "concurrency", app_concurrency())))
    policy = policy_of(settings)
    fields = dict(
        policy=policy.name,
        skip_excel_and_small=bool(getattr(settings, "skip_excel_and_small", False)),
        chat_base_url=settings.chat_base_url,
        chat_api_key=settings.chat_api_key,
        chat_model=settings.chat_model,
        writer_base_url=str(getattr(settings, "writer_base_url", "") or ""),
        writer_api_key=str(getattr(settings, "writer_api_key", "") or ""),
        writer_model=str(getattr(settings, "writer_model_name", "") or ""),
        judge_base_url=str(getattr(settings, "judge_base_url", "") or ""),
        judge_api_key=str(getattr(settings, "judge_api_key", "") or ""),
        judge_model=str(getattr(settings, "judge_model_name", "") or ""),
        temperature=0.7,
        output_language=getattr(settings, "wiki_output_language", "Japanese (日本語)"),
        section_target_lines=int(getattr(settings, "wiki_section_target_lines", 80)),
        write_attempts=int(getattr(settings, "wiki_write_attempts", 3)),
        planner_concurrency=int(getattr(settings, "wiki_planner_concurrency", concurrency)),
        rewrite_concurrency=int(
            getattr(settings, "wiki_rewrite_concurrency", concurrency)
        ),
        request_timeout=int(getattr(settings, "wiki_request_timeout", 300)),
        run_dir=str(run_dir),
        plan_cache_dir=(
            str(Path(settings.cache_dir) / "planner")
            if str(getattr(settings, "cache_dir", "") or "")
            else ""
        ),
        resume=resume,
        require_resume=require_resume,
        source_kind=source_kind,
        structure_target_lines=int(getattr(settings, "structure_target_lines", 250)),
        structure_min_lines=int(getattr(settings, "structure_min_lines", 40)),
        slide_delimiter=getattr(settings, "slide_delimiter", r"^## Slide (\d+)\s*$"),
        slide_title=getattr(settings, "slide_title", r"^### (.+?)\s*$"),
        pdf_use_headings=bool(getattr(settings, "pdf_use_headings", False)),
        human_guidance=dict(human_guidance or {}),
        tabular_slice_records=int(getattr(settings, "tabular_slice_records", 40)),
        tabular_preview_rows=int(getattr(settings, "tabular_preview_rows", 12)),
        tabular_preview_cols=int(getattr(settings, "tabular_preview_cols", 12)),
    )
    fields.update(policy.config_overrides())
    return WikiConfig(**fields)


def run_wiki(
    source_path: Path,
    *,
    run_dir: Path,
    settings: Any,
    llm: Any | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    resume: bool = True,
    source_kind: str = "md",
    require_resume: bool = False,
    human_guidance: dict[str, list[str]] | None = None,
) -> Path:
    from graph.common.async_tools import run_async_blocking
    from graph.wiki.pipeline import run_pipeline
    from graph.wiki.model import ChatModelPort

    config = wiki_config(settings, run_dir=run_dir, resume=resume, source_kind=source_kind,
                         require_resume=require_resume, human_guidance=human_guidance)
    model = llm if hasattr(llm, "structured") and hasattr(llm, "text") else ChatModelPort(config, llm=llm) if llm is not None else None
    return run_async_blocking(
        run_pipeline(
            source_path,
            config=config,
            model=model,
            on_progress=on_progress,
            stop_check=stop_check,
        )
    )


def build_wiki_output(
    *,
    source_path: Path,
    document_name: str,
    out_dir: Path,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    state_dir: Path | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    source_kind: str | None = None,
    resume: bool = True,
    require_resume: bool = False,
    human_guidance: dict[str, list[str]] | None = None,
) -> SimpleNamespace:
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    from graph.formats import is_tabular, kind_of

    if mode not in {"wiki", "chunks"}:
        raise ValueError("mode must be wiki or chunks")
    kind = source_kind or kind_of(document_name)
    if mode == "chunks" and llm is None:
        # Standalone phase calls may not have a prebuilt publisher model. The
        # standard hook still returns None, preserving the legacy error path;
        # FastPolicy supplies the gated writer/judge pair.
        from common.policy import policy_of

        config = wiki_config(
            settings,
            run_dir=state_dir or (out_dir / "wiki-state"),
            resume=resume,
            source_kind=kind,
        )
        llm = policy_of(config).model_if_missing(config)
    if is_tabular(kind):
        from graph.common.async_tools import run_async_blocking
        from graph.formats import csv as csv_format, xlsx as xlsx_format
        from common.policy import policy_of
        from graph.wiki.model import ChatModelPort

        config = wiki_config(settings, run_dir=state_dir or (out_dir / "wiki-state"), resume=resume, source_kind=kind)
        runner = xlsx_format.run if kind == "xlsx" else csv_format.run
        model = (
            llm
            if hasattr(llm, "structured") and hasattr(llm, "text")
            else policy_of(config).model_port(config)
            if llm is None
            else ChatModelPort(config, llm=llm)
        )
        run_async_blocking(runner(source_path, run_dir=out_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check))
        return SimpleNamespace(out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md"))))
    if mode == "wiki":
        from graph.wiki.export import export_ingest_layout

        run_root = run_wiki(
            source_path,
            run_dir=state_dir or (out_dir / "wiki-state"),
            settings=settings,
            llm=llm,
            on_progress=on_progress,
            stop_check=stop_check,
            source_kind=kind,
            resume=resume,
            require_resume=require_resume,
            human_guidance=human_guidance,
        )
        export_ingest_layout(run_root, out_dir, document_name=document_name)
        return SimpleNamespace(
            out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md")))
        )

    body = source_path.read_text(encoding="utf-8")
    from graph.wiki.legacy import run_chunk_pipeline
    from graph.wiki.model import ModelPair

    return run_chunk_pipeline(
        source_text=body,
        document_name=document_name,
        out_dir=out_dir,
        # Keep the historical raw client for standard/custom ports.  A fast
        # ModelPair is the one case that must stay wrapped so its writer gate
        # is not bypassed.
        llm=llm if isinstance(llm, ModelPair) else getattr(llm, "llm", llm),
        concurrency=max(
            1,
            int(
                getattr(
                    settings,
                    "ingest_concurrency",
                    getattr(settings, "concurrency", app_concurrency()),
                )
            ),
        ),
        on_progress=on_progress,
        stop_check=stop_check,
    )


def publish_output(staged: Path, target: Path) -> int:
    """Publish ``docs/*.md`` flat beside ``_planning/``."""

    staged = Path(staged)
    target = Path(target)
    preserved: dict[str, bytes] = {}
    old_planning = target / "_planning"
    for name in ("chunks.json", "links.json", "linker.json", "navigation.json"):
        path = old_planning / name
        if path.exists():
            preserved[name] = path.read_bytes()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    count = 0
    for page in sorted((staged / "docs").glob("*.md")):
        shutil.copyfile(page, target / page.name)
        count += 1
    if (staged / "_planning").exists():
        shutil.copytree(staged / "_planning", target / "_planning")
    if preserved:
        (target / "_planning").mkdir(parents=True, exist_ok=True)
        for name, payload in preserved.items():
            (target / "_planning" / name).write_bytes(payload)
    return count


def _plan_filenames(state_root: Path) -> set[str]:
    from graph.wiki.storage import read_json

    return {str(page["filename"]) for page in read_json(Path(state_root) / "state" / "plan.json", default={}).get("pages", [])}


def _wiki_run_complete(state_root: Path) -> bool:
    """Return whether every page required by the stored plan is publishable."""

    state_root = Path(state_root)
    names = _plan_filenames(state_root)
    return bool(names) and all(
        (state_root / "wiki" / name).is_file() for name in names
    )


def _page_hashes(folder: Path, names: set[str]) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(folder).glob("*.md"))
        if path.name in names
    }


def _human_edited(state_root: Path) -> list[str]:
    from graph.wiki.storage import read_json

    names = []
    for path in sorted((Path(state_root) / "state" / "pages").glob("*.json")):
        state = read_json(path, default={})
        if state.get("human_edited") and state.get("filename"):
            names.append(str(state["filename"]))
    return names

def write_wiki_pages(
    project: Any,
    rel: str,
    *,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    resume: bool = True,
    identity_seed: str | None = None,
) -> WriteResult:
    from graph.formats import is_tabular, kind_of, supports_page_updates
    from graph.formats.xlsx import decide_update as decide_workbook
    from graph.wiki.export import export_ingest_layout
    from graph.wiki.incremental import FULL_MIN_REGEN_SHARE, UpdateDecision, apply_update, decide_update, drop_pages
    from graph.wiki.pipeline import ResumeUnavailable
    from graph.wiki.page import is_small_document
    from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic

    kind = kind_of(rel)
    state_root = Path(project.state_dir(rel))
    old_source = state_root / "source" / "original.md"
    new_text = project.raw_file(rel).read_text(encoding="utf-8")
    old_text = ""
    workbook = None
    stored_run = read_json(state_root / "run.json", default={})
    requested_policy = str(getattr(settings, "policy", "standard"))
    stored_policy = str(stored_run.get("policy") or "standard")
    shortcut = bool(getattr(settings, "skip_excel_and_small", False))
    generation_mode = (
        "sheets-only" if shortcut and is_tabular(kind)
        else "single-page" if shortcut and mode == "wiki" and is_small_document(new_text)
        else "normal"
    )
    plan_path = state_root / "state" / "plan.json"
    stored_mode = stored_run.get("generation_mode") or (
        "single-page" if read_json(plan_path, default={}).get("single_page_rewrite") else "normal"
    )
    if resume and (stored_run or plan_path.exists()) and stored_mode != generation_mode:
        decision = UpdateDecision(tier=3, reason="generation-mode-switch")
    elif resume and old_source.exists() and stored_policy != requested_policy:
        # A changed policy must not reuse an incompatible page draft. This is
        # lazy and document-scoped; unchanged documents never reach this code.
        decision = UpdateDecision(tier=3, reason="policy-switch")
    elif not resume:
        decision = UpdateDecision(tier=3, reason="forced")
    elif mode == "wiki" and kind == "xlsx" and not shortcut and (workbook := decide_workbook(state_root, new_text, project.wiki_dir(rel))):
        decision = workbook[0]
    elif mode != "wiki" or not supports_page_updates(kind):
        decision = UpdateDecision(tier=3, reason="format-full-only")
    elif not old_source.exists() or not (state_root / "state" / "plan.json").exists():
        decision = UpdateDecision(tier=3, reason="no-previous-state")
    else:
        old_text = old_source.read_text(encoding="utf-8")
        if not _wiki_run_complete(state_root):
            decision = UpdateDecision(
                tier=3,
                reason=(
                    "interrupted-initial-build"
                    if old_text == new_text
                    else "incomplete-previous-state"
                ),
            )
        else:
            decision = decide_update(
                state_root, old_text, new_text, kind=kind,
                structure_target_lines=int(getattr(settings, "structure_target_lines", 250)),
                structure_min_lines=int(getattr(settings, "structure_min_lines", 40)),
                pdf_use_headings=bool(getattr(settings, "pdf_use_headings", False)),
            )

    wiki_document = Path(project.wiki_dir(rel)).relative_to(project.wiki)
    # Old reverse-sync versions put human text into generator state; a rebuild would drop it.
    if _human_edited(state_root):
        raise ValueError("legacy human state")
    from publisher.human_changes import HumanStore, LlmHumanModel, apply_generated

    guidance = HumanStore(project).guidance(rel)

    def decision_event() -> dict[str, Any]:
        pages = set(decision.patch) | decision.regenerate | set(decision.retitle)
        return {
            "stage": "wiki", "step": "update_decision", "file": rel,
            **decision.summary(),
            "changed_pages": sorted((wiki_document / name).as_posix() for name in pages),
        }

    if on_progress:
        on_progress(decision_event())
    # A stopped first build has already written its source snapshot and may
    # contain expensive observation/planning/rewrite checkpoints even though
    # plan.json does not exist yet.  That is different from an ordinary full
    # rebuild: keep the run directory and let run_pipeline validate/reuse its
    # input-keyed checkpoints.  Previously the tier-3 path deleted state_root
    # here, so queue --continue restarted an interrupted initial build from
    # zero before the wiki pipeline ever saw resume=True.
    resume_initial_build = (
        resume
        and decision.reason in {"no-previous-state", "interrupted-initial-build"}
        and old_source.exists()
        and old_source.read_text(encoding="utf-8") == new_text
    )
    names = _plan_filenames(state_root)
    before = _page_hashes(state_root / "wiki", names)
    human: list[str] = []
    work = project.work_dir(rel)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        out_dir: Path | None = None
        if workbook is not None and decision.tier in (0, 2):
            # A workbook keeps state only for its 解説 run: sheet pages are re-rendered and
            # the run resumes every 解説 page except the dropped ones. Unchanged: keep all.
            if decision.reason != "unchanged":
                human = apply_update(state_root / "excel-story", decision, workbook[1])
                out_dir = build_wiki_output(
                    source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                    mode=mode, settings=settings, llm=llm, embedder=embedder,
                    state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                    source_kind=kind, resume=True, human_guidance=guidance,
                ).out_dir
        elif decision.tier in (0, 1, 2):
            human = apply_update(state_root, decision, new_text)
            write_text_atomic(old_source, new_text)
            failed: dict[str, str] = {}
            if decision.patch:
                _changed, failed = _apply_incremental_edits(
                    state_root, old_text, new_text, decision,
                    settings=settings, llm=llm, stop_check=stop_check, guidance=guidance,
                )
            if failed:
                plan_pages = read_json(state_root / "state" / "plan.json")["pages"]
                human += drop_pages(state_root, plan_pages, set(failed), research=set())
                decision.regenerate |= set(failed)
                decision.tier, decision.reason = 2, "patch-escalated"
                if on_progress:
                    on_progress({"stage": "wiki", "step": "patch_escalated", "file": rel, "pages": sorted(failed), "errors": failed})
            if len(decision.regenerate) > FULL_MIN_REGEN_SHARE * max(len(names), 1):
                decision.tier, decision.reason = 3, "escalated-most-pages"
            elif decision.regenerate:
                try:
                    out_dir = build_wiki_output(
                        source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                        mode=mode, settings=settings, llm=llm, embedder=embedder,
                        state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                        source_kind=kind, resume=True, require_resume=True, human_guidance=guidance,
                    ).out_dir
                except ResumeUnavailable:
                    decision = UpdateDecision(tier=3, reason="resume-failed")
            else:
                out_dir = export_ingest_layout(state_root, work / "out", document_name=rel)
        if decision.tier == 3:
            if decision.reason not in {
                "forced", "format-full-only", "no-previous-state",
                "interrupted-initial-build", "incomplete-previous-state",
            } and on_progress:
                on_progress(decision_event())
            human = sorted(set(human) | set(_human_edited(state_root)))
            if not resume_initial_build:
                shutil.rmtree(state_root, ignore_errors=True)
            out_dir = build_wiki_output(
                source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                mode=mode, settings=settings, llm=llm, embedder=embedder,
                state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                source_kind=kind, resume=resume_initial_build, human_guidance=guidance,
            ).out_dir
        # A rebuilt workbook republishes and relinks as a whole (the linker's caches make
        # unchanged sheets cheap); only its 解説 regeneration is incremental.
        rebuild = "full" if decision.tier == 3 or (workbook is not None and out_dir is not None) else "incremental"
        after = _page_hashes(state_root / "wiki", _plan_filenames(state_root)) if rebuild == "incremental" else {}
        changed_output_pages = {name for name, digest in after.items() if before.get(name) != digest}
        target = project.wiki_dir(rel)
        if out_dir is not None:
            publish_output(out_dir, target)
        from common.policy import STANDARD, resolve_policy

        if (resolve_policy(requested_policy) is not STANDARD or "policy" in stored_run
                or generation_mode != "normal" or "generation_mode" in stored_run):
            # Also restamp a variant-built document rebuilt as standard; a document
            # only ever built as standard gets no new file.
            run_state = read_json(state_root / "run.json", default={})
            run_state["policy"] = requested_policy
            run_state["policy_version"] = resolve_policy(requested_policy).version
            if generation_mode != "normal" or "generation_mode" in stored_run:
                run_state["generation_mode"] = generation_mode
            write_json_atomic(state_root / "run.json", run_state)
        write_source_stamp(target, project.raw_file(rel), rel, identity_seed=identity_seed)
        if out_dir is not None:
            overlay = apply_generated(project, rel, model=LlmHumanModel(settings, project))
            changed_output_pages.update(Path(path).name for path in overlay.changed_pages)
        marker = target / "_planning" / "linker.json"
        status = "pending" if getattr(settings, "wiki_linker_enabled", True) else "disabled"
        previous_linker = read_json(marker, default={})
        mode_name = str(getattr(settings, "wiki_linker_mode", "legacy"))
        marker_data = {"schema_version": 2, "status": status, "mode": mode_name}
        keep_linker = (
            rebuild == "incremental"
            and previous_linker.get("status") == "complete"
            and previous_linker.get("mode") == mode_name
            and not changed_output_pages
        )
        if status == "pending" and keep_linker:
            marker_data = previous_linker
        elif rebuild == "incremental" and previous_linker.get("status") == "complete" and previous_linker.get("mode") == mode_name:
            marker_data["resume"] = True
        write_json_atomic(marker, marker_data)
        document = target.relative_to(project.wiki)
        return WriteResult(
            target=target,
            touched=[],
            rebuild=rebuild,
            changed_pages=sorted((document / name).as_posix() for name in changed_output_pages),
            tier=decision.tier,
            reason=decision.reason,
            regenerated_pages=sorted((document / name).as_posix() for name in decision.regenerate) if rebuild == "incremental" else [],
            human_edits_overwritten=[],
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
def write_wiki(
    project: Any, rel: str, *, mode: str, settings: Any, llm: Any, embedder: Any,
    on_progress: Progress = None, stop_check: StopCheck = None,
) -> WriteResult:
    """Compatibility wrapper: generate one wiki and immediately link it."""
    result = write_wiki_pages(
        project, rel, mode=mode, settings=settings, llm=llm, embedder=embedder,
        on_progress=on_progress, stop_check=stop_check,
    )
    touched = run_linkers(
        project, [rel], settings=settings, llm=llm, embedder=embedder,
        on_progress=on_progress, stop_check=stop_check,
    )
    return WriteResult(target=result.target, touched=touched)


def run_linker(
    project: Any,
    rel: str,
    *,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> list[str]:
    """Run the sole cross-document linker after pristine pages are published."""

    from graph.wiki.storage import write_json_atomic

    marker_path = project.wiki_dir(rel) / "_planning" / "linker.json"
    if not getattr(settings, "wiki_linker_enabled", True):
        # disabled mode constructs no services and never opens the catalog
        write_json_atomic(marker_path, {"schema_version": 2, "status": "disabled"})
        return []

    from graph.common.async_tools import run_async_blocking
    from common.policy import policy_of
    from graph.wiki.model import ChatModelPort

    if hasattr(llm, "structured"):
        model = llm  # already a model port (the linker only needs structured()); no services to construct
    else:
        config = wiki_config(settings, run_dir=project.state_dir(rel))
        model = (
            policy_of(config).model_port(config)
            if llm is None
            else ChatModelPort(config, llm=llm)
        )
    if embedder is None:
        try:
            from graph.clients.embeddings import Embedder

            embedder = Embedder(settings)
        except Exception as exc:
            embedder = None
            if on_progress:
                on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
    from graph.linker import link_document
    result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check))
    return result.touched_documents


run_wiki_linker = run_linker


def run_linkers(
    project: Any, rels: list[str], *, settings: Any, llm: Any, embedder: Any,
    on_progress: Progress = None, stop_check: StopCheck = None,
    affected_pages: set[str] | None = None,
    regenerated_pages: set[str] | None = None,
) -> list[str]:
    """Link a completed wiki batch and render affected pages once."""
    if not rels:
        return []
    from graph.wiki.storage import write_json_atomic
    if not getattr(settings, "wiki_linker_enabled", True):
        for rel in rels:
            write_json_atomic(project.wiki_dir(rel) / "_planning" / "linker.json", {"schema_version": 2, "status": "disabled"})
        return []
    from graph.common.async_tools import run_async_blocking
    from graph.linker import link_documents
    from common.policy import policy_of
    from graph.wiki.model import ChatModelPort
    if hasattr(llm, "structured"):
        model = llm
    else:
        config = wiki_config(settings, run_dir=project.metadata / "state" / "linker")
        model = (
            policy_of(config).model_if_missing(config)
            if llm is None
            else ChatModelPort(config, llm=llm)
        )
    changed_pages = set(affected_pages) if affected_pages else None
    result = run_async_blocking(link_documents(
        project, rels, model=model, embedder=embedder, settings=settings,
        on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages,
        regenerated_pages=set(regenerated_pages) if regenerated_pages else None,
    ))
    if affected_pages is not None:
        affected_pages.update(result.affected_pages or [])
    return result.touched_documents


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_source_stamp(target: Path, raw_file: Path, rel: str, *, identity_seed: str | None = None) -> None:
    planning = Path(target) / "_planning"
    planning.mkdir(exist_ok=True)
    tmp = planning / "source.json.tmp"
    tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file), "id_seed": identity_seed or rel}), encoding="utf-8")
    tmp.replace(planning / "source.json")


def wiki_up_to_date(project: Any, rel: str) -> bool:
    planning = project.wiki_dir(rel) / "_planning"
    raw = project.raw_file(rel)
    current = _sha256_file(raw) if raw.exists() else ""
    for name, key in (("source.json", "sha256"), ("manifest.json", "source_sha256")):
        path = planning / name
        if not path.exists():
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get(key) != current:
                return False
        except (OSError, ValueError):
            return False
        return True
    return False


def links_up_to_date(project: Any, rel: str, *, mode: str | None = None) -> bool:
    marker = project.wiki_dir(rel) / "_planning" / "linker.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("status") not in ("complete", "disabled"):
        return False
    return mode is None or (data.get("status") == "complete" and data.get("mode") == mode)


def up_to_date(project: Any, rel: str) -> bool:
    return wiki_up_to_date(project, rel) and links_up_to_date(project, rel)


def write_index(project: Any) -> None:
    lines = ["# Wiki", ""]
    for planning in sorted(project.wiki.rglob("_planning/metadata.json")):
        folder = planning.parent.parent
        rel = folder.relative_to(project.wiki).as_posix()
        pages = sorted(page.name for page in folder.glob("*.md"))
        lines.append(f"## {rel}")
        lines.extend(f"- [{page[:-3]}]({rel}/{page})" for page in pages)
        lines.append("")
    project.wiki.mkdir(parents=True, exist_ok=True)
    (project.wiki / "index.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )
