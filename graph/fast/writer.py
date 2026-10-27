"""Fast page creation using the same writer and semantic judge as repair.

Creation starts with the writer while repair starts with a judge. After that,
both paths use the shared page-level writer -> judge loop from ``repair.py``.
Model work may run concurrently, but page files and state are committed in the
document's serial order.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from common.policy import policy_of
from graph.wiki.model import judge_model, writer_model
from graph.wiki.page import PLACEHOLDER_RE
from graph.wiki.storage import slice_text, write_text_atomic

from .passes import bounded_map
from .repair import (
    MAX_REWRITE_ATTEMPTS,
    RepairJudgeResult,
    _Artifact,
    _ImageSlot,
    _PageInput,
    _PageShell,
    _approved,
    _feedback,
    _image_errors,
    _judge,
    _judge_rank,
    _writer,
    _write_artifacts,
)
from .wiki import append_low_coverage_source, recent_writer_context


@dataclass
class _GenerationSession:
    page: Any
    prompt_page: _PageInput
    page_units: list[Any]
    task_dir: Path
    raw_source: str
    artifacts: list[_Artifact] = field(default_factory=list)


@dataclass
class _GenerationOutcome:
    session: _GenerationSession
    title: str
    body: str
    attempts: int
    verdict: RepairJudgeResult | None = None
    errors: list[str] = field(default_factory=list)
    verbatim: bool = False


def _source_body(
    page: Any,
    *,
    lines: Sequence[str],
    units: Sequence[Any],
) -> str:
    """Readable lossless fallback used only when no candidate can be judged."""

    from graph.wiki import pipeline as wiki

    chunks: list[str] = []
    for start, end in page.owner_ranges:
        range_units = [
            unit
            for unit in units
            if start <= unit.source_start and unit.source_end <= end
        ]
        chunks.append(wiki._verbatim_section(lines, start, end, range_units).rstrip())
    return "\n\n".join(chunks).rstrip() + "\n"


def _prepare_session(
    page: Any,
    *,
    pages: Sequence[Any],
    lines: list[str],
    units: Sequence[Any],
    config: Any,
    work_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
) -> _GenerationSession:
    from graph.wiki import pipeline as wiki

    if not page.owner_ranges:
        raise wiki.PipelineError(f"page {page.number} has no source range")
    page_units = wiki._page_units(page, units)
    raw_source = wiki._prompt_safe(
        "\n\n".join(
            slice_text(lines, start, end) for start, end in page.owner_ranges
        ),
        page_units,
    )
    if not raw_source.strip():
        raise wiki.PipelineError(
            f"page {page.number}: source is empty; check conversion/extraction before writing the wiki"
        )
    first_line = min(start for start, _end in page.owner_ranges)
    previous_start = max(0, first_line - 1 - 50)
    context = recent_writer_context(
        page=page,
        pages=pages,
        previous_source=wiki._prompt_safe(
            "\n".join(lines[previous_start:first_line - 1]), units
        ),
    )
    task_dir = work_root / f"page-{page.number:03d}"
    prompt_page = _PageInput(
        document=str(source_path),
        number=page.number,
        filename=page.filename,
        owner_ranges=list(page.owner_ranges),
        reference_ranges=[],
        original_text="",
        shell=_PageShell(page.title, "", ""),
        masked_body="",
        images=[_ImageSlot(unit.placeholder, unit.raw) for unit in page_units],
        owner_evidence=wiki._numbered_source(
            lines, page.owner_ranges, page_units
        ),
        reference_evidence="（なし）",
        allowed_source=raw_source,
        context=context,
        page_path=wiki_root / page.filename,
        state_path=wiki._page_state_path(state_root, page),
        state={},
        guidance=list(config.human_guidance.get(page.filename, ())),
        has_initial_candidate=False,
    )
    return _GenerationSession(
        page=page,
        prompt_page=prompt_page,
        page_units=page_units,
        task_dir=task_dir,
        raw_source=raw_source,
    )


def _clean_generation_images(
    body: str,
    session: _GenerationSession,
    *,
    lines: Sequence[str],
) -> str:
    """Keep only this page's opaque image tags, exactly once each."""

    from graph.wiki import pipeline as wiki

    expected = {unit.placeholder for unit in session.page_units}
    body = re.sub(r"`+(\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\])`+", r"\1", body)
    body = PLACEHOLDER_RE.sub(
        lambda match: match.group(0) if match.group(0) in expected else "",
        body,
    )
    return wiki._preserve_image_placeholders(body, session.page_units, lines)


async def _generate_page(
    session: _GenerationSession,
    *,
    lines: Sequence[str],
    model: Any,
    config: Any,
    stop_check: Any,
    on_progress: Any,
) -> _GenerationOutcome:
    """Run writer -> judge retries and retain the best judged page candidate."""

    from graph.wiki import pipeline as wiki

    page = session.page
    prompt_page = session.prompt_page
    fallback = _source_body(page, lines=lines, units=session.page_units)
    if model is None:
        return _GenerationOutcome(
            session,
            page.title,
            fallback,
            0,
            errors=["model unavailable"],
            verbatim=True,
        )

    current_title = page.title
    current_body = ""
    feedback: list[str] = []
    feedback_history: list[str] = []
    best_title = page.title
    best_body = ""
    best_verdict: RepairJudgeResult | None = None
    best_rank = (-1, -10**9, -1)
    errors: list[str] = []
    max_attempts = min(
        MAX_REWRITE_ATTEMPTS,
        max(1, int(getattr(config, "write_attempts", MAX_REWRITE_ATTEMPTS))),
    )

    wiki._emit(
        on_progress,
        "writer",
        "page_start",
        page=page.title,
        current=0,
        total=0,
    )
    for attempt in range(1, max_attempts + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("page writing cancelled")
        try:
            candidate_title, candidate_body = await _writer(
                prompt_page,
                current_title,
                current_body,
                feedback,
                model=writer_model(model),
                attempt=attempt,
                earlier_feedback=[
                    item for item in feedback_history if item not in feedback
                ],
                artifacts=session.artifacts,
            )
        except Exception as exc:
            error = f"writer unavailable: {type(exc).__name__}: {exc}"[:1000]
            errors.append(error)
            feedback = [error]
            feedback_history.extend(
                item for item in feedback if item not in feedback_history
            )
            continue

        candidate_body = _clean_generation_images(
            candidate_body, session, lines=lines
        )
        image_errors = _image_errors(
            prompt_page, candidate_title, candidate_body
        )
        if image_errors:
            errors.extend(image_errors)
            feedback = image_errors
            feedback_history.extend(
                item for item in feedback if item not in feedback_history
            )
            current_title, current_body = candidate_title, candidate_body
            continue

        verdict, judge_error = await _judge(
            prompt_page,
            candidate_title,
            candidate_body,
            model=judge_model(model),
            stem=f"rewrite-{attempt:02d}-judge",
            artifacts=session.artifacts,
        )
        current_title, current_body = candidate_title, candidate_body
        if verdict is None:
            error = "judge unavailable: " + (judge_error or "unknown error")
            errors.append(error)
            feedback = [error]
            feedback_history.extend(
                item for item in feedback if item not in feedback_history
            )
            continue

        rank = _judge_rank(verdict, attempt=attempt)
        if rank > best_rank:
            best_title = candidate_title
            best_body = candidate_body
            best_verdict = verdict
            best_rank = rank
        wiki._emit(
            on_progress,
            "writer",
            "page_judged",
            page=page.title,
            attempt=attempt,
            score=verdict.coverage_score,
            approved=_approved(verdict),
        )
        if _approved(verdict):
            break
        feedback = _feedback(verdict)
        feedback_history.extend(
            item for item in feedback if item not in feedback_history
        )

    if best_verdict is None:
        wiki._emit(
            on_progress,
            "write",
            "page_verbatim",
            page=page.title,
            error="; ".join(errors)[-500:],
        )
        return _GenerationOutcome(
            session,
            page.title,
            fallback,
            max_attempts,
            errors=errors or ["no judged candidate"],
            verbatim=True,
        )

    return _GenerationOutcome(
        session,
        best_title,
        append_low_coverage_source(
            best_body, session.raw_source, best_verdict.coverage_score
        ),
        max_attempts if not _approved(best_verdict) else best_rank[2],
        verdict=best_verdict,
        errors=errors,
    )


async def rewrite_pending_pages(
    *,
    pending: Sequence[Any],
    results: list[Any],
    pages: Sequence[Any],
    lines: list[str],
    units: Sequence[Any],
    model: Any,
    config: Any,
    work_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
    source_snapshot_path: Path,
    source_sha256: str,
    source_line_count: int,
    rewrite_version: str,
    stop_check: Any,
    on_progress: Any,
    parents: dict[str, str] | None,
) -> None:
    """Create whole pages with the repair writer/judge, then commit serially."""

    del source_line_count, parents
    from graph.wiki import pipeline as wiki

    policy = policy_of(config)
    sessions = [
        _prepare_session(
            page,
            pages=pages,
            lines=lines,
            units=units,
            config=config,
            work_root=work_root,
            wiki_root=wiki_root,
            state_root=state_root,
            source_path=source_path,
        )
        for page in pending
    ]

    async def generate(session: _GenerationSession) -> _GenerationOutcome:
        try:
            return await _generate_page(
                session,
                lines=lines,
                model=model,
                config=config,
                stop_check=stop_check,
                on_progress=on_progress,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fallback = _source_body(
                session.page, lines=lines, units=session.page_units
            )
            error = f"{type(exc).__name__}: {exc}"[:1000]
            session.artifacts.append(_Artifact("generation-error.txt", error + "\n"))
            return _GenerationOutcome(
                session,
                session.page.title,
                fallback,
                0,
                errors=[error],
                verbatim=True,
            )

    outcomes = await bounded_map(
        sessions,
        max(1, int(config.rewrite_concurrency)),
        generate,
    )

    # Final titles must be known before cross-page links and navigation are made.
    for outcome in outcomes:
        page = outcome.session.page
        previous_title = page.title
        page.title = outcome.title
        if page.path and page.path[-1] == previous_title:
            page.path[-1] = outcome.title

    for outcome in outcomes:
        session = outcome.session
        page = session.page
        _write_artifacts(session.task_dir, session.artifacts)
        body = policy.section(outcome.body).rstrip()
        markdown = f"# {page.title}\n\n{body}\n"
        masked, unmask = policy.mask_for_linking(markdown)
        markdown = unmask(
            wiki.link_titles(
                masked,
                [
                    (other.title, other.filename)
                    for other in pages
                    if other.number != page.number
                ],
            )
        )
        markdown += wiki._nav_footer(page, pages)
        restored, unresolved = wiki.restore_images(markdown, session.page_units)
        if unresolved:
            raise wiki.PipelineError(
                f"page {page.number} has unresolved image placeholders: {unresolved}"
            )
        page.reference_ranges = []
        verdict = outcome.verdict
        defects = []
        missing = []
        if verdict is not None:
            defects = [
                *[f"タイトル: {item}" for item in verdict.title_defects],
                *[f"本文: {item}" for item in verdict.body_defects],
            ]
            missing = list(verdict.missing_important_information)
        result = wiki.RewriteResult(
            page=page,
            markdown=wiki.strip_reader_references(restored),
            attempts=outcome.attempts,
            judge_score=(verdict.coverage_score if verdict is not None else None),
            missing_important_information=missing,
            defects=defects,
            verbatim_sections=(
                ["ページ全体: " + "; ".join(outcome.errors)]
                if outcome.verbatim
                else []
            ),
        )
        results.append(result)
        write_text_atomic(wiki_root / page.filename, result.markdown)
        wiki._write_page_state(
            wiki._page_state_path(state_root, page),
            result,
            rewrite_version=rewrite_version,
            pages=pages,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_sha256,
        )
        wiki._emit(
            on_progress,
            "writer",
            "page_done",
            page=page.title,
            total=len(pages),
        )
        wiki._emit(
            on_progress,
            "rewrite",
            "page_done",
            current=len(results),
            total=len(pages),
            page=page.title,
            attempts=result.attempts,
            score=result.judge_score,
            verbatim_sections=len(result.verbatim_sections),
        )


__all__ = ["rewrite_pending_pages"]
