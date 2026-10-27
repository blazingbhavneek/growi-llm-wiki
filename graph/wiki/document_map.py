"""Turn overlapping observations into one exact sequential seed plan.

The work is intentionally narrow at each model call:

1. at most ten window inventories become one provisional regional map;
2. one semantic call reasons about the compact regional maps;
3. one compiler emits only title/summary/chapter/start/end fields;
4. deterministic validation feeds exact errors back to the compiler.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Sequence

from common.policy import policy_of

from .config import SEED_PLAN_COMPILE_VERSION, SEED_PLAN_VERSION, WikiConfig
from .markdown_blocks import BlockIndex, build_block_index
from .page import split_sections
from .prompts import (
    regional_plan_prompt,
    seed_plan_compile_prompt,
    semantic_plan_prompt,
)
from .schemas import CompiledSeedPlan, ObservationSet, RegionalReport, WindowReport
from .storage import clean_workdir, hash_of, read_json, write_json_atomic, write_text_atomic
from .wire import RegionalPage, RegionalPlan, SeedPlan, SeedRange, SemanticPlan

StopCheck = Callable[[], bool] | None


class SeedPlanningError(RuntimeError):
    """The final range table does not own every source line exactly once."""


MIN_SEED_LINES = 1


def _merge_seed_ranges(pages: Sequence[SeedRange]) -> SeedRange:
    """Collapse ranges that cannot be separated without cutting an atomic block."""

    def distinct(values: Sequence[str], separator: str) -> str:
        return separator.join(dict.fromkeys(value.strip() for value in values if value.strip()))

    title = distinct([page.title for page in pages], "・")
    paths = [page.path for page in pages if page.path]
    common_path: list[str] = []
    if paths:
        for values in zip(*paths):
            if len(set(values)) != 1:
                break
            common_path.append(values[0])
    if common_path and common_path[-1] not in title:
        common_path.append(title)
    elif not common_path and paths:
        common_path = [title]
    return SeedRange(
        title=title,
        summary=distinct([page.summary for page in pages], " "),
        chapter=distinct([page.chapter for page in pages], " / "),
        source_start=pages[0].source_start,
        source_end=pages[-1].source_end,
        path=common_path,
    )


def _window_reports_text(reports: Sequence[WindowReport]) -> str:
    output: list[str] = []
    for report in reports:
        output.extend(
            [
                f"### Window {report.ordinal}: {report.source_start}-{report.source_end}",
                report.summary or "（要約なし）",
            ]
        )
        for item in report.observations:
            flags = []
            if item.repeated_format:
                flags.append("反復エンティティ")
            if item.continues_before:
                flags.append("前から継続")
            if item.continues_after:
                flags.append("後へ継続")
            output.append(
                "- "
                f"{item.source_start}-{item.source_end} | {item.kind or '未分類'} | "
                f"{item.title} | 親={item.parent or '-'} | "
                f"列挙群={item.enumeration_family or '-'} | "
                f"状態={','.join(flags) or '-'} | {item.summary or '-'}"
            )
        output.append("")
    return "\n".join(output).rstrip()


def _regional_reports_text(reports: Sequence[RegionalReport]) -> str:
    output: list[str] = []
    for report in reports:
        output.extend(
            [
                f"### Region {report.ordinal}: {report.source_start}-{report.source_end}",
                report.summary or "（要約なし）",
            ]
        )
        for page in report.pages:
            flags = []
            if page.continues_before:
                flags.append("前から継続")
            if page.continues_after:
                flags.append("後へ継続")
            output.append(
                "- "
                f"{page.source_start}-{page.source_end} | {page.title} | "
                f"種別={page.entity_kind or '-'} | 列挙群={page.enumeration_family or '-'} | "
                f"状態={','.join(flags) or '-'} | {page.scope or '-'}"
            )
        output.append("")
    return "\n".join(output).rstrip()


def validate_regional_plan(
    plan: RegionalPlan,
    *,
    source_start: int,
    source_end: int,
) -> tuple[RegionalPlan | None, str | None]:
    pages: list[RegionalPage] = []
    for position, raw in enumerate(plan.pages, start=1):
        page = RegionalPage.model_validate(raw)
        if not page.title.strip():
            return None, f"regional page #{position} has no title"
        if not page.scope.strip():
            return None, f"regional page #{position} ({page.title}) has no scope summary"
        if not (
            source_start <= page.source_start <= page.source_end <= source_end
        ):
            return None, (
                f"regional page #{position} range {page.source_start}-{page.source_end} "
                f"must stay inside regional evidence {source_start}-{source_end}"
            )
        pages.append(page)
    if not pages:
        return None, "regional plan contains no page candidates"
    pages.sort(key=lambda item: (item.source_start, item.source_end, item.title))
    return RegionalPlan(summary=plan.summary.strip(), pages=pages), None


def validate_seed_plan(
    plan: SeedPlan | CompiledSeedPlan,
    *,
    source_line_count: int,
    block_index: BlockIndex,
    lines: Sequence[str] = (),
    page_target_lines: int = 0,
) -> tuple[CompiledSeedPlan | None, str | None]:
    """Require one ordered, contiguous, non-overlapping partition of ``1..N``."""

    pages = [SeedRange.model_validate(item) for item in plan.pages]
    if source_line_count == 0:
        if pages:
            return None, "empty source must have no seed pages"
        return CompiledSeedPlan(summary=plan.summary, pages=[]), None
    if not pages:
        return None, "the plan contains no seed pages"

    expected = 1
    cleaned: list[SeedRange] = []
    for position, page in enumerate(pages, start=1):
        if not page.title.strip():
            return None, f"page #{position} has no title"
        if not page.summary.strip():
            return None, f"page #{position} ({page.title}) has no summary"
        if page.source_start != expected:
            relation = "gap" if page.source_start > expected else "overlap"
            return None, (
                f"line ownership {relation} before page #{position} ({page.title}): "
                f"source_start must be {expected}, got {page.source_start}. "
                "Return the complete plan again; no line may leak between seeds."
            )
        if page.source_end < page.source_start:
            return None, f"page #{position} has an inverted range"
        if page.source_end > source_line_count:
            return None, (
                f"page #{position} ends at {page.source_end}, but the source ends at "
                f"{source_line_count}"
            )
        cleaned.append(
            SeedRange(
                title=page.title.strip(),
                summary=page.summary.strip(),
                chapter=page.chapter.strip(),
                source_start=page.source_start,
                source_end=page.source_end,
                path=list(page.path),
            )
        )
        expected = page.source_end + 1

    if expected != source_line_count + 1:
        return None, (
            f"line ownership stops at {expected - 1}; final page must end at "
            f"{source_line_count}. Return the complete plan again."
        )
    cuts = [1, *(page.source_start for page in cleaned[1:]), source_line_count + 1]
    snapped = list(cuts)
    for index in range(1, len(cuts) - 1):
        cut = cuts[index]
        block = block_index.block_at_cut(cut)
        if block is None:
            continue
        lower = snapped[index - 1] + 1
        upper = cuts[index + 1] - 1
        choices = [
            candidate
            for candidate in (block.start, block.end + 1)
            if lower <= candidate <= upper and block_index.cut_is_safe(candidate)
        ]
        if not choices:
            overlapping = [
                position
                for position, page in enumerate(cleaned, start=1)
                if page.source_start <= block.end and page.source_end >= block.start
            ]
            first = overlapping[0]
            last = overlapping[-1]
            merged_start = cleaned[first - 1].source_start
            merged_end = cleaned[last - 1].source_end
            merged = _merge_seed_ranges(cleaned[first - 1:last])
            assert merged.source_start == merged_start and merged.source_end == merged_end
            return validate_seed_plan(
                SeedPlan(
                    summary=plan.summary,
                    pages=[*cleaned[:first - 1], merged, *cleaned[last:]],
                ),
                source_line_count=source_line_count,
                block_index=block_index,
                lines=lines,
                page_target_lines=page_target_lines,
            )
        snapped[index] = min(choices, key=lambda candidate: (abs(candidate - cut), candidate))

    repaired = [
        page.model_copy(
            update={"source_start": snapped[index], "source_end": snapped[index + 1] - 1}
        )
        for index, page in enumerate(cleaned)
    ]
    for position, page in enumerate(repaired[1:], start=2):
        if not block_index.cut_is_safe(page.source_start):
            block = block_index.block_at_cut(page.source_start)
            return None, (
                f"boundary before line {page.source_start} still cuts inside "
                f"{block.kind if block else 'an atomic block'} "
                f"{block.start if block else '?'}-{block.end if block else '?'}"
            )
        if page.source_start > page.source_end:
            return None, f"page #{position} became empty while preserving an atomic block"
    if source_line_count >= MIN_SEED_LINES:
        for position, page in enumerate(repaired, start=1):
            line_count = page.source_end - page.source_start + 1
            if line_count < MIN_SEED_LINES:
                return None, (
                    f"page #{position} ({page.title}) owns only {line_count} source lines; "
                    f"every seed must own at least {MIN_SEED_LINES}. Merge this whole topic "
                    "with the semantically closer previous or next topic after comparing "
                    "both neighbors, then return the complete plan. "
                    "Do not split an entity or move only filler lines to satisfy the limit."
                )
    if lines and page_target_lines > 0:
        # ponytail: the planner is only asked for ~target lines; Python cuts oversize
        # pages at headings. Ask the model to title the parts if "（i/n）" reads badly.
        sized: list[SeedRange] = []
        for page in repaired:
            parts = (
                split_sections(
                    lines, page.source_start, page.source_end,
                    target=2 * page_target_lines, min_lines=page_target_lines // 2,
                )
                if page.source_end - page.source_start + 1 > 2 * page_target_lines
                else []
            )
            if len(parts) < 2:
                sized.append(page)
                continue
            sized.extend(
                page.model_copy(update={
                    "title": f"{page.title}（{index}/{len(parts)}）",
                    "source_start": part_start,
                    "source_end": part_end,
                })
                for index, (part_start, part_end) in enumerate(parts, start=1)
            )
        repaired = sized
    return CompiledSeedPlan(summary=plan.summary.strip(), pages=repaired), None


def _boundary_repairs(
    original: SeedPlan | CompiledSeedPlan,
    checked: CompiledSeedPlan,
) -> list[dict[str, int | str]]:
    repairs: list[dict[str, int | str]] = []
    if len(original.pages) != len(checked.pages):
        return [{"title": "*", "note": f"{len(original.pages)} planned pages compiled into {len(checked.pages)}"}]
    for before, after in zip(original.pages, checked.pages):
        if (
            before.source_start == after.source_start
            and before.source_end == after.source_end
        ):
            continue
        repairs.append(
            {
                "title": after.title,
                "from_start": before.source_start,
                "from_end": before.source_end,
                "to_start": after.source_start,
                "to_end": after.source_end,
            }
        )
    return repairs


def _regional_fallback(
    reports: Sequence[WindowReport],
    *,
    ordinal: int,
) -> RegionalReport:
    start = reports[0].source_start
    end = reports[-1].source_end
    seen: set[tuple[int, int, str]] = set()
    pages: list[RegionalPage] = []
    for report in reports:
        for item in report.observations:
            key = (item.source_start, item.source_end, item.title.casefold())
            if key in seen:
                continue
            seen.add(key)
            pages.append(
                RegionalPage(
                    title=item.title,
                    scope=item.summary,
                    source_start=item.source_start,
                    source_end=item.source_end,
                    entity_kind=item.kind,
                    enumeration_family=item.enumeration_family,
                    continues_before=item.continues_before,
                    continues_after=item.continues_after,
                )
            )
    if not pages:
        pages.append(
            RegionalPage(
                title=f"原文 {start}-{end}行",
                scope="地域計画を再確認する必要がある。",
                source_start=start,
                source_end=end,
                continues_before=start > 1,
                continues_after=True,
            )
        )
    return RegionalReport(
        ordinal=ordinal,
        source_start=start,
        source_end=end,
        summary="地域モデルが失敗したため、重複観察をそのまま保持した。",
        pages=sorted(pages, key=lambda item: (item.source_start, item.source_end)),
    )


async def _build_regions(
    observations: ObservationSet,
    *,
    model,
    config: WikiConfig,
    checkpoint_root: Path | None,
    stop_check: StopCheck,
    on_progress: Callable[[dict], None] | None,
) -> list[RegionalReport]:
    size = max(1, config.regional_window_count)
    batches = [
        observations.windows[index : index + size]
        for index in range(0, len(observations.windows), size)
    ]
    regions: list[RegionalReport] = []
    for ordinal, reports in enumerate(batches, start=1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("regional seed planning cancelled")
        source_start = reports[0].source_start
        source_end = reports[-1].source_end
        material = _window_reports_text(reports)
        previous = _regional_reports_text(regions[-1:])
        key = hash_of(
            {
                "observation_prompt_version": config.prompt_version,
                "seed_plan_version": SEED_PLAN_VERSION,
                "region": ordinal,
                "reports": [item.model_dump(mode="json") for item in reports],
                "previous": regions[-1].model_dump(mode="json") if regions else None,
            }
        )
        task_root = (
            checkpoint_root / "regions" / f"region-{ordinal:04d}-{key[:12]}"
            if checkpoint_root
            else None
        )
        result_path = task_root / "result.json" if task_root else None
        result: RegionalReport | None = None
        cached = False
        if config.resume and result_path and result_path.exists():
            try:
                candidate = RegionalReport.model_validate(read_json(result_path))
                checked, error = validate_regional_plan(
                    RegionalPlan(summary=candidate.summary, pages=candidate.pages),
                    source_start=source_start,
                    source_end=source_end,
                )
                if error is None and checked is not None:
                    result = candidate
                    cached = True
            except (OSError, TypeError, ValueError):
                pass
        if task_root:
            task_root.mkdir(parents=True, exist_ok=True)

        last_error = ""
        if result is None:
            for attempt in range(1, max(1, config.planner_attempts) + 1):
                prompt = regional_plan_prompt(
                    region_number=ordinal,
                    source_start=source_start,
                    source_end=source_end,
                    window_reports=material,
                    previous_region=previous,
                    output_language=config.output_language,
                    last_error=last_error or None,
                    page_target_lines=config.page_target_lines,
                )
                if task_root:
                    write_text_atomic(task_root / f"attempt-{attempt:02d}-prompt.md", prompt.render())
                try:
                    raw = await model.structured(
                        RegionalPlan,
                        prompt.messages(),
                        max_output_tokens=config.map_max_output_tokens,
                        temperature=config.retry_temperature if last_error else config.temperature,
                    )
                    candidate = raw if isinstance(raw, RegionalPlan) else RegionalPlan.model_validate(raw)
                    if task_root:
                        write_json_atomic(task_root / f"attempt-{attempt:02d}-response.json", candidate)
                    checked, error = validate_regional_plan(
                        candidate,
                        source_start=source_start,
                        source_end=source_end,
                    )
                    if error is None and checked is not None:
                        result = RegionalReport(
                            ordinal=ordinal,
                            source_start=source_start,
                            source_end=source_end,
                            summary=checked.summary,
                            pages=checked.pages,
                        )
                        break
                    last_error = error or "invalid regional plan"
                except Exception as exc:  # noqa: BLE001 - retry includes feedback
                    last_error = f"{type(exc).__name__}: {exc}"[:1000]
                if task_root:
                    write_text_atomic(task_root / f"attempt-{attempt:02d}-error.txt", last_error + "\n")

        if result is None:
            result = _regional_fallback(reports, ordinal=ordinal)
        if result_path:
            write_json_atomic(result_path, result)
        regions.append(result)
        if on_progress:
            on_progress(
                {
                    "stage": "plan",
                    "step": "region",
                    "current": ordinal,
                    "total": len(batches),
                    "source_start": source_start,
                    "source_end": source_end,
                    "pages": len(result.pages),
                    "cached": cached,
                    "fallback": bool(last_error and result.summary.startswith("地域モデル")),
                }
            )
    return regions


def _fallback_semantic_plan(regions: Sequence[RegionalReport]) -> SemanticPlan:
    lines = [
        "地域候補を原文順に照合し、重複観察を統合する。",
        "各行を一つだけの連続シードへ割り当てる。",
        "反復形式の具体的エンティティは一件ずつ独立ページにする。",
    ]
    for region in regions:
        for page in region.pages:
            lines.append(
                f"- {page.source_start}-{page.source_end}: {page.title} — {page.scope}"
            )
    return SemanticPlan(plan="\n".join(lines))


async def _build_semantic_plan(
    regions: Sequence[RegionalReport],
    *,
    source_line_count: int,
    model,
    config: WikiConfig,
    checkpoint_root: Path | None,
    stop_check: StopCheck,
    on_progress: Callable[[dict], None] | None,
) -> SemanticPlan:
    material = _regional_reports_text(regions)
    key = hash_of(
        {
            "observation_prompt_version": config.prompt_version,
            "seed_plan_version": SEED_PLAN_VERSION,
            "regions": [item.model_dump(mode="json") for item in regions],
        }
    )
    task_root = checkpoint_root / "semantic" / key[:12] if checkpoint_root else None
    result_path = task_root / "result.json" if task_root else None
    if config.resume and result_path and result_path.exists():
        try:
            cached = SemanticPlan.model_validate(read_json(result_path))
            if cached.plan.strip():
                if on_progress:
                    on_progress({"stage": "plan", "step": "semantic", "cached": True})
                return cached
        except (OSError, TypeError, ValueError):
            pass
    if task_root:
        task_root.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(1, max(1, config.planner_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("semantic seed planning cancelled")
        prompt = semantic_plan_prompt(
            source_line_count=source_line_count,
            regional_reports=material,
            output_language=config.output_language,
            last_error=last_error or None,
            page_target_lines=config.page_target_lines,
        )
        if task_root:
            write_text_atomic(task_root / f"attempt-{attempt:02d}-prompt.md", prompt.render())
        try:
            raw = await model.structured(
                SemanticPlan,
                prompt.messages(),
                max_output_tokens=config.map_max_output_tokens,
                temperature=config.retry_temperature if last_error else config.temperature,
            )
            result = raw if isinstance(raw, SemanticPlan) else SemanticPlan.model_validate(raw)
            if task_root:
                write_json_atomic(task_root / f"attempt-{attempt:02d}-response.json", result)
            if result.plan.strip():
                if result_path:
                    write_json_atomic(result_path, result)
                if on_progress:
                    on_progress(
                        {"stage": "plan", "step": "semantic", "attempt": attempt}
                    )
                return result
            last_error = "semantic plan is empty"
        except Exception as exc:  # noqa: BLE001 - bounded semantic retry
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
        if task_root:
            write_text_atomic(task_root / f"attempt-{attempt:02d}-error.txt", last_error + "\n")
    result = _fallback_semantic_plan(regions)
    if result_path:
        write_json_atomic(result_path, result)
    if on_progress:
        on_progress(
            {"stage": "plan", "step": "semantic", "fallback": True, "error": last_error}
        )
    return result


async def _compile_seed_plan(
    semantic: SemanticPlan,
    regions: Sequence[RegionalReport],
    *,
    lines: Sequence[str],
    model,
    config: WikiConfig,
    checkpoint_root: Path | None,
    stop_check: StopCheck,
    on_progress: Callable[[dict], None] | None,
) -> CompiledSeedPlan:
    source_line_count = len(lines)
    block_index = build_block_index(list(lines))
    material = _regional_reports_text(regions)
    key = hash_of(
        {
            "observation_prompt_version": config.prompt_version,
            "seed_plan_compile_version": SEED_PLAN_COMPILE_VERSION,
            "semantic": semantic.model_dump(mode="json"),
            "regions": [item.model_dump(mode="json") for item in regions],
            "source_line_count": source_line_count,
        }
    )
    task_root = checkpoint_root / "compile" / key[:12] if checkpoint_root else None
    result_path = task_root / "result.json" if task_root else None
    if config.resume and result_path and result_path.exists():
        try:
            cached = CompiledSeedPlan.model_validate(read_json(result_path))
            checked, error = validate_seed_plan(
                cached,
                source_line_count=source_line_count,
                block_index=block_index,
                lines=lines,
                page_target_lines=config.page_target_lines,
            )
            if error is None and checked is not None:
                if on_progress:
                    on_progress(
                        {
                            "stage": "plan",
                            "step": "compile_done",
                            "cached": True,
                            "pages": len(checked.pages),
                        }
                    )
                return checked
        except (OSError, TypeError, ValueError):
            pass
    if task_root:
        # A failed compile is one resumable stage, not a conversation to continue
        # across process runs. Keep earlier planning stages, but start this stage at 1.
        clean_workdir(task_root)

    last_error = ""
    previous_plan = ""
    repeated_invalid = 0
    attempt = 0
    # Each invocation gets a fresh, short repair conversation for this stage.
    for _ in range(max(1, config.map_attempts)):
        attempt += 1
        if stop_check and stop_check():
            raise asyncio.CancelledError("seed plan compilation cancelled")
        prompt = seed_plan_compile_prompt(
            source_line_count=source_line_count,
            semantic_plan=semantic.plan,
            regional_reports=material,
            output_language=config.output_language,
            last_error=last_error or None,
            previous_plan=previous_plan,
            page_target_lines=config.page_target_lines,
        )
        prompt_path = ""
        response_path = ""
        if task_root:
            prompt_file = task_root / f"attempt-{attempt:04d}-prompt.md"
            write_text_atomic(prompt_file, prompt.render())
            prompt_path = str(prompt_file)
        try:
            raw = await model.structured(
                SeedPlan,
                prompt.messages(),
                max_output_tokens=config.map_max_output_tokens,
                temperature=config.retry_temperature if last_error else config.temperature,
            )
            candidate = raw if isinstance(raw, SeedPlan) else SeedPlan.model_validate(raw)
            candidate_plan = json.dumps(
                candidate.model_dump(mode="json"), indent=2, ensure_ascii=False
            )
            repeated = candidate_plan == previous_plan
            previous_plan = candidate_plan
            if task_root:
                response_file = task_root / f"attempt-{attempt:04d}-response.json"
                write_json_atomic(response_file, candidate)
                response_path = str(response_file)
            checked, error = validate_seed_plan(
                candidate,
                source_line_count=source_line_count,
                block_index=block_index,
                lines=lines,
                page_target_lines=config.page_target_lines,
            )
            if error is None and checked is not None:
                repairs = _boundary_repairs(candidate, checked)
                if result_path:
                    write_json_atomic(result_path, checked)
                    if repairs:
                        write_json_atomic(task_root / "boundary-repairs.json", repairs)
                if on_progress:
                    on_progress(
                        {
                            "stage": "plan",
                            "step": "compile_done",
                            "attempt": attempt,
                            "pages": len(checked.pages),
                            "repairs": len(repairs),
                            "prompt": prompt_path,
                            "response": response_path,
                        }
                    )
                return checked
            last_error = error or "invalid seed plan"
            if repeated:
                repeated_invalid += 1
                last_error += (
                    f" The exact same invalid plan has now been repeated "
                    f"{repeated_invalid} time(s). Do not return it again; apply the exact "
                    "range and page-count correction above."
                )
            else:
                repeated_invalid = 0
        except Exception as exc:  # noqa: BLE001 - retry feedback is the recovery path
            last_error = f"{type(exc).__name__}: {exc}"[:1500]
        if task_root:
            write_text_atomic(task_root / f"attempt-{attempt:04d}-error.txt", last_error + "\n")
        if on_progress:
            on_progress(
                {
                    "stage": "plan",
                    "step": "compile_retry",
                    "attempt": attempt,
                    "error": last_error,
                    "prompt": prompt_path,
                    "response": response_path,
                }
            )
    raise SeedPlanningError(last_error or "seed plan compilation failed")


async def build_seed_plan(
    observations: ObservationSet,
    *,
    lines: Sequence[str],
    model,
    config: WikiConfig,
    checkpoint_dir: Path | str | None = None,
    stop_check: StopCheck = None,
    on_progress: Callable[[dict], None] | None = None,
) -> CompiledSeedPlan:
    """Build regional maps, semantic advice, then the exact seed partition."""

    if not lines:
        return CompiledSeedPlan()
    checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
    if checkpoint_root:
        checkpoint_root.mkdir(parents=True, exist_ok=True)
    regions = await _build_regions(
        observations,
        model=model,
        config=config,
        checkpoint_root=checkpoint_root,
        stop_check=stop_check,
        on_progress=on_progress,
    )
    semantic = await _build_semantic_plan(
        regions,
        source_line_count=len(lines),
        model=model,
        config=config,
        checkpoint_root=checkpoint_root,
        stop_check=stop_check,
        on_progress=on_progress,
    )
    plan = await _compile_seed_plan(
        semantic,
        regions,
        lines=lines,
        model=model,
        config=config,
        checkpoint_root=checkpoint_root,
        stop_check=stop_check,
        on_progress=on_progress,
    )
    return policy_of(config).adjust_seed_plan(plan, config=config)
