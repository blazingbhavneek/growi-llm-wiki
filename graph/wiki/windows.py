"""Observe fixed overlapping source windows without creating semantic chunks.

Windows are transport units only.  Their inventories may overlap and nest;
only the later seed compiler is allowed to assign source-line ownership.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import WikiConfig
from .ids import document_id, window_id
from .images import ImageUnit, extract_image_units, image_records, numbered_prompt_block
from .model import ModelPort
from .prompts import window_inventory_prompt
from .schemas import ObservationSet, WindowReport
from .storage import (
    hash_of,
    normalize_source,
    read_json,
    sha256_text,
    split_source_lines,
    write_json_atomic,
    write_text_atomic,
)
from .wire import ObservedRange, WindowInventory

StopCheck = Callable[[], bool] | None


class ObservationError(RuntimeError):
    """An observation window or its configuration is invalid."""


def overlapping_windows(
    line_count: int,
    *,
    target: int = 250,
    overlap: int = 50,
) -> list[tuple[int, int]]:
    """Return exact fixed windows with ``overlap`` shared source lines."""

    if target <= 0:
        raise ObservationError("window_target_lines must be positive")
    if overlap < 0 or overlap >= target:
        raise ObservationError(
            "window_overlap_lines must be at least zero and smaller than the window"
        )
    if line_count <= 0:
        return []

    stride = target - overlap
    result: list[tuple[int, int]] = []
    start = 1
    while start <= line_count:
        end = min(line_count, start + target - 1)
        result.append((start, end))
        if end == line_count:
            break
        start += stride
    return result


def _clean(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def validate_inventory(
    inventory: WindowInventory,
    *,
    source_start: int,
    source_end: int,
) -> tuple[WindowInventory | None, str | None]:
    """Validate line bounds while deliberately allowing nested/overlapping ranges."""

    observations: list[ObservedRange] = []
    for position, raw in enumerate(inventory.observations, start=1):
        item = ObservedRange.model_validate(raw)
        if not item.title.strip():
            return None, f"observation #{position} has no title"
        if not (
            source_start
            <= item.source_start
            <= item.source_end
            <= source_end
        ):
            return None, (
                f"observation #{position} range {item.source_start}-{item.source_end} "
                f"must stay inside visible window {source_start}-{source_end}"
            )
        observations.append(
            ObservedRange(
                title=_clean(item.title, 200),
                kind=_clean(item.kind, 80),
                summary=_clean(item.summary, 500),
                source_start=item.source_start,
                source_end=item.source_end,
                parent=_clean(item.parent, 200),
                enumeration_family=_clean(item.enumeration_family, 160),
                repeated_format=bool(item.repeated_format),
                continues_before=bool(item.continues_before),
                continues_after=bool(item.continues_after),
            )
        )

    observations.sort(
        key=lambda item: (item.source_start, -item.source_end, item.title)
    )
    summary = _clean(inventory.summary, 800)
    if not summary and observations:
        summary = "; ".join(item.title for item in observations)[:800]
    if not summary and not observations:
        return None, "inventory contains neither a summary nor observations"
    return WindowInventory(summary=summary, observations=observations), None


def _mechanical_inventory(
    source_start: int,
    source_end: int,
    source_line_count: int,
    error: str,
) -> WindowInventory:
    return WindowInventory(
        summary=(
            f"原文{source_start}-{source_end}行。観察モデルが有効な目録を返さなかったため、"
            "最終計画者が原文範囲として再確認する必要がある。"
        ),
        observations=[
            ObservedRange(
                title=f"原文 {source_start}-{source_end}行（要再確認）",
                kind="mechanical-window",
                summary=_clean(error, 300),
                source_start=source_start,
                source_end=source_end,
                continues_before=source_start > 1,
                continues_after=source_end < source_line_count,
            )
        ],
    )


def _window_markdown(report: WindowReport, numbered_source: str, total: int) -> str:
    output = [
        f"# Observation window {report.ordinal}/{total}",
        "",
        f"Source lines: {report.source_start}-{report.source_end}",
        "",
        report.summary,
        "",
        "## Observed ranges",
        "",
    ]
    for item in report.observations:
        flags = []
        if item.continues_before:
            flags.append("continues-before")
        if item.continues_after:
            flags.append("continues-after")
        if item.repeated_format:
            flags.append("repeated-entity")
        suffix = f" ({', '.join(flags)})" if flags else ""
        output.extend(
            [
                f"- **{item.source_start}-{item.source_end}** {item.title}{suffix}",
                f"  - kind: {item.kind or 'unspecified'}",
                f"  - parent: {item.parent or 'none'}",
                f"  - enumeration: {item.enumeration_family or 'none'}",
                f"  - {item.summary or '要約なし'}",
            ]
        )
    output.extend(["", "## Numbered source shown to the model", "", numbered_source, ""])
    return "\n".join(output)


async def _observe_one(
    *,
    ordinal: int,
    total: int,
    source_start: int,
    source_end: int,
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    document: str,
    model: ModelPort,
    config: WikiConfig,
    checkpoint_root: Path | None,
    live_root: Path | None,
    stop_check: StopCheck,
) -> tuple[WindowReport, bool]:
    if stop_check and stop_check():
        raise asyncio.CancelledError("window observation cancelled")

    block = numbered_prompt_block(lines, units, source_start, source_end)
    key = hash_of(
        {
            "prompt_version": config.prompt_version,
            "output_language": config.output_language,
            "model": getattr(model, "name", ""),
            "provider": getattr(model, "provider", ""),
            "source_start": source_start,
            "source_end": source_end,
            "source": sha256_text(block.text),
        }
    )
    task_root = (
        checkpoint_root
        / f"window-{ordinal:06d}-lines-{source_start:06d}-{source_end:06d}-{key[:12]}"
        if checkpoint_root
        else None
    )
    result_path = task_root / "result.json" if task_root else None
    if config.resume and result_path and result_path.exists():
        try:
            cached = WindowReport.model_validate(read_json(result_path))
            checked, error = validate_inventory(
                WindowInventory(
                    summary=cached.summary,
                    observations=cached.observations,
                ),
                source_start=source_start,
                source_end=source_end,
            )
            if error is None and checked is not None:
                return cached, True
        except (OSError, TypeError, ValueError):
            pass

    if task_root:
        task_root.mkdir(parents=True, exist_ok=True)
    last_error = ""
    inventory: WindowInventory | None = None
    for attempt in range(1, max(1, config.planner_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("window observation cancelled")
        prompt = window_inventory_prompt(
            window_start=source_start,
            window_end=source_end,
            block=block,
            output_language=config.output_language,
            last_error=last_error or None,
        )
        if task_root:
            write_text_atomic(task_root / f"attempt-{attempt:02d}-prompt.md", prompt.render())
        try:
            raw = await model.structured(
                WindowInventory,
                prompt.messages(),
                max_output_tokens=config.planner_max_output_tokens,
                temperature=config.retry_temperature if last_error else config.temperature,
            )
            candidate = (
                raw
                if isinstance(raw, WindowInventory)
                else WindowInventory.model_validate(raw)
            )
            if task_root:
                write_json_atomic(task_root / f"attempt-{attempt:02d}-response.json", candidate)
            inventory, error = validate_inventory(
                candidate,
                source_start=source_start,
                source_end=source_end,
            )
            if error is None:
                break
            last_error = error
        except Exception as exc:  # noqa: BLE001 - bounded retry with feedback
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
        if task_root:
            write_text_atomic(
                task_root / f"attempt-{attempt:02d}-error.txt", last_error + "\n"
            )

    mechanical = inventory is None
    if inventory is None:
        inventory = _mechanical_inventory(
            source_start, source_end, len(lines), last_error
        )
    report = WindowReport(
        id=window_id(document, source_start, source_end),
        ordinal=ordinal,
        source_start=source_start,
        source_end=source_end,
        summary=inventory.summary,
        observations=inventory.observations,
        mechanical=mechanical,
        note=last_error if mechanical else "",
    )
    if result_path:
        write_json_atomic(result_path, report)
    if live_root:
        live_root.mkdir(parents=True, exist_ok=True)
        stem = f"window-{ordinal:06d}-lines-{source_start:06d}-{source_end:06d}"
        write_json_atomic(live_root / f"{stem}.json", report)
        write_text_atomic(
            live_root / f"{stem}.md",
            _window_markdown(report, block.text, total),
        )
    return report, False


async def observe_document(
    source_text: str,
    *,
    model: ModelPort,
    config: WikiConfig | None = None,
    document: str | None = None,
    checkpoint_dir: Path | str | None = None,
    live_output_dir: Path | str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_check: StopCheck = None,
) -> ObservationSet:
    """Inventory independent overlapping windows, four at a time by default."""

    config = config or WikiConfig()
    text = normalize_source(source_text)
    document = document or document_id(text)
    if not text:
        return ObservationSet(
            document_id=document,
            source_sha256=sha256_text(source_text),
            normalized_source_sha256=sha256_text(text),
            source_line_count=0,
        )
    lines = split_source_lines(text)
    units = extract_image_units(lines)
    windows = overlapping_windows(
        len(lines),
        target=config.window_target_lines,
        overlap=config.window_overlap_lines,
    )
    checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
    live_root = Path(live_output_dir) if live_output_dir else None
    semaphore = asyncio.Semaphore(max(1, config.planner_concurrency))
    completed = 0

    async def one(ordinal: int, start: int, end: int) -> WindowReport:
        nonlocal completed
        async with semaphore:
            report, cached = await _observe_one(
                ordinal=ordinal,
                total=len(windows),
                source_start=start,
                source_end=end,
                lines=lines,
                units=units,
                document=document,
                model=model,
                config=config,
                checkpoint_root=checkpoint_root,
                live_root=live_root,
                stop_check=stop_check,
            )
        completed += 1
        if on_progress:
            stem = f"window-{ordinal:06d}-lines-{start:06d}-{end:06d}.md"
            on_progress(
                {
                    "stage": "observe",
                    "step": "window",
                    "current": completed,
                    "total": len(windows),
                    "window": ordinal,
                    "source_start": start,
                    "source_end": end,
                    "observations": len(report.observations),
                    "fallback": report.mechanical,
                    "cached": cached,
                    "live_output": str(live_root / stem) if live_root else "",
                }
            )
        return report

    reports = await asyncio.gather(
        *(one(position, start, end) for position, (start, end) in enumerate(windows, 1))
    )
    reports.sort(key=lambda item: item.ordinal)
    if on_progress:
        on_progress(
            {
                "stage": "observe",
                "step": "done",
                "windows": len(reports),
                "observations": sum(len(item.observations) for item in reports),
                "images": len(units),
                "mechanical_fallbacks": sum(item.mechanical for item in reports),
            }
        )
    return ObservationSet(
        document_id=document,
        source_sha256=sha256_text(source_text),
        normalized_source_sha256=sha256_text(text),
        source_line_count=len(lines),
        windows=reports,
        images=image_records(units),
    )
