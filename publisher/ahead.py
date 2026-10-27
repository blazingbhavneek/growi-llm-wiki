"""Background workers that keep the parser and the linker busy while sync builds.

``sync`` (runner/cli.py:_cmd_sync_isolated) builds one document at a time, and every
live-state write stays in that serial loop: candidates, promotion, linking,
publication. These threads only fill content-addressed caches that the serial steps
read:

- ``ParseAhead`` parses queued documents in claim order (publisher/queue.py:_PRIORITY)
  into the parse cache read by publisher/pipeline.py:_parse.
- ``PlannerAhead`` consumes those parsed documents in the same order and fills the
  validated seed-plan cache read by graph/wiki/pipeline.py:run_pipeline.
- ``LinkAhead`` describes the chunks of each built document (and runs the Jev role
  check) into the metadata cache read by graph/linker/service.py:link_document
  (graph/linker/meta_cache.py).

The builder and the linker each use the configured model concurrency, so the most
in flight is twice the setting. A worker failure is only a cache miss: the serial
step does the work itself.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


class ParseAhead:
    """Parse at most two queue positions beyond the current serial builder."""

    def __init__(self, project: Any, settings: Any, jobs: Sequence[Any], *, on_ready: Any = None) -> None:
        self._stop = threading.Event()
        self._on_ready = on_ready
        # Before the first claim, allow X and X+1. Claiming X opens X+2. Every
        # later claim opens one more position, preserving the three-stage train.
        self._slots = threading.Semaphore(2)
        self._thread = threading.Thread(
            target=self._run, args=(project, settings, list(jobs)), name="parse-ahead", daemon=True,
        )
        self._thread.start()

    def advance(self) -> None:
        """Allow the worker to inspect one more queued position."""
        self._slots.release()

    def stop(self) -> None:
        self._stop.set()  # a running parse finishes; nothing new starts
        self._slots.release()  # wake an idle worker so it can observe the stop

    def _ready(self, position: int, job: Any, markdown: str) -> None:
        if self._on_ready is None or self._stop.is_set():
            return
        try:
            self._on_ready(position, job, markdown)
        except Exception as exc:  # noqa: BLE001 - planning ahead is only a cache optimization
            log.warning("plan-ahead enqueue %s failed; the build will plan it: %s: %s",
                        job.rel, type(exc).__name__, exc)

    def _run(self, project: Any, settings: Any, jobs: list[Any]) -> None:
        from graph.workspace.parser_client import ParserSkip, decode_text_bytes, parse_document, parser_skip_cache

        from .history import read_blob
        from .ledger import load_ledger
        from .pipeline import (
            PARSER_TIMEOUT, VERBATIM, _PARSING, _PARSING_LOCK, _check_parse_size, _write_raw, parse_cache_file,
        )

        sources = load_ledger(project.metadata / "pipeline.json").sources
        for position, job in enumerate(jobs):
            self._slots.acquire()
            if self._stop.is_set():
                return
            raw_path = project.raw_file(job.raw_rel)
            if not job.target_blob_oid:
                continue
            if job.from_rel and job.target_sha256 == str(sources.get(job.from_rel, {}).get("source_sha256") or ""):
                continue
            if Path(job.rel).suffix.lower() in VERBATIM:
                markdown = decode_text_bytes(read_blob(project, job.target_blob_oid), source=Path(job.rel))
                if parser_skip_cache() is not None and not markdown.strip():
                    continue  # TEMPORARY: the builder records the empty-text skip.
                self._ready(position, job, markdown)
                continue
            if raw_path.exists() and job.target_sha256 == str(sources.get(job.rel, {}).get("parsed_source_sha256") or ""):
                self._ready(position, job, raw_path.read_text(encoding="utf-8"))
                continue
            # Same inputs the build passes (publisher/pipeline.py:sync_once), so the key matches.
            previous = raw_path.read_text(encoding="utf-8") if raw_path.exists() else None
            validating = previous is not None and job.classification != "forced"
            target = parse_cache_file(settings, job.target_sha256, previous, validating)
            if target is None:
                continue
            if target.exists():
                self._ready(position, job, target.read_text(encoding="utf-8"))
                continue
            skips = parser_skip_cache()
            skip_key = (str(target), Path(job.rel).name)
            if skips is not None and skip_key in skips:
                continue
            with _PARSING_LOCK:
                active = _PARSING.get(target.name)
                if active is None:
                    running = threading.Event()
                    _PARSING[target.name] = running
                else:
                    running = active
            if active is not None:
                running.wait()
                if target.exists():
                    self._ready(position, job, target.read_text(encoding="utf-8"))
                continue
            started = time.monotonic()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=target.parent) as folder:
                    source = Path(folder) / Path(job.rel).name
                    source.write_bytes(read_blob(project, job.target_blob_oid))
                    markdown = parse_document(
                        source,
                        base_url=str(getattr(settings, "parser_base_url", "")),
                        settings=settings,
                        timeout_s=float(getattr(settings, "parser_timeout", PARSER_TIMEOUT)),
                        previous_markdown=previous,
                        validate_markdown=(lambda text: _check_parse_size(job.rel, previous, text)) if validating else None,
                    )
                _write_raw(target, markdown)
                self._ready(position, job, markdown)
                log.info("parse-ahead %s: %.1fs", job.rel, time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - the build parses it itself
                if isinstance(exc, ParserSkip):
                    if skips is not None:
                        skips[skip_key] = str(exc)
                    log.warning("パースをスキップ: %s: %s", job.rel, exc)
                else:
                    log.warning("parse-ahead %s failed; the build will parse it: %s: %s", job.rel, type(exc).__name__, exc)
            finally:
                with _PARSING_LOCK:
                    _PARSING.pop(target.name, None)
                running.set()


class PlannerAhead:
    """Plan parsed documents in FIFO order, starting with the builder's next one."""

    def __init__(self, project: Any, settings: Any) -> None:
        # Import here, not first in two threads at once (Python's module-lock deadlock).
        import graph.formats, graph.wiki.page, graph.wiki.pipeline  # noqa: E401,F401
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run(project, settings)), name="planner-ahead", daemon=True,
        )
        self._thread.start()

    def add(self, position: int, job: Any, markdown: str) -> None:
        # X is owned by the serial builder; planning starts at X+1.
        if position > 0 and not self._stop.is_set():
            self._queue.put((job, markdown))

    def close(self, *, wait: bool = False) -> None:
        if not wait:
            self._stop.set()
        self._queue.put(None)
        if wait:
            self._thread.join()

    async def _run(self, project: Any, settings: Any) -> None:
        from graph.formats import is_tabular, kind_of
        from graph.workspace.writer import wiki_config
        from graph.wiki.page import is_small_document
        from graph.wiki.pipeline import prepare_seed_plan_cache

        from .pipeline import _model

        if str(getattr(settings, "ingest_mode", "wiki")) != "wiki":
            return
        try:
            model = _model(settings, project)
        except Exception as exc:  # noqa: BLE001 - the serial builder retains the normal fallback
            log.warning("plan-ahead disabled: %s: %s", type(exc).__name__, exc)
            return
        while True:
            item = await asyncio.to_thread(self._queue.get)
            if item is None or self._stop.is_set():
                return
            job, markdown = item
            kind = kind_of(job.raw_rel)
            if is_tabular(kind):
                continue
            if getattr(settings, "skip_excel_and_small", False) and is_small_document(markdown):
                continue
            started = time.monotonic()
            try:
                config = wiki_config(
                    settings,
                    run_dir=Path(settings.cache_dir) / "planner-state",
                    source_kind=kind,
                )
                await prepare_seed_plan_cache(
                    markdown,
                    config=config,
                    model=model,
                    stop_check=self._stop.is_set,
                )
                log.info("plan-ahead %s: %.1fs", job.rel, time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - the serial builder plans it itself
                log.warning("plan-ahead %s failed; the build will plan it: %s: %s",
                            job.rel, type(exc).__name__, exc)


class LinkAhead:
    """Describe built documents' chunks for the linker while later documents build."""

    def __init__(self, project: Any, settings: Any, *, model: Any = None) -> None:
        # Import here, not first in two threads at once (Python's module-lock deadlock).
        import graph.linker.jev_judge, graph.linker.prompts, graph.linker.service  # noqa: E401,F401
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run(project, settings, model)), name="link-ahead", daemon=True,
        )
        self._thread.start()

    def add(self, project: Any, raw_rels: Sequence[str]) -> None:
        """Snapshot the pages the linker will read, now, before the next promotion."""
        from graph.linker.service import _document, _id_seed, _previously_complete, _team

        for rel in raw_rels:
            folder = Path(project.wiki_dir(rel))
            planning = folder / "_planning"
            if not folder.is_dir() or _previously_complete(planning):
                continue
            # link_document chunks _planning/pages, or the pages it is about to snapshot there
            pages = sorted((planning / "pages").glob("*.md")) or sorted(folder.glob("*.md"))
            document = _document(project, rel)
            snapshot = [(page.name, page.read_text(encoding="utf-8")) for page in pages]
            self._queue.put((sum(len(text) for _, text in snapshot), self._order, (
                rel, document, _team(project), _id_seed(planning, document), planning / "chunks.json",
                snapshot,
            )))
            self._order += 1

    def close(self, *, wait: bool) -> None:
        """``wait``: finish every added document first (before sync's link phase)."""
        if not wait:
            self._stop.set()
        self._queue.put((float("inf"), self._order, None))
        if wait:
            self._thread.join()

    async def _run(self, project: Any, settings: Any, model: Any) -> None:
        from common.policy import policy_of
        from graph.linker.prompts import CHUNK_META_VERSION

        from .pipeline import _model

        policy = policy_of(settings)
        try:
            model = model or _model(settings, project)
        except Exception as exc:  # noqa: BLE001 - without a model the linker describes everything itself
            log.warning("link-ahead disabled: %s: %s", type(exc).__name__, exc)
            return
        engine = None
        if str(getattr(settings, "wiki_linker_judge", "llm")) == "jev":
            try:
                from jev import get_engine_for

                engine = get_engine_for(settings)
            except Exception as exc:  # noqa: BLE001 - the linker reports Jev itself
                log.warning("link-ahead without Jev roles: %s", exc)
        meta_version = policy.cache_key(CHUNK_META_VERSION)
        while True:
            _, _, item = await asyncio.to_thread(self._queue.get)
            if item is None or self._stop.is_set():
                return
            try:
                await self._describe(item, settings=settings, model=model, engine=engine,
                                     policy=policy, meta_version=meta_version)
            except Exception as exc:  # noqa: BLE001 - the linker describes it itself
                log.warning("link-ahead %s failed; the linker will describe it: %s: %s", item[0], type(exc).__name__, exc)

    async def _describe(self, item: tuple, *, settings: Any, model: Any, engine: Any, policy: Any, meta_version: str) -> None:
        from graph.linker import chunks, meta_cache
        from graph.linker.jev_judge import check_roles
        from graph.linker.service import _concurrency

        rel, document, team, id_seed, chunk_cache, pages = item
        items = [chunk for name, text in pages for chunk in chunks.make_chunks(document, team, name, text, id_seed=id_seed)]
        known = chunks.cache_by_hash(chunk_cache, meta_version=meta_version)
        new = [chunk for chunk in items if chunk.text_sha256 not in known]
        ahead = meta_cache.load(settings, meta_version, (chunk.text_sha256 for chunk in new))
        if all(chunk.text_sha256 in ahead for chunk in new):
            return
        started = time.monotonic()
        # The same call link_document makes for a document that is not linked yet:
        # every chunk, with chunks.json as the cache.
        calls, _fallbacks = await chunks.describe_all(
            items, model=model, output_language=str(getattr(settings, "wiki_output_language", "Japanese (日本語)")),
            concurrency=_concurrency(settings), cache={**ahead, **known},
            artifact_dir=Path(settings.cache_dir) / "linker-runs" / document,
            stop_check=self._stop.is_set, parallel=str(getattr(settings, "wiki_linker_judge", "llm")) == "jev",
            policy=policy,
        )
        if engine is not None:
            await check_roles(engine, items, settings)
        # Fallback metadata (a failed call) is left out, so the linker tries that chunk again.
        meta_cache.store(settings, meta_version, {
            chunk.text_sha256: chunk.meta for chunk in new
            if chunk.meta.keywords or chunk.meta.search_terms or chunk.meta.entities
        })
        log.info("link-ahead %s: %d chunks, %d calls, %.1fs", rel, len(new), calls, time.monotonic() - started)


__all__ = ["LinkAhead", "ParseAhead", "PlannerAhead"]
