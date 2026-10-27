"""One reconciliation pass for the minimal publisher."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from graph.clients.embeddings import Embedder
from graph.common.markdown import strip_big_tables, strip_image_media
from graph.growi import MARKER_FORMAT, GrowiClient, GrowiPublisher, growi_path
from graph.workspace.parser_client import ParserSkip, UnsupportedDocument, parse_document, parser_skip_cache, read_text_source
from graph.workspace.project import (
    VERBATIM,
    Project,
    assert_unique_generated_paths,
    open_project,
    raw_name_for,
    wiki_folder_name,
)
from graph.workspace.writer import links_up_to_date, run_linkers, wiki_config, wiki_up_to_date, write_wiki_pages
from common.policy import policy_of
from graph.wiki.storage import read_json, write_json_atomic

from .ledger import Ledger, load_ledger, save_ledger
from .scanner import Scan, SourceFile, scan_mount

log = logging.getLogger(__name__)
PARSER_TIMEOUT = 7200.0
PARSE_MIN_RATIO = 0.30  # a re-parse this much smaller than before is treated as broken
PARSE_GATE_MIN_CHARS = 2000  # tiny documents may legitimately lose most text


@contextmanager
def _progress_heartbeat(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    file: str,
    document: str | None = None,
    interval: float = 10.0,
):
    if callback is None:
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()

    def pulse() -> None:
        while not stopped.wait(interval):
            event = {
                "stage": stage,
                "step": "waiting",
                "file": file,
                "elapsed_seconds": round(time.monotonic() - started),
            }
            if document:
                event["document"] = document
            callback(event)

    thread = threading.Thread(target=pulse, name=f"{stage}-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def _source_row(
    item: SourceFile,
    raw_rel: str,
    error: str = "",
    *,
    details: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    parsed_source_sha256: str | None = None,
) -> dict[str, Any]:
    details, previous = details or {}, previous or {}
    source_id = str(details.get("source_id") or previous.get("source_id") or uuid.uuid4())
    raw_path = Path(raw_rel)
    legacy_seed = (raw_path.parent / wiki_folder_name(raw_path.name)).as_posix()
    return {
        "source_id": source_id,
        "id_seed": str(previous.get("id_seed") or details.get("id_seed") or (legacy_seed if previous else source_id)),
        "mount_rel": item.rel,
        "source_sha256": item.source_sha256,
        "parsed_source_sha256": (
            parsed_source_sha256 if parsed_source_sha256 is not None else
            item.source_sha256 if not error else
            str(previous.get("parsed_source_sha256") or (
                previous.get("source_sha256") if not previous.get("last_error") else ""
            ) or "")
        ),
        "source_blob_oid": str(details.get("source_blob_oid") or previous.get("source_blob_oid") or ""),
        "size": item.size,
        "mtime_ns": item.mtime_ns,
        "raw_rel": raw_rel,
        "wiki_rel": item.rel,
        "parser": item.parser,
        "completed_at": "" if error else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_error": error,
    }


def _identity_path(project: Project) -> Path:
    return project.metadata / "source-identities.json"


def _identities(project: Project) -> dict[str, Any]:
    data = read_json(_identity_path(project), default={})
    return {
        "schema_version": 1,
        "active": dict(data.get("active") or {}),
        "tombstones": dict(data.get("tombstones") or {}),
    }


def _save_identities(project: Project, identities: dict[str, Any]) -> None:
    write_json_atomic(_identity_path(project), identities)


def _store_source(project: Project, item: SourceFile, row: dict[str, Any], source_path: Path) -> None:
    source_id = str(row.get("source_id") or "")
    if not source_id:
        return
    folder = project.root / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{source_id}.*"):
        old.unlink()
    shutil.copyfile(source_path, folder / f"{source_id}{Path(item.rel).suffix.lower()}")
    identities = _identities(project)
    identities["active"][item.rel] = source_id
    identities["tombstones"].pop(item.rel, None)
    _save_identities(project, identities)


def _content_hash(folder: Path) -> str:
    pages = []
    for page in sorted(Path(folder).glob("*.md")):
        pages.append((page.name, hashlib.sha256(page.read_bytes()).hexdigest()))
    payload = json.dumps(pages, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _raw_rel(item: SourceFile) -> str:
    path = Path(item.rel)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _document_raw_rel(document: str) -> str:
    path = Path(document)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _assert_unique_growi_locations(
    project: Project,
    documents: set[str],
    publisher: GrowiPublisher,
) -> None:
    """Reject document/folder names that GROWI normalizes onto one location."""

    logical_paths = set(documents)
    for document in documents:
        parts = Path(document).parts
        logical_paths.update("/".join(parts[:index]) for index in range(1, len(parts)))
    remote_owners: dict[str, str] = {}
    for logical_path in logical_paths:
        connection = getattr(publisher, "connection", None)
        remote_path = (
            growi_path(connection.write_path, logical_path)
            if connection is not None
            else publisher.doc_path(project, _document_raw_rel(logical_path))
        )
        previous = remote_owners.get(remote_path)
        if previous is not None and previous != logical_path:
            raise ValueError(
                f"wiki locations resolve to the same GROWI path {remote_path!r}: "
                f"{previous!r}, {logical_path!r}"
            )
        remote_owners[remote_path] = logical_path


def _folders(project: Project, *, allow_unlinked: bool = False) -> dict[str, Path]:
    ready = {"complete", "disabled"}
    if allow_unlinked:
        ready.update({"pending", "failed", "render_pending"})
    result: dict[str, Path] = {}
    for marker in project.wiki.rglob("_planning/linker.json") if project.wiki.exists() else ():
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("status") not in ready:
                continue
        except (OSError, ValueError):
            continue
        result[marker.parent.parent.relative_to(project.wiki).as_posix()] = marker.parent.parent
    return result


def _write_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".raw-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _prune_empty_parents(path: Path, root: Path) -> None:
    """Remove empty generated directories below ``root`` and keep the root itself."""

    root = Path(root).resolve(strict=False)
    current = Path(path).resolve(strict=False)
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _assert_source_unchanged(item: SourceFile, path: Path) -> None:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != item.source_sha256:
        raise RuntimeError(f"source changed during parse: {item.rel}")


def _check_parse_size(rel: str, previous: str, current: str) -> None:
    """Refuse a parse that lost most of the document's text (R7)."""

    before = len(strip_image_media(previous).strip())
    after = len(strip_image_media(current).strip())
    if before >= PARSE_GATE_MIN_CHARS and after < PARSE_MIN_RATIO * before:
        raise RuntimeError(
            f"suspicious parse for {rel}: text shrank from {before} to {after} characters; "
            f"if this is intended run `python main.py sync --force {rel}`"
        )


def _growi_url(settings: Any) -> str:
    return str(getattr(settings, "growi_url", "") or os.environ.get("GROWI_URL", "")).strip().rstrip("/")


def _connection(settings: Any) -> Any | None:
    url = _growi_url(settings)
    if not url:
        return None
    target = str(settings.target_name).strip("/")
    return SimpleNamespace(
        name=target,
        write_path=f"/{target}",
        root_path=f"/{target}",
        mode=str(getattr(settings, "growi_mode", "attach")).strip() or "attach",
    )


def _publisher(settings: Any) -> GrowiPublisher | None:
    connection = _connection(settings)
    if connection is None:
        return None
    client = GrowiClient(
        _growi_url(settings),
        str(getattr(settings, "growi_token", "") or os.environ.get("GROWI_TOKEN", "")),
        timeout=float(getattr(settings, "growi_timeout", 30)),
    )
    from graph.config import HumanSyncPolicy

    from .human_changes import LlmHumanModel

    policy = HumanSyncPolicy.resolve(getattr(settings, "human_sync_mode", "off"))

    return GrowiPublisher(
        client,
        connection,
        human_sync_policy=policy,
        human_model_factory=lambda project: LlmHumanModel(settings, project),
    )


@contextmanager
def _lock(project: Project):
    path = project.metadata / "pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("publisher already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Parses the parse-ahead worker (publisher/ahead.py) is running now; _parse waits for
# one instead of sending the same document to the parser a second time.
_PARSING: dict[str, threading.Event] = {}
_PARSING_LOCK = threading.Lock()


def parse_cache_file(settings: Any, source_sha256: str, previous_markdown: str | None, validating: bool) -> Path | None:
    """Where the parsed Markdown for these exact parser inputs is cached (None: caching off)."""

    root = str(getattr(settings, "cache_dir", "") or "")
    if not root or not source_sha256:
        return None
    previous = hashlib.sha256(previous_markdown.encode("utf-8")).hexdigest() if previous_markdown is not None else ""
    base_url = str(getattr(settings, "parser_base_url", ""))
    key = hashlib.sha256(f"{source_sha256}\0{base_url}\0{previous}\0{int(validating)}".encode("utf-8")).hexdigest()
    return Path(root) / "parse" / f"{key}.md"


def _parse(
    item: SourceFile,
    path: Path,
    settings: Any,
    previous_markdown: str | None = None,
    validate_markdown: Callable[[str], None] | None = None,
) -> str:
    if path.suffix.lower() in VERBATIM:
        return read_text_source(path)
    base_url = str(getattr(settings, "parser_base_url", ""))
    if not base_url:
        raise RuntimeError(f"WIKI_PARSER_BASE_URL is required for {item.rel}")
    cached = parse_cache_file(settings, item.source_sha256, previous_markdown, validate_markdown is not None)
    if cached is not None:
        with _PARSING_LOCK:
            running = _PARSING.get(cached.name)
        if running is not None:
            running.wait()
        if cached.exists():
            markdown = cached.read_text(encoding="utf-8")
            if validate_markdown is not None:
                validate_markdown(markdown)
            return markdown
    # TEMPORARY: reuse a rejection from parse-ahead for these exact cached inputs.
    skips = parser_skip_cache()
    skip_key = (str(cached), path.name) if cached is not None else None
    if skips is not None and skip_key in skips:
        raise ParserSkip(skips[skip_key])
    try:
        markdown = parse_document(
            path,
            base_url=base_url,
            settings=settings,
            timeout_s=float(getattr(settings, "parser_timeout", PARSER_TIMEOUT)),
            previous_markdown=previous_markdown,
            validate_markdown=validate_markdown,
        )
    except ParserSkip as exc:
        if skips is not None and skip_key is not None:
            skips[skip_key] = str(exc)
        raise
    if cached is not None:
        _write_raw(cached, markdown)
    return markdown


def _model(settings: Any, project: Project) -> Any:
    config = wiki_config(settings, run_dir=project.metadata / "state" / "publisher")
    return policy_of(config).model_port(config)


def _wiki_raw_rels(project: Project) -> list[str]:
    result: list[str] = []
    for marker in sorted(project.wiki.rglob("_planning/source.json")):
        try:
            result.append(str(json.loads(marker.read_text(encoding="utf-8"))["raw"]))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return list(dict.fromkeys(result))


def _pending_link_rels(project: Project, settings: Any, rels: list[str] | None = None, *, force: bool = False) -> list[str]:
    if not getattr(settings, "wiki_linker_enabled", True):
        return []
    candidates = rels if rels is not None else _wiki_raw_rels(project)
    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    return [rel for rel in candidates if project.wiki_dir(rel).exists() and (force or not links_up_to_date(project, rel, mode=mode))]


def _capture_remote(
    project: Project,
    ledger: Ledger,
    publisher: Any,
    *,
    only: set[str] | None = None,
    page_ids: set[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[str], list[str], set[str]]:
    """Capture while local files still represent the previous accepted output."""
    if not hasattr(publisher, "pull_changes"):
        return [], [], set()
    # A document whose first publish died midway has page rows but no document row yet; its pages are owned too.
    documents = set(ledger.published_documents) | {Path(path).parent.as_posix() for path in ledger.published_pages}
    folders = {doc: project.wiki / doc for doc in documents if (project.wiki / doc).is_dir()}
    if only is not None:
        wanted = {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
        folders = {doc: folder for doc, folder in folders.items() if doc in wanted}
    if not ledger.published_pages and ledger.published_documents:
        discovered = publisher.discover_documents(project, [_document_raw_rel(doc) for doc in folders])
        ledger.published_pages.update({path: {
            "growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
            "marker_id": publisher.page_marker_id(project, path), "marker_seed": publisher.page_marker_seed(project, path),
        } for path, page in discovered.items()})
    pages = {
        path: row for path, row in ledger.published_pages.items()
        if Path(path).parent.as_posix() in folders
        and (page_ids is None or str(row.get("page_id") or "") in page_ids)
    }
    unchanged = {doc for doc, folder in folders.items()
                 if ledger.published_documents.get(doc, {}).get("content_sha256") == _content_hash(folder)}
    pull_args: dict[str, Any] = {}
    # Keep compatibility with the small fake publishers used by callers and tests;
    # the real GROWI publisher owns the per-page capture events.
    if on_progress is not None and isinstance(publisher, GrowiPublisher):
        pull_args["on_progress"] = on_progress
    pulled, failures, blocked = publisher.pull_changes(project, pages, unchanged, **pull_args)
    for doc in {Path(path).parent.as_posix() for path in pulled}:
        if doc in ledger.published_documents:
            ledger.published_documents[doc]["content_sha256"] = _content_hash(folders[doc])
    return pulled, failures, blocked


def _capture_detected(
    project: Project,
    ledger: Ledger,
    publisher: Any,
    settings: Any,
    *,
    force_inventory: bool = False,
) -> tuple[list[str], list[str], set[str], dict[str, Any], tuple[Any, Any] | None]:
    """Use activities as hints and the normal pull path as authority."""

    from .activity import ActivityDetector
    from .human_changes import HumanStore
    from graph.growi.client import _page_stamps

    if not hasattr(publisher.client, "list_activities") or not hasattr(publisher.client, "list_all_pages"):
        pulled, failures, blocked = _capture_remote(project, ledger, publisher)
        return pulled, failures, blocked, {
            "fallback_reason": "activity_api_not_supported_by_client",
            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
        }, None

    detector = ActivityDetector(
        project,
        endpoint=_growi_url(settings),
        boundary=str(publisher.connection.root_path),
        overlap_seconds=int(getattr(settings, "human_sync_activity_overlap_seconds", 60)),
    )
    batch = detector.poll(publisher.client, ledger.published_pages)
    inventory = None
    try:
        cursor = detector.cursor()
    except (OSError, ValueError, TypeError):
        cursor = {}
    last_inventory = str(cursor.get("last_inventory_at") or "")
    audit_seconds = int(getattr(settings, "human_sync_activity_audit_seconds", 3600))
    audit_due = not last_inventory
    if last_inventory and audit_seconds > 0:
        try:
            from datetime import datetime, timezone

            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_inventory.replace("Z", "+00:00"))).total_seconds()
            audit_due = age >= audit_seconds
        except ValueError:
            audit_due = True
    discovery_failures: list[str] = []
    if force_inventory or batch.fallback_reason or batch.unknown_page_ids or audit_due:
        # A reset cursor must point to the beginning of the inventory window.
        # An edit that lands while the inventory is running will then remain in
        # the next activity overlap instead of being skipped by a post-scan
        # "now" timestamp.
        from datetime import datetime, timezone

        inventory_anchor_at = datetime.now(timezone.utc).isoformat()
        inventory = detector.inventory(publisher.client, ledger.published_pages)
        batch.selected_page_ids.update(inventory.selected_page_ids)
        store = HumanStore(project)
        for page in inventory.discovered_pages:
            stamps = _page_stamps(page.body)
            marker = stamps[0].group("id") if len(stamps) == 1 else ""
            baseline = store.page(marker) if marker else {}
            local_path = str(baseline.get("local_path") or "")
            if marker and local_path and store.prepared_match(marker, page):
                ledger.published_pages[local_path] = {
                    "growi_path": page.path,
                    "page_id": page.page_id,
                    "revision_id": str(baseline.get("prepared_revision") or ""),
                    "marker_id": marker,
                    "marker_seed": local_path,
                }
                batch.selected_page_ids.add(page.page_id)
            elif marker:
                reason = "unledgered owned page has no exact prepared publication evidence"
                if local_path:
                    row = {"marker_id": marker, "page_id": page.page_id,
                           "growi_path": page.path, "revision_id": ""}
                    store.block_page(local_path, row, reason, page)
                batch.metrics.setdefault("inventory_blocks", 0)
                batch.metrics["inventory_blocks"] += 1
                discovery_failures.append(f"inventory: {page.path}: {reason}")
        batch.metrics.update({f"inventory_{key}": value for key, value in inventory.metrics.items()})
    pulled, failures, blocked = _capture_remote(
        project, ledger, publisher, page_ids=batch.selected_page_ids
    ) if batch.selected_page_ids else ([], [], set())
    failures.extend(discovery_failures)
    if inventory is not None:
        ambiguous = [row for row in inventory.classifications
                     if row["classification"] in {"ambiguous", "duplicate_ownership"}]
        failures.extend(
            f"inventory: {row['classification']}: {row.get('path') or row.get('marker_id') or ''}"
            for row in ambiguous
        )
    reported_fallback = batch.fallback_reason
    if not failures:
        resettable = {
            "endpoint_changed", "malformed_cursor", "sequence_gap", "cursor_too_old",
            "activity_pagination_gap", "clock_skew", "malformed_activity_response",
            "activity_permission_gap",
        }
        if inventory is not None and batch.fallback_reason in resettable:
            batch.cursor = {
                "schema_version": 1,
                "endpoint_identity": detector.endpoint_identity,
                "last_processed_at": inventory_anchor_at,
                "ids_at_last_timestamp": [],
                "recent_ids": [],
                "last_sequence": None,
            }
            batch.fallback_reason = ""
        if not batch.cursor:
            batch.cursor = detector.cursor()
            batch.cursor["endpoint_identity"] = detector.endpoint_identity
        if inventory is not None:
            from datetime import datetime, timezone

            batch.cursor["last_inventory_at"] = datetime.now(timezone.utc).isoformat()
    metrics = {
        **batch.metrics,
        "fallback_reason": reported_fallback,
        "forced_inventory": force_inventory,
        "audit_due": audit_due,
        "audit_interval_seconds": audit_seconds,
        "inventory_performed": inventory is not None,
        "authoritative_pages_fetched": len(batch.selected_page_ids),
        "classifications": inventory.classifications if inventory is not None else [],
        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    }
    cursor_commit = (detector, batch) if not failures and not batch.fallback_reason and batch.cursor else None
    return pulled, failures, blocked, metrics, cursor_commit


def _published_page_row(project: Project, publisher: Any, path: str, page: Any) -> dict[str, Any]:
    row = {"growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
           "marker_id": publisher.page_marker_id(project, path),
           "marker_seed": publisher.page_marker_seed(project, path) if hasattr(publisher, "page_marker_seed") else path}
    if isinstance(publisher, GrowiPublisher):
        from graph.growi.client import managed_page_markdown
        from .human_changes import HumanStore

        remote = managed_page_markdown(page.body, row["marker_id"])
        if remote is None:
            raise ValueError(f"published page returned no ownership marker: {path}")
        local = (project.wiki / path).read_text(encoding="utf-8")
        HumanStore(project).remember_page(path, row, remote, local, published=True)
    return row


def _publish_sweep(
    project: Project,
    ledger: Ledger,
    publisher: GrowiPublisher | None,
    run_id: str,
    *,
    only: set[str] | None = None,
    only_pages: set[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    settings: Any | None = None,
    captured: bool = False,
    allow_unlinked: bool = False,
    link_pending: bool = True,
) -> list[str]:
    from .index import build_index, delete_document_index  # index imports this module

    failures: list[str] = []
    if settings is not None and not getattr(settings, "wiki_linker_enabled", True):
        rels = [rel for rel in _wiki_raw_rels(project) if only is None or rel in only]
        run_linkers(project, rels, settings=settings, llm=None, embedder=None)
    folders = _folders(project, allow_unlinked=allow_unlinked)
    if publisher is not None:
        _assert_unique_growi_locations(project, set(folders), publisher)
    scoped_documents = (
        {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
        if only is not None
        else None
    )
    if scoped_documents is not None:
        folders = {document: folder for document, folder in folders.items() if document in scoped_documents}
    blocked: set[str] = set()
    known_pages = dict(ledger.published_pages)
    if publisher is not None:
        try:
            if not captured:
                pulled, conflicts, blocked = _capture_remote(
                    project, ledger, publisher, only=only, on_progress=on_progress
                )
                failures.extend(conflicts)
                if pulled:
                    if only_pages is not None:
                        only_pages = set(only_pages) | set(pulled)
                if not conflicts and settings is not None and link_pending:
                    pending = _pending_link_rels(project, settings, sorted(only) if only is not None else None)
                    if pending:
                        run_linkers(project, pending, settings=settings, llm=None, embedder=None, on_progress=on_progress)
                folders = _folders(project, allow_unlinked=allow_unlinked)
                if scoped_documents is not None:
                    folders = {doc: folder for doc, folder in folders.items() if doc in scoped_documents}
            known_pages = dict(ledger.published_pages)
            if hasattr(publisher, "assert_known_revisions"):
                publisher.assert_known_revisions({path: row for path, row in known_pages.items()
                                                 if Path(path).parent.as_posix() in set(folders) - blocked})
        except Exception as exc:
            blocked = set(folders)
            log.exception("run=%s stage=preflight failed", run_id)
            failures.append(f"pull: {type(exc).__name__}: {exc}")
    documents = [
        (document, folder, _document_raw_rel(document))
        for document, folder in sorted(folders.items())
        if document not in blocked
    ]
    not_ready = {project.wiki_dir(rel).relative_to(project.wiki).as_posix()
                 for rel in _wiki_raw_rels(project)
                 if project.wiki_dir(rel).is_dir() and (only is None or rel in only)} - set(folders) - blocked
    failures.extend(f"{document}: linker output is not ready" for document in sorted(not_ready))
    if publisher is not None:
        started = time.monotonic()
        try:
            if documents and begin_publish is not None:
                begin_publish()
            publish_args = {
                "known_pages": known_pages,
                **({"only_pages": only_pages} if only_pages is not None else {}),
            }
            if on_progress is not None and isinstance(publisher, GrowiPublisher):
                publish_args["on_progress"] = on_progress
            pages = publisher.publish_documents(
                project,
                [raw_rel for _, _, raw_rel in documents],
                **publish_args,
            )
            published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for document, folder, raw_rel in documents:
                ledger.published_documents[document] = {
                    "content_sha256": _content_hash(folder),
                    "growi_path": publisher.doc_path(project, raw_rel),
                    "raw_rel": raw_rel,
                    "published_at": published_at,
                }
            updated_pages = {path: _published_page_row(project, publisher, path, page) for path, page in pages.items()}
            if only_pages is None:
                published_prefixes = tuple(document.rstrip("/") + "/" for document, _, _ in documents)
                ledger.published_pages = {
                    path: row for path, row in ledger.published_pages.items()
                    if not path.startswith(published_prefixes)
                }
            ledger.published_pages.update(updated_pages)
            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
            if on_progress:
                on_progress({
                    "stage": "growi-publish",
                    "step": "batch_done",
                    "pages": len(pages),
                    "documents": len(documents),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                })
        except Exception as exc:
            failures.append(f"publish: {type(exc).__name__}: {exc}")
            log.exception("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
    for document, row in list(ledger.published_documents.items()):
        if scoped_documents is not None and document not in scoped_documents:
            continue
        if (project.wiki / document).is_dir() or publisher is None:
            continue
        raw_rel = _document_raw_rel(document)
        try:
            if begin_publish is not None:
                begin_publish()
            prefix = document.rstrip("/") + "/"
            if hasattr(publisher, "assert_known_revisions"):
                publisher.assert_known_revisions({
                    path: page for path, page in ledger.published_pages.items() if path.startswith(prefix)
                })
            delete_args = {"known_pages": {path: page for path, page in ledger.published_pages.items() if path.startswith(prefix)}} if isinstance(publisher, GrowiPublisher) else {}
            publisher.delete_document(project, raw_rel, **delete_args)
            delete_document_index(publisher, document)
            ledger.published_documents.pop(document, None)
            ledger.published_pages = {
                path: row for path, row in ledger.published_pages.items() if not path.startswith(prefix)
            }
        except Exception as exc:
            log.exception("run=%s document=%s stage=delete failed", run_id, document)
            failures.append(f"{document}: {type(exc).__name__}: {exc}")
    if publisher is not None and settings is not None and not failures:
        # Index pages are derived: they follow a batch that published cleanly, and a
        # failure here is logged rather than failed, because rolling back a published
        # document over a table of contents is worse. A failed batch indexes nothing and
        # the retry indexes it once.
        try:
            for problem in build_index(
                settings,
                only=sorted(only) if only is not None else None,
                locked=True,
                ledger=ledger,
                on_progress=on_progress,
            )["failures"]:
                log.warning("run=%s stage=index error=%s", run_id, problem,
                            extra={"failure_summary": f"index: {problem}"})
        except Exception as exc:
            log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc,
                        exc_info=True, extra={"failure_summary": f"index: {type(exc).__name__}: {exc}"})
    return failures


def _remove_sources(project: Project, ledger: Ledger, sources: dict[str, str]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    done: list[dict[str, Any]] = []
    touched_raw: set[str] = set()
    failures: list[str] = []
    for rel, raw_rel in sources.items():
        try:
            source = dict(ledger.sources.get(rel) or {})
            from .human_changes import HumanStore

            # Deleting the document deletes its Appendix, so human text blocks the delete.
            HumanStore(project).assert_deletable(raw_rel)
            if (project.wiki_dir(raw_rel) / "_planning" / "linker.json").exists():
                from graph.linker import remove_document
                touched = remove_document(project, raw_rel)
                touched_raw.update(touched)
            else:
                touched = []
            shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
            shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
            project.raw_file(raw_rel).unlink(missing_ok=True)
            _prune_empty_parents(project.wiki_dir(raw_rel).parent, project.wiki)
            _prune_empty_parents(project.state_dir(raw_rel).parent, project.metadata / "state")
            _prune_empty_parents(project.raw_file(raw_rel).parent, project.raw)
            ledger.sources.pop(rel, None)
            source_id = str(source.get("source_id") or "")
            if source_id:
                for stored in (project.root / "sources").glob(f"{source_id}.*"):
                    stored.unlink()
                identities = _identities(project)
                identities["active"].pop(rel, None)
                identities["tombstones"][rel] = {
                    "source_id": source_id,
                    "source_sha256": str(source.get("source_sha256") or ""),
                    "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                _save_identities(project, identities)
            done.append({
                "path": rel,
                "raw_rel": raw_rel,
                "status": "deleted",
                "touched": touched,
                "rebuild": "full",
            })
        except Exception as exc:
            log.exception("ドキュメントの削除に失敗: %s", rel)
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
    return done, touched_raw, failures


def _can_resume_parsed_source(
    previous_source: dict[str, Any],
    item: SourceFile,
    raw_path: Path,
    *,
    requested_resume: bool,
    classification: str,
    known_source_sha256: str = "",
    known_source_blob_oid: str = "",
) -> bool:
    """Reuse parsed Markdown when retrying stages for an unchanged source binary."""

    # Attempted source identity is not proof of extraction. Older successful
    # rows remain reusable; ambiguous legacy failure rows are reparsed.
    expected_sha256 = str(previous_source.get("parsed_source_sha256") or "")
    if "parsed_source_sha256" not in previous_source and not previous_source.get("last_error"):
        expected_sha256 = str(previous_source.get("source_sha256") or "")
    return bool(
        requested_resume
        and classification != "forced"
        and expected_sha256 == item.source_sha256
        and raw_path.exists()
    )


def sync_once(
    settings: Any,
    *,
    only: list[str] | None = None,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
    include_pending: bool = False,
    resume: bool | None = None,
    source_details: dict[str, dict[str, Any]] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
    retry_documents: bool = True,
    defer_linker: bool = False,
) -> dict[str, Any]:
    """One reconciliation pass, optionally scoped to mount-relative paths."""
    project = open_project(settings)
    history_enabled = (project.root / ".git").is_dir()
    ledger_path = project.metadata / "pipeline.json"
    run_id = "prun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    parser_skips: list[str] = []
    cancelled = False
    with _lock(project):
        if history_enabled:
            from .history import candidate_is_clean, last_good

            if not candidate_is_clean(project, last_good(project)):
                raise RuntimeError("cannot sync from a dirty last-good working tree")
        ledger = load_ledger(ledger_path)
        scan = scan_mount(project.mount, ledger.sources)
        assert_unique_generated_paths(scan.files)
        publisher = _publisher(settings)
        if publisher is None:
            raise RuntimeError("GROWI_URL is required for sync/watch; use build for local-only output")
        if on_revision is not None and hasattr(publisher, "on_revision"):
            publisher.on_revision = on_revision
        wanted = {rel.strip().lstrip("/") for rel in only} if only is not None else None
        scan_failures = [f"scan {rel}: {error}" for rel, error in scan.errors.items()
                         if wanted is None or rel in wanted or rel == "."]
        if scan_failures:
            return {"run_id": run_id, "done": [], "failures": scan_failures, "cancelled": False}
        if wanted is not None:
            missing = wanted - set(scan.files) - set(ledger.sources)
            if missing:
                raise FileNotFoundError(f"not under mount/: {sorted(missing)}")
        scoped_raw = {
            str(ledger.sources.get(rel, {}).get("raw_rel") or (
                _raw_rel(scan.files[rel]) if rel in scan.files else (Path(rel).parent / raw_name_for(Path(rel).name)).as_posix()
            ))
            for rel in wanted or ()
            if rel in scan.files or rel in ledger.sources
        }
        if on_progress:
            on_progress({"stage": "capture", "step": "start", "document": "<batch>", "total": 1})
        # Preserve the actual common ancestor before parse, deletion or any tier
        # changes local pages. Late remote edits are handled by revision preflight.
        captured_pages, capture_failures, _blocked = _capture_remote(
            project, ledger, publisher, only=scoped_raw if wanted is not None else None,
        )
        if on_progress:
            on_progress({
                "stage": "capture", "step": "done", "document": "<batch>",
                "current": 1, "total": 1, "pages": len(captured_pages),
            })
        save_ledger(ledger_path, ledger)
        if capture_failures:
            if history_enabled:
                from .history import checkpoint_live
                from .human_changes import HumanStore

                HumanStore(project).audit()
                checkpoint_live(project, f"capture blocked GROWI revision {run_id}")
            return {"run_id": run_id, "scan": scan, "done": [], "failures": capture_failures,
                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None,
                    "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
        touched_raw: set[str] = set()
        deleted = scan.deleted if wanted is None else sorted(set(scan.deleted) & wanted)
        removed, touched, remove_failures = _remove_sources(project, ledger, {
            rel: str(ledger.sources.get(rel, {}).get("raw_rel") or _raw_rel(SourceFile(rel, "", 0, 0, "md")))
            for rel in deleted
        })
        done.extend(removed)
        touched_raw.update(touched)
        failures.extend(remove_failures)
        if deleted:
            save_ledger(ledger_path, ledger)
        changed = set(scan.added) | set(scan.changed)
        if wanted is not None:
            changed &= wanted
            if force:
                changed |= wanted & set(scan.files)
        changed = sorted(changed)
        incremental_pages: set[str] = set(captured_pages)
        regenerated_pages: set[str] = set()
        incremental_publish = bool(changed) and not deleted
        scoped_candidates = None if include_pending else (sorted(scoped_raw) if wanted is not None else None)
        pending_before = _pending_link_rels(project, settings, scoped_candidates)
        model = _model(settings, project) if changed or pending_before else None
        try:
            embedder = Embedder(settings) if changed or pending_before else None
        except Exception as exc:
            if (str(getattr(settings, "embed_backend", "server")) == "off"
                    and isinstance(exc, ValueError)
                    and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
            else:
                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        source_details = source_details or {}
        retried: set[str] = set()
        for rel in changed:  # grows: a failed document is retried once at the end
            if should_continue is not None and not should_continue():
                cancelled = True
                break
            item = scan.files[rel]
            raw_rel = _raw_rel(item)
            details = source_details.get(rel, {})
            previous_source = dict(ledger.sources.get(rel) or {})
            classification = str(details.get("classification") or "none")
            requested_resume = not force if resume is None else resume
            if classification == "forced":
                requested_resume = False
            started = time.monotonic()
            parsed_sha256 = str(previous_source.get("parsed_source_sha256") or (
                previous_source.get("source_sha256") if not previous_source.get("last_error") else ""
            ) or "")
            try:
                log.debug("run=%s path=%s stage=parse start", run_id, rel)
                if on_progress:
                    on_progress({
                        "stage": "parse",
                        "step": "start",
                        "document": raw_rel,
                        "file": rel,
                        "parser": item.parser,
                        "bytes": item.size,
                    })
                with _progress_heartbeat(on_progress, stage="parse", file=rel, document=raw_rel):
                    raw_path = project.raw_file(raw_rel)
                    previous_markdown = (
                        raw_path.read_text(encoding="utf-8")
                        if (previous_source or details.get("source_sha256")) and raw_path.exists()
                        else None
                    )
                    if _can_resume_parsed_source(
                        previous_source,
                        item,
                        raw_path,
                        requested_resume=requested_resume,
                        classification=classification,
                        known_source_sha256=str(details.get("source_sha256") or ""),
                        known_source_blob_oid=str(details.get("source_blob_oid") or ""),
                    ):
                        assert previous_markdown is not None
                        markdown = previous_markdown
                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                        if on_progress:
                            on_progress({"stage": "parse", "step": "resumed", "document": raw_rel, "file": rel})
                    else:
                        parse_args = (
                            {"previous_markdown": previous_markdown}
                            if previous_markdown is not None
                            else {}
                        )
                        if (
                            previous_markdown is not None
                            and classification != "forced"
                            and (project.mount / rel).suffix.lower() not in VERBATIM
                        ):
                            parse_args["validate_markdown"] = lambda text: _check_parse_size(rel, previous_markdown, text)
                        markdown = _parse(item, project.mount / rel, settings, **parse_args)
                _assert_source_unchanged(item, project.mount / rel)
                if (
                    previous_markdown is not None
                    and classification != "forced"
                    and (project.mount / rel).suffix.lower() not in VERBATIM
                ):
                    _check_parse_size(rel, previous_markdown, markdown)
                if on_progress:
                    on_progress({
                        "stage": "parse",
                        "step": "done",
                        "document": raw_rel,
                        "file": rel,
                        "characters": len(markdown),
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    })
                _write_raw(project.raw_file(raw_rel), markdown)
                parsed_sha256 = item.source_sha256
                wiki_started = time.monotonic()
                if on_progress:
                    on_progress({"stage": "wiki", "step": "start", "document": raw_rel, "file": raw_rel})
                wiki_progress = None
                if on_progress:
                    def wiki_progress(event: dict[str, Any], *, _raw_rel: str = raw_rel) -> None:
                        enriched = dict(event)
                        enriched.setdefault("document", _raw_rel)
                        on_progress(enriched)
                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel, document=raw_rel):
                    result = write_wiki_pages(
                        project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                        llm=model, embedder=embedder, on_progress=wiki_progress,
                        resume=requested_resume,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                        identity_seed=str(
                            previous_source.get("id_seed")
                            or ((Path(raw_rel).parent / wiki_folder_name(Path(raw_rel).name)).as_posix() if previous_source else "")
                            or details.get("source_id")
                            or raw_rel
                        ),
                    )
                if getattr(result, "rebuild", "full") == "incremental":
                    incremental_pages.update(getattr(result, "changed_pages", []))
                    regenerated_pages.update(getattr(result, "regenerated_pages", []))
                else:
                    incremental_publish = False
                if on_progress:
                    on_progress({
                        "stage": "wiki",
                        "step": "done",
                        "document": raw_rel,
                        "file": raw_rel,
                        "touched_documents": len(result.touched),
                        "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
                    })
                ledger.sources[rel] = _source_row(item, raw_rel, details=details, previous=previous_source)
                _store_source(project, item, ledger.sources[rel], project.mount / rel)
                save_ledger(ledger_path, ledger)
                done.append({
                    "path": rel,
                    "status": "changed" if previous_source else "added",
                    "touched": result.touched,
                    "rebuild": "incremental" if getattr(result, "rebuild", "full") == "incremental" else "full",
                    "tier": getattr(result, "tier", 3),
                    "reason": getattr(result, "reason", ""),
                    "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                })
                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as exc:
                if should_continue is not None and not should_continue():
                    cancelled = True
                    break
                if isinstance(exc, ParserSkip):
                    parser_skips.append(rel)
                    log.warning("ドキュメントをスキップ: %s: %s", rel, exc, exc_info=True)
                else:
                    log.exception("run=%s path=%s stage=generate failed", run_id, rel)
                error = f"{type(exc).__name__}: {exc}"[:500]
                ledger.sources[rel] = _source_row(
                    item, raw_rel, error, details=details, previous=previous_source,
                    parsed_source_sha256=parsed_sha256,
                )
                save_ledger(ledger_path, ledger)
                if retry_documents and rel not in retried and not isinstance(exc, ParserSkip):
                    retried.add(rel)
                    changed.append(rel)
                    log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
                    continue
                failures.append(f"{rel}: {error}")
                if not isinstance(exc, ParserSkip):
                    log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
        pending_links = _pending_link_rels(project, settings, scoped_candidates)
        if should_continue is not None and not should_continue():
            cancelled = True
        if pending_links and not failures and not cancelled and not defer_linker:
            try:
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "batch_start",
                        "current": 0,
                        "total": len(pending_links),
                        "documents": len(pending_links),
                    })
                with _progress_heartbeat(on_progress, stage="linker", file="batch"):
                    touched = run_linkers(
                        project, pending_links, settings=settings, llm=model, embedder=embedder,
                        on_progress=on_progress,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                        affected_pages=incremental_pages if incremental_publish else None,
                        regenerated_pages=regenerated_pages if incremental_publish else None,
                    )
                touched_raw.update(touched)
                done.append({"path": "*", "status": "linked", "documents": len(pending_links), "touched": touched})
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "batch_done",
                        "current": len(pending_links),
                        "total": len(pending_links),
                        "touched": len(touched),
                    })
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                if should_continue is not None and not should_continue():
                    cancelled = True
                else:
                    log.exception("run=%s stage=link failed", run_id)
                    failures.append(f"link: {type(exc).__name__}: {exc}")
        if should_continue is not None and not should_continue():
            cancelled = True
        publish_only = scoped_raw | set(pending_links) | touched_raw if wanted is not None else None
        if not failures and not cancelled and not defer_linker:
            if prepare_publish is not None:
                prepare_publish()
            publish_args = {
                "only": publish_only,
                "on_progress": on_progress,
                **({"only_pages": incremental_pages} if incremental_publish else {}),
                **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                "settings": settings,
                "allow_unlinked": False,
                "link_pending": True,
            }
            failures.extend(
                _publish_sweep(project, ledger, publisher, run_id, **publish_args)
            )
        save_ledger(ledger_path, ledger)
        if history_enabled and not failures and not cancelled:
            from .history import checkpoint_live

            checkpoint_live(project, f"sync {run_id}")
    index_paths = None if publish_only is None else sorted(publish_only)
    return {
        "run_id": run_id,
        "scan": scan,
        "done": done,
        "failures": failures,
        "cancelled": cancelled,
        "index_paths": index_paths,
        **({"parser_skips": parser_skips} if parser_skips else {}),
        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    }


def delete_sources(
    settings: Any,
    sources: dict[str, str],
    *,
    should_continue: Callable[[], bool] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Idempotently remove queued source documents locally and from GROWI."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for queued deletion")
    if on_revision is not None and hasattr(publisher, "on_revision"):
        publisher.on_revision = on_revision
    ledger_path = project.metadata / "pipeline.json"
    run_id = "del-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        _pulled, capture_failures, _blocked = _capture_remote(project, ledger, publisher, only=set(sources.values()))
        save_ledger(ledger_path, ledger)
        if capture_failures:
            return {"run_id": run_id, "done": [], "failures": capture_failures,
                    "cancelled": False, "index_paths": sorted(sources.values())}
        if getattr(settings, "wiki_linker_enabled", True):
            from graph.linker.catalog import Catalog

            mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
            catalog = Catalog.open(project.linker_database, mode=mode)
            try:
                catalog.sync_from_planning(project)
            finally:
                catalog.close()
        done, touched, failures = _remove_sources(project, ledger, sources)
        save_ledger(ledger_path, ledger)
        cancelled = should_continue is not None and not should_continue()
        if not failures and not cancelled:
            if prepare_publish is not None:
                prepare_publish()
            failures.extend(_publish_sweep(
                project, ledger, publisher, run_id, only=set(sources.values()) | touched,
                settings=settings,
                captured=True,
                **({"begin_publish": begin_publish} if begin_publish is not None else {}),
            ))
        save_ledger(ledger_path, ledger)
    return {
        "run_id": run_id,
        "done": done,
        "failures": failures,
        "cancelled": cancelled,
        "index_paths": sorted(set(sources.values()) | touched),
    }


def _replace_metadata_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: item if key == "id_seed" else _replace_metadata_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_metadata_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            if value == old:
                return new
            if value.startswith(old.rstrip("/") + "/"):
                return new.rstrip("/") + value[len(old):]
        return value
    return value


def move_sources(
    settings: Any,
    jobs: list[Any],
    *,
    should_continue: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
    defer_linker: bool = False,
) -> dict[str, Any]:
    """Move generated state and GROWI pages while retaining source/page IDs."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for queued move")
    if on_revision is not None and hasattr(publisher, "on_revision"):
        publisher.on_revision = on_revision
    ledger_path = project.metadata / "pipeline.json"
    run_id = "move-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    touched_raw: set[str] = set()
    with _lock(project):
        ledger = load_ledger(ledger_path)
        capture_scope = {str(ledger.sources[str(job.from_rel)].get("raw_rel") or job.raw_rel)
                         for job in jobs if str(job.from_rel) in ledger.sources}
        _pulled, capture_failures, _blocked = _capture_remote(project, ledger, publisher, only=capture_scope)
        save_ledger(ledger_path, ledger)
        if capture_failures:
            return {"run_id": run_id, "done": [], "failures": capture_failures,
                    "cancelled": False, "index_paths": sorted(capture_scope)}
        identities = _identities(project)
        moves: list[tuple[Any, str, str, str, dict[str, dict[str, Any]]]] = []
        index_paths: set[str] = set()
        try:
            for job in jobs:
                if should_continue is not None and not should_continue():
                    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
                old_rel, new_rel = str(job.from_rel), str(job.rel)
                source = ledger.sources.get(old_rel)
                if not source:
                    failures.append(f"{old_rel}: source identity missing for move")
                    continue
                old_raw = str(source.get("raw_rel") or job.raw_rel)
                new_raw = (Path(new_rel).parent / raw_name_for(Path(new_rel).name)).as_posix()
                old_document = project.wiki_dir(old_raw).relative_to(project.wiki).as_posix()
                new_document = project.wiki_dir(new_raw).relative_to(project.wiki).as_posix()
                content_changed = bool(
                    job.target_sha256
                    and job.target_sha256 != str(source.get("source_sha256") or "")
                )
                for old_path, new_path in (
                    (project.raw_file(old_raw), project.raw_file(new_raw)),
                    (project.wiki_dir(old_raw), project.wiki_dir(new_raw)),
                    (project.state_dir(old_raw), project.state_dir(new_raw)),
                ):
                    if old_path.exists() and old_path != new_path:
                        if new_path.exists():
                            raise FileExistsError(f"move target already exists: {new_path}")
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_path), str(new_path))
                        stop = (
                            project.raw if old_path == project.raw_file(old_raw)
                            else project.wiki if old_path == project.wiki_dir(old_raw)
                            else project.metadata / "state"
                        )
                        _prune_empty_parents(old_path.parent, stop)
                replacements = {old_raw: new_raw, old_document: new_document, old_rel: new_rel}
                planning = project.wiki_dir(new_raw) / "_planning"
                for path in planning.glob("*.json") if planning.is_dir() else ():
                    data = read_json(path, default={})
                    if isinstance(data, dict):
                        data = _replace_metadata_paths(data, replacements)
                        if path.name in {"source.json", "chunks.json"}:
                            data["id_seed"] = str(source.get("id_seed") or old_document)
                        write_json_atomic(path, data)
                if defer_linker:
                    previous_linker = read_json(planning / "linker.json", default={})
                    write_json_atomic(planning / "linker.json", {
                        "schema_version": 2,
                        "status": "pending",
                        "mode": str(getattr(settings, "wiki_linker_mode", "legacy")),
                        **({"resume": True} if previous_linker.get("status") == "complete" else {}),
                    })
                source.update({
                    "mount_rel": new_rel,
                    "raw_rel": new_raw,
                    "wiki_rel": new_rel,
                })
                # A rename plus an edit still has to parse the new immutable blob.
                # Leaving the previous digest here makes sync_once see the content
                # change and prevents its resume gate from reusing the old raw text.
                if not content_changed:
                    source.update({
                        "source_sha256": str(job.target_sha256 or source.get("source_sha256") or ""),
                        "source_blob_oid": str(job.target_blob_oid or source.get("source_blob_oid") or ""),
                    })
                if (project.mount / new_rel).is_file():
                    stat = (project.mount / new_rel).stat()
                    source.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                ledger.sources.pop(old_rel, None)
                ledger.sources[new_rel] = source
                identities["active"].pop(old_rel, None)
                identities["active"][new_rel] = str(source["source_id"])
                if old_document in ledger.published_documents:
                    ledger.published_documents[new_document] = ledger.published_documents.pop(old_document)
                    ledger.published_documents[new_document]["raw_rel"] = new_raw
                old_prefix = old_document.rstrip("/") + "/"
                moved_pages: dict[str, dict[str, Any]] = {}
                for local_path, row in list(ledger.published_pages.items()):
                    if local_path.startswith(old_prefix):
                        moved_pages[new_document + local_path[len(old_document):]] = ledger.published_pages.pop(local_path)
                ledger.published_pages.update(moved_pages)
                moves.append((job, old_raw, new_raw, new_document, moved_pages))
                index_paths.update((old_raw, new_raw))
            if failures:
                return {"run_id": run_id, "done": done, "failures": failures, "cancelled": False}
            _assert_unique_growi_locations(
                project,
                set(_folders(project, allow_unlinked=defer_linker)),
                publisher,
            )
            _save_identities(project, identities)
            if getattr(settings, "wiki_linker_enabled", True) and moves and not defer_linker:
                from graph.linker.catalog import Catalog

                mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
                catalog = Catalog.open(project.linker_database, mode=mode)
                try:
                    catalog.sync_from_planning(project)
                finally:
                    catalog.close()
                touched_raw.update(run_linkers(
                    project, [new_raw for _job, _old_raw, new_raw, _document, _pages in moves],
                    settings=settings, llm=None, embedder=None, on_progress=on_progress,
                    stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                ))
            save_ledger(ledger_path, ledger)
            from .human_changes import HumanStore

            for _job, _old_raw, new_raw, _document, _pages in moves:  # human state follows the move (3.8)
                state = HumanStore(project)._dir(new_raw) / "doc.json"
                if state.exists():
                    write_json_atomic(state, {**read_json(state), "raw_rel": new_raw})
            if should_continue is not None and not should_continue():
                return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
            if prepare_publish is not None:
                prepare_publish()
            if moves and begin_publish is not None:
                begin_publish()
            for job, old_raw, new_raw, new_document, moved_pages in moves:
                remote = publisher.move_document(project, old_raw, new_raw, moved_pages)
                for local_path, page in remote.items():
                    moved_pages[local_path] = _published_page_row(project, publisher, local_path, page)
                ledger.published_pages.update(moved_pages)
                if new_document in ledger.published_documents:
                    ledger.published_documents[new_document]["growi_path"] = publisher.doc_path(project, new_raw)
                save_ledger(ledger_path, ledger)
                done.append({"path": str(job.rel), "from": str(job.from_rel), "status": "moved", "rebuild": "move"})
            if touched_raw:
                index_paths.update(touched_raw)
                failures.extend(_publish_sweep(
                    project, ledger, publisher, run_id, only=touched_raw, on_progress=on_progress,
                    settings=settings,
                    **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                ))
            if moves and not failures:
                # A pure move does not need a document publish sweep, but its old/new
                # document indexes and both ancestor trees still have to move. Indexes
                # are derived output, so keep their failures non-transactional just as
                # _publish_sweep does for add/update/delete.
                from .index import build_index

                try:
                    for problem in build_index(
                        settings,
                        only=sorted(index_paths),
                        locked=True,
                        ledger=ledger,
                        on_progress=on_progress,
                    )["failures"]:
                        log.warning("run=%s stage=index error=%s", run_id, problem,
                                    extra={"failure_summary": f"index: {problem}"})
                except Exception as exc:
                    log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc,
                                exc_info=True, extra={"failure_summary": f"index: {type(exc).__name__}: {exc}"})
            save_ledger(ledger_path, ledger)
        except Exception as exc:
            log.exception("ドキュメントの移動に失敗: %s", [(job.from_rel, job.rel) for job in jobs])
            failures.append(f"move: {type(exc).__name__}: {exc}")
    return {
        "run_id": run_id,
        "done": done,
        "failures": failures,
        "cancelled": False,
        "index_paths": sorted(index_paths),
    }


def _sources_by_id(ledger: Ledger) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        str(row.get("source_id")): (rel, row)
        for rel, row in ledger.sources.items()
        if row.get("source_id")
    }


def _affected_documents(live: Project, staged: Project, base: Ledger, candidate: Ledger) -> set[str]:
    affected: set[str] = set(base.published_documents) ^ set(candidate.published_documents)
    base_sources, candidate_sources = _sources_by_id(base), _sources_by_id(candidate)
    for source_id in set(base_sources) | set(candidate_sources):
        old = base_sources.get(source_id)
        new = candidate_sources.get(source_id)
        if old == new:
            continue
        for project, item in ((live, old), (staged, new)):
            if item is not None:
                affected.add(project.wiki_dir(str(item[1]["raw_rel"])).relative_to(project.wiki).as_posix())
    for document in set(base.published_documents) & set(candidate.published_documents):
        old, new = base.published_documents[document], candidate.published_documents[document]
        folder = staged.wiki / document
        if old.get("content_sha256") != new.get("content_sha256") or (
            folder.is_dir() and old.get("content_sha256") != _content_hash(folder)
        ):
            affected.add(document)
    return affected


def candidate_publication_complete(settings: Any, candidate_project: Project) -> bool:
    """Return true only for a committed candidate whose remote revisions match."""
    live = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        return False
    base = load_ledger(live.metadata / "pipeline.json")
    candidate = load_ledger(candidate_project.metadata / "pipeline.json")
    folders = _folders(candidate_project)
    if set(folders) != set(candidate.published_documents):
        return False
    for document, folder in folders.items():
        row = candidate.published_documents[document]
        if row.get("content_sha256") != _content_hash(folder):
            return False
        if row.get("growi_path") != publisher.doc_path(candidate_project, str(row.get("raw_rel") or _document_raw_rel(document))):
            return False
        local_pages = {path.relative_to(candidate_project.wiki).as_posix() for path in folder.glob("*.md")}
        if local_pages != {path for path in candidate.published_pages if Path(path).parent.as_posix() == document}:
            return False

    async def check() -> bool:
        for row in candidate.published_pages.values():
            if not row.get("page_id") or not row.get("revision_id") or not row.get("growi_path"):
                return False
            page = await publisher.client.get_page(page_id=str(row["page_id"]))
            if page is None or page.revision_id != row.get("revision_id") or page.path != row.get("growi_path"):
                return False
        candidate_ids = {str(row.get("page_id")) for row in candidate.published_pages.values() if row.get("page_id")}
        for row in base.published_pages.values():
            page_id = str(row.get("page_id") or "")
            if page_id and page_id not in candidate_ids and await publisher.client.get_page(page_id=page_id) is not None:
                return False
        return True

    return asyncio.run(check())


def restore_publication(
    settings: Any,
    candidate_project: Project,
    jobs: list[Any],
    *,
    known_revisions: dict[str, set[str]] | None = None,
) -> str:
    """Restore affected base documents without overwriting unknown revisions."""
    del jobs  # The two ledgers are the authoritative transaction scope.
    live = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        return
    base = load_ledger(live.metadata / "pipeline.json")
    candidate = load_ledger(candidate_project.metadata / "pipeline.json")
    affected = _affected_documents(live, candidate_project, base, candidate)
    known_revisions = known_revisions or {}
    base_by_id, candidate_by_id = _sources_by_id(base), _sources_by_id(candidate)
    candidate_only_raw = [
        str(row["raw_rel"]) for source_id, (_rel, row) in candidate_by_id.items()
        if source_id not in base_by_id
    ]
    scoped_rows = [
        row for pages in (base.published_pages, candidate.published_pages) for path, row in pages.items()
        if Path(path).parent.as_posix() in affected and row.get("page_id")
    ]
    inspected_remote: dict[str, Any] = {}
    from .human_changes import HumanStore

    candidate_store = HumanStore(candidate_project)
    candidate_store.audit()
    prepared_pages = [read_json(path) for path in (candidate_store.root / "pages").glob("*.json")]
    confirmed_deletions = {str(page["deleted_page_id"]): str(page["deleted_revision"])
                           for page in prepared_pages if page.get("deleted_page_id") and page.get("deleted_revision")}

    def matches_prepared(page: Any) -> bool:
        for prepared in prepared_pages:
            if prepared.get("prepared_path") != page.path or not prepared.get("prepared_remote_blob"):
                continue
            if prepared.get("prepared_page_id") and prepared["prepared_page_id"] != page.page_id:
                continue
            if candidate_store.get(prepared["prepared_remote_blob"]) == page.body:
                known_revisions.setdefault(page.page_id, set()).add(page.revision_id)
                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
                return True
        return False

    async def conflicts() -> list[str]:
        problems: list[str] = []
        by_id: dict[str, set[str]] = {}
        for row in scoped_rows:
            page_id = str(row["page_id"])
            by_id.setdefault(page_id, set()).add(str(row.get("revision_id") or ""))
        for page_id, revisions in known_revisions.items():
            by_id.setdefault(page_id, set()).update(revisions)
        for page_id, revisions in by_id.items():
            current = await publisher.client.get_page(page_id=page_id)
            if current is not None:
                inspected_remote[page_id] = current
                allowed_paths = {str(row.get("growi_path")) for row in scoped_rows
                                 if row.get("page_id") == page_id and row.get("growi_path")}
                if allowed_paths and current.path not in allowed_paths:
                    problems.append(f"{current.path}: GROWI page moved during publication")
            if current is None and confirmed_deletions.get(page_id) not in revisions:
                problems.append(f"{page_id}: GROWI page disappeared during publication")
            elif current is not None and current.revision_id not in revisions and not matches_prepared(current):
                problems.append(f"{current.path}: unknown GROWI revision {current.revision_id}")
        for raw_rel in candidate_only_raw:
            doc_path = publisher.doc_path(candidate_project, raw_rel)
            for listed in await publisher.client.list_all_pages(doc_path):
                if listed.path == doc_path:
                    continue
                current = await publisher.client.get_page(page_id=listed.page_id)
                revisions = by_id.get(listed.page_id, set())
                if current is not None and current.revision_id not in revisions:
                    if matches_prepared(current):
                        inspected_remote[current.page_id] = current
                    else:
                        problems.append(f"{current.path}: unknown GROWI revision {current.revision_id}")
        return problems

    problems = asyncio.run(conflicts())
    if problems:
        raise RuntimeError("rollback conflict; " + "; ".join(problems))

    # A failed candidate may have captured human edits that were absent from
    # last-good. Restore the old source with those edits still applied; restoring
    # the old visible wiki verbatim would erase them after a partial bot write.
    from .human_changes import HumanStore, LlmHumanModel

    base_rels = [str(row["raw_rel"]) for document, row in base.published_documents.items() if document in affected]
    HumanStore(live).replay_captured(candidate_project, base_rels, model=LlmHumanModel(settings, live))

    moved_candidate_docs: set[str] = set()
    for source_id in set(base_by_id) & set(candidate_by_id):
        _old_rel, old = base_by_id[source_id]
        _new_rel, new = candidate_by_id[source_id]
        old_raw, new_raw = str(old["raw_rel"]), str(new["raw_rel"])
        if old_raw == new_raw:
            continue
        new_document = candidate_project.wiki_dir(new_raw).relative_to(candidate_project.wiki).as_posix()
        pages = {
            path: row for path, row in candidate.published_pages.items()
            if Path(path).parent.as_posix() == new_document
        }
        pages = {path: {**row, "revision_id": inspected_remote[str(row["page_id"])].revision_id}
                 for path, row in pages.items() if str(row.get("page_id") or "") in inspected_remote}
        restored_move = publisher.move_document(live, new_raw, old_raw, pages)
        for page in restored_move.values():
            known_revisions.setdefault(page.page_id, set()).add(page.revision_id)
            inspected_remote[page.page_id] = page
        moved_candidate_docs.add(new_document)

    base_rels = [
        str(row["raw_rel"])
        for document, row in base.published_documents.items()
        if document in affected
    ]
    if base_rels:
        async def inspected_rows() -> dict[str, dict[str, Any]]:
            inspected = {}
            for path, row in base.published_pages.items():
                if Path(path).parent.as_posix() not in affected:
                    continue
                current = await publisher.client.get_page(page_id=str(row.get("page_id") or ""))
                if current is not None:
                    allowed = {str(row.get("revision_id") or "")}
                    allowed.update(known_revisions.get(current.page_id, set()))
                    allowed.update(str(item.get("revision_id") or "") for item in candidate.published_pages.values()
                                   if item.get("page_id") == current.page_id)
                    if current.revision_id not in allowed:
                        raise RuntimeError(f"rollback conflict; unknown GROWI revision: {current.path}")
                    inspected[path] = {**row, "revision_id": current.revision_id, "growi_path": current.path,
                                       "marker_id": publisher.page_marker_id(live, path)}
            return inspected

        cleanup = {page_id: page.revision_id for page_id, page in inspected_remote.items()}
        extra_args = {"cleanup_revisions": cleanup} if isinstance(publisher, GrowiPublisher) else {}
        restored_pages = publisher.publish_documents(live, base_rels, known_pages=asyncio.run(inspected_rows()), **extra_args)
        base_prefixes = {
            document.rstrip("/") + "/"
            for document in affected
            if document in base.published_documents
        }
        base.published_pages = {
            path: row for path, row in base.published_pages.items()
            if not path.startswith(tuple(base_prefixes))
        }
        base.published_pages.update({path: _published_page_row(live, publisher, path, page)
                                     for path, page in restored_pages.items()})
        published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for document in affected & set(base.published_documents):
            row = base.published_documents[document]
            raw_rel = str(row.get("raw_rel") or _document_raw_rel(document))
            row.update({
                "content_sha256": _content_hash(live.wiki / document),
                "growi_path": publisher.doc_path(live, raw_rel),
                "published_at": published_at,
            })
    base_source_ids = set(base_by_id)
    for source_id, (_rel, row) in candidate_by_id.items():
        document = candidate_project.wiki_dir(str(row["raw_rel"])).relative_to(candidate_project.wiki).as_posix()
        if source_id not in base_source_ids and document in affected and document not in moved_candidate_docs:
            extra_args = {"known_pages": {page_id: {"page_id": page_id, "revision_id": page.revision_id}
                                           for page_id, page in inspected_remote.items()}} if isinstance(publisher, GrowiPublisher) else {}
            publisher.delete_document(candidate_project, str(row["raw_rel"]), **extra_args)
    save_ledger(live.metadata / "pipeline.json", base)
    from .history import checkpoint_live

    return checkpoint_live(live, "restore last-good publication")


def pull_growi_once(settings: Any, *, force_inventory: bool = False) -> dict[str, Any]:
    """Pull user revisions into local wiki state without rebuilding or publishing."""
    project = open_project(settings)
    history_enabled = (project.root / ".git").is_dir()
    if history_enabled:
        from .history import candidate_is_clean, last_good

        if not candidate_is_clean(project, last_good(project), allow_initial_human_schema=True):
            raise RuntimeError("cannot pull GROWI changes into a dirty last-good working tree")
    publisher = _publisher(settings)
    if publisher is None:
        return {"run_id": "pull-disabled", "done": [], "failures": []}
    ledger_path = project.metadata / "pipeline.json"
    run_id = "pull-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        try:
            pulled, failures, _blocked, detector, cursor_commit = _capture_detected(
                project, ledger, publisher, settings, force_inventory=force_inventory
            )
            save_ledger(ledger_path, ledger)
            from .human_changes import HumanStore

            store = HumanStore(project)
            store._initialize()  # the activity cursor committed below lives in the store's folder
            detector["operator"] = store.status()["counts"]
            cursor_backup: dict[str, Any] | None = None
            cursor_existed = False
            if cursor_commit is not None:
                activity_detector, batch = cursor_commit
                cursor_existed = activity_detector.path.exists()
                cursor_backup = read_json(activity_detector.path) if cursor_existed else None
                activity_detector.commit(batch)
            if history_enabled:
                from .history import checkpoint_live

                HumanStore(project).audit()
                try:
                    checkpoint_live(project, f"pull GROWI {run_id}")
                except Exception:
                    if cursor_commit is not None:
                        if cursor_existed and cursor_backup is not None:
                            write_json_atomic(activity_detector.path, cursor_backup)
                        else:
                            activity_detector.path.unlink(missing_ok=True)
                    raise
        except Exception as exc:
            pulled = []
            failures = [f"pull: {type(exc).__name__}: {exc}"]
            detector = {"fallback_reason": "pull_exception"}
    human_sync = dict(getattr(publisher, "human_sync_summary", {}))
    if not failures:
        checked = int(detector.get("authoritative_pages_fetched", 0) or 0)
        log.info(
            "GROWIユーザー編集チェック完了: モード=%s 対象ページ=%d 取り込み=%d ブロック=%d 復旧=%d",
            human_sync.get("mode", getattr(settings, "human_sync_mode", "off")),
            checked,
            int(human_sync.get("captured", 0) or 0),
            int(human_sync.get("blocked", 0) or 0),
            int(human_sync.get("recovered", 0) or 0),
        )
        log.debug(
            "human_sync event=pull_complete run=%s checked=%d decisions=%s fallback=%s",
            run_id, checked, human_sync, detector.get("fallback_reason", ""),
        )
    return {
        "run_id": run_id,
        "done": [{"status": "pulled", "path": path} for path in pulled],
        "failures": failures,
        "human_sync": {**detector, **human_sync},
    }


def build_wiki_only(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Build raw Markdown into pristine wiki pages, leaving linking pending."""
    project = open_project(settings)
    available = set(project.raw_files())
    rels = list(dict.fromkeys(rel.strip().lstrip("/") for rel in only)) if only is not None else sorted(available)
    missing = [rel for rel in rels if rel not in available]
    if missing:
        raise FileNotFoundError(f"not under raw/: {missing}")
    pending = [rel for rel in rels if force or not wiki_up_to_date(project, rel)]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    run_id = "brun-" + uuid.uuid4().hex[:16]
    with _lock(project):
        if only is None:
            ledger = load_ledger(project.metadata / "pipeline.json")
            mount_raw = {str(row.get("raw_rel", "")) for row in ledger.sources.values()}
            for marker in sorted(project.wiki.rglob("_planning/source.json")):
                try:
                    raw_rel = str(json.loads(marker.read_text(encoding="utf-8"))["raw"])
                except (KeyError, TypeError, ValueError):
                    continue
                if raw_rel in available or raw_rel in mount_raw:
                    continue
                try:
                    from graph.linker import remove_document
                    from .human_changes import HumanStore

                    HumanStore(project).assert_deletable(raw_rel)
                    touched = remove_document(project, raw_rel)
                    shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
                    shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
                    done.append({"path": raw_rel, "status": "deleted", "touched": touched})
                except Exception as exc:
                    log.exception("ドキュメントの削除に失敗: %s", raw_rel)
                    failures.append(f"{raw_rel}: {type(exc).__name__}: {exc}")
        model = _model(settings, project) if pending else None
        for rel in rels:
            if rel not in pending:
                done.append({"path": rel, "status": "up-to-date"})
                continue
            try:
                result = write_wiki_pages(project, rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=None, on_progress=on_progress, resume=not force)
                done.append({"path": rel, "status": "built", "touched": result.touched})
            except Exception as exc:
                log.exception("ドキュメントのビルドに失敗: %s", rel)
                failures.append(f"{rel}: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures}


def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Link pending wiki documents without regenerating their pages."""
    project = open_project(settings)
    rels = list(dict.fromkeys(rel.strip().lstrip("/") for rel in only)) if only is not None else _wiki_raw_rels(project)
    missing = [rel for rel in rels if not project.wiki_dir(rel).exists()]
    if missing:
        raise FileNotFoundError(f"no wiki output for: {missing}")
    pending = _pending_link_rels(project, settings, rels, force=force)
    run_id = "lrun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    with _lock(project):
        if not pending:
            return {"run_id": run_id, "done": [{"status": "up-to-date"}], "failures": []}
        model = _model(settings, project)
        try:
            embedder = Embedder(settings)
        except Exception as exc:
            if (str(getattr(settings, "embed_backend", "server")) == "off"
                    and isinstance(exc, ValueError)
                    and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
            else:
                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        try:
            touched = run_linkers(project, pending, settings=settings, llm=model, embedder=embedder, on_progress=on_progress)
            done.append({"status": "linked", "documents": pending, "touched": touched})
        except Exception as exc:
            log.exception("ドキュメントのリンク処理に失敗: %s", pending)
            failures.append(f"link: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures}


def link_pending_isolated(
    settings: Any,
    *,
    only: list[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Link pending documents one candidate at a time, then publish their changes.

    Wiki generation has already been accepted before this phase starts.  A failed
    linker candidate therefore cannot damage the built wiki, and failures are
    retried once after every other pending document has had an attempt.
    """
    import copy

    from .history import candidate, commit_candidate, ensure_repository, last_good, promote

    project = open_project(settings)
    ensure_repository(project)
    rels = (
        list(dict.fromkeys(rel.strip().lstrip("/") for rel in only))
        if only is not None
        else _wiki_raw_rels(project)
    )
    pending = _pending_link_rels(project, settings, rels)
    run_id = "ilink-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    ledger = load_ledger(project.metadata / "pipeline.json")
    ready_documents = set(_folders(project))
    publish_scope: set[str] = {
        rel
        for rel in rels
        if project.wiki_dir(rel).is_dir()
        and project.wiki_dir(rel).relative_to(project.wiki).as_posix() in ready_documents
        and ledger.published_documents.get(
            project.wiki_dir(rel).relative_to(project.wiki).as_posix(), {}
        ).get("content_sha256") != _content_hash(project.wiki_dir(rel))
    }
    errors: dict[str, str] = {}

    if on_progress:
        on_progress({
            "stage": "linker",
            "step": "phase_start",
            "current": 0,
            "total": len(pending),
            "documents": len(pending),
        })

    for attempt in (1, 2):
        work = pending if attempt == 1 else list(errors)
        if not work:
            break
        if attempt == 2:
            errors = {}
            log.info("linker first pass complete; final retry documents=%d", len(work))
        for index, rel in enumerate(work, 1):
            operation_id = "link-" + uuid.uuid4().hex
            try:
                with _lock(project):
                    base = last_good(project)
                    with candidate(project, operation_id) as staged:
                        updates = {
                            "data_root": str(staged.root.parent),
                            "target_name": staged.root.name,
                            "mount_path": str(staged.mount),
                        }
                        if hasattr(settings, "model_copy"):
                            staged_settings = settings.model_copy(update=updates)
                        else:
                            staged_settings = copy.copy(settings)
                            for name, value in updates.items():
                                setattr(staged_settings, name, value)
                        linked = link_raw(
                            staged_settings,
                            only=[rel],
                            on_progress=on_progress,
                        )
                        if linked.get("failures"):
                            raise RuntimeError("; ".join(linked["failures"]))
                        touched = {
                            str(item)
                            for row in linked.get("done", [])
                            for item in row.get("touched", [])
                        }
                        commit = commit_candidate(
                            staged,
                            f"link {rel}",
                            {"base": base, "operation": operation_id, "raw_rel": rel, "stage": "linker"},
                        )
                        promote(project, staged, commit)
                publish_scope.update({rel, *touched})
                done.append({"path": rel, "status": "linked", "touched": sorted(touched)})
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "document_done",
                        "document": rel,
                        "current": index,
                        "total": len(work),
                        "attempt": attempt,
                    })
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                errors[rel] = error
                log.warning("linker document attempt %d failed: %s: %s", attempt, rel, error, exc_info=True)
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "document_failed",
                        "document": rel,
                        "current": index,
                        "total": len(work),
                        "attempt": attempt,
                        "error": error,
                    })

    failures = [f"{rel}: {error}" for rel, error in sorted(errors.items())]
    if publish_scope:
        published = publish_only(
            settings,
            only=sorted(publish_scope),
            allow_unlinked=False,
            link_pending=False,
        )
        done.extend(published.get("done", []))
        failures.extend(published.get("failures", []))
    if on_progress:
        on_progress({
            "stage": "linker",
            "step": "phase_done",
            "current": len(pending) - len(errors),
            "total": len(pending),
            "failures": len(errors),
        })
    return {
        "run_id": run_id,
        "done": done,
        "failures": failures,
        "failed_documents": sorted(errors),
    }


def build_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Generate the complete wiki batch first, then link it as one batch."""
    wiki = build_wiki_only(settings, only=only, force=force, on_progress=on_progress)
    project = open_project(settings)
    link_only = [rel for rel in only if project.wiki_dir(rel.strip().lstrip("/")).exists()] if only else None
    links = link_raw(settings, only=link_only, force=force, on_progress=on_progress)
    return {"run_id": wiki["run_id"], "done": [*wiki["done"], *links["done"]], "failures": [*wiki["failures"], *links["failures"]]}


def publish_only(
    settings: Any,
    *,
    only: list[str] | None = None,
    allow_unlinked: bool = True,
    link_pending: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    captured: bool = False,
) -> dict[str, Any]:
    """Publish the current wiki tree without scanning or changing mount/raw.

    Built pages publish unlinked by default. Link completion is checked only
    when explicitly requested. ``captured`` retains the caller's inspected
    revisions, so a late remote edit blocks publication instead of changing
    freshly linked output during the publish pass.
    """
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for publish")
    ledger_path = project.metadata / "pipeline.json"
    run_id = "pub-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        failures = _publish_sweep(
            project,
            ledger,
            publisher,
            run_id,
            only=set(only) if only is not None else None,
            settings=settings,
            allow_unlinked=allow_unlinked,
            link_pending=link_pending and not allow_unlinked,
            on_progress=on_progress,
            captured=captured,
        )
        if not failures:
            # The tree is live on this endpoint, so its page IDs are now the ledger's.
            ledger.endpoint = _growi_url(settings)
            ledger.marker_format = MARKER_FORMAT
        save_ledger(ledger_path, ledger)
        if (project.root / ".git").is_dir():
            # Durable state changed as soon as a page was written, so a partially
            # failed publish must still be checkpointed: leaving the tracked store
            # dirty relative to last-good blocks the pull that recovers it.
            from .history import checkpoint_live

            checkpoint_live(project, f"publish {run_id}" if not failures else f"publish {run_id} (incomplete)")
    return {"run_id": run_id, "done": [], "failures": failures,
            "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}


def republish_if_stale(settings: Any) -> dict[str, Any] | None:
    """Re-push the whole wiki tree once after the endpoint or the marker format changed.

    The queue is content-addressed, so neither change enqueues anything: every stored page
    ID can belong to another server, and every page can still carry the previous ownership
    marker. Reconciling through `publish_only` rewrites both, and leaves the ledger
    unstamped when a sweep fails so the next run retries it.
    """
    url = _growi_url(settings)
    if not url:
        return None
    project = open_project(settings)
    with _lock(project):
        ledger = load_ledger(project.metadata / "pipeline.json")
        if not ledger.published_documents:
            # Nothing published yet means nothing was pushed to another server either.
            return None
        if ledger.endpoint == url and ledger.marker_format == MARKER_FORMAT:
            return None
    return publish_only(settings)


def reset_growi(
    settings: Any,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Trash publisher-marked pages below the configured target and reset state."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for reset")
    ledger_path = project.metadata / "pipeline.json"
    with _lock(project):
        deleted = publisher.reset(on_progress=on_progress)
        from publisher.index import delete_index_pages
        delete_index_pages(settings, on_progress=on_progress)  # index pages carry no chunk marker, reset() skips them
        ledger = load_ledger(ledger_path)
        ledger.published_documents.clear()
        ledger.published_pages.clear()
        save_ledger(ledger_path, ledger)
        if (project.root / ".git").is_dir():
            from .history import checkpoint_live

            checkpoint_live(project, "reset GROWI publication")
    return {"run_id": "reset-" + uuid.uuid4().hex[:16], "done": [{"status": "reset", "deleted": deleted}], "failures": []}


__all__ = ["build_raw", "build_wiki_only", "candidate_publication_complete", "delete_sources", "link_pending_isolated", "link_raw", "move_sources", "publish_only", "pull_growi_once", "republish_if_stale", "reset_growi", "restore_publication", "sync_once"]
