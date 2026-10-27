"""Persistent, coalescing work queue for immutable mount snapshots."""

from __future__ import annotations

import copy
import hashlib
import io
import logging
import sqlite3
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from docx import Document
from docx.table import Table

from graph.workspace.project import Project, assert_unique_generated_paths, open_project, raw_name_for
from graph.wiki.incremental import line_hunks
from graph.wiki.storage import read_json

from .history import (
    amend_candidate, candidate, candidate_is_clean, candidate_project, commit_candidate, ensure_repository, is_ancestor, last_good, list_candidate_ids, promote,
    prune_candidates, read_blob, remove_candidate, reopen_candidate, restore_last_good, resumed_candidate, stage_blob,
)
from .ledger import load_ledger
from .scanner import discover_mount

log = logging.getLogger(__name__)

SMALL_DOCUMENT_LINES = 1000
SMALL_DOCUMENT_RATIO = 0.25
LARGE_DOCUMENT_RATIO = 0.10
MOVE_SIMILARITY = 0.90


@dataclass(frozen=True)
class Job:
    rel: str
    raw_rel: str
    operation: str
    lane: str
    version: int
    token: str
    source_id: str = ""
    from_rel: str = ""
    target_blob_oid: str = ""
    target_sha256: str = ""
    classification: str = "none"
    base_commit: str = ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    present = _columns(conn, table)
    for name, definition in definitions.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@contextmanager
def _connect(project: Project):
    conn = sqlite3.connect(project.queue_database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                rel TEXT PRIMARY KEY,
                raw_rel TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                source_sha256 TEXT NOT NULL DEFAULT '',
                blob_oid TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS jobs (
                rel TEXT PRIMARY KEY,
                raw_rel TEXT NOT NULL,
                operation TEXT NOT NULL,
                lane TEXT NOT NULL CHECK(lane IN ('fast','slow')),
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','failed')),
                token TEXT NOT NULL DEFAULT '',
                available_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                from_rel TEXT NOT NULL DEFAULT '',
                target_blob_oid TEXT NOT NULL DEFAULT '',
                target_sha256 TEXT NOT NULL DEFAULT '',
                classification TEXT NOT NULL DEFAULT 'none',
                base_commit TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, lane, available_at, created_at);
            CREATE TABLE IF NOT EXISTS transactions (
                operation_id TEXT PRIMARY KEY,
                candidate_commit TEXT NOT NULL DEFAULT '',
                base_commit TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transaction_revisions (
                operation_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                PRIMARY KEY(operation_id,page_id,revision_id)
            );
            """
        )
        _add_columns(conn, "sources", {
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "source_sha256": "TEXT NOT NULL DEFAULT ''",
            "blob_oid": "TEXT NOT NULL DEFAULT ''",
        })
        _add_columns(conn, "jobs", {
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "from_rel": "TEXT NOT NULL DEFAULT ''",
            "target_blob_oid": "TEXT NOT NULL DEFAULT ''",
            "target_sha256": "TEXT NOT NULL DEFAULT ''",
            "classification": "TEXT NOT NULL DEFAULT 'none'",
            "base_commit": "TEXT NOT NULL DEFAULT ''",
        })
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _snapshot(root: Path, errors: dict[str, str] | None = None) -> dict[str, tuple[str, int, int]]:
    files: dict[str, tuple[str, int, int]] = {}
    for rel, stat in discover_mount(root, errors if errors is not None else {}).items():
        raw_rel = (Path(rel).parent / raw_name_for(Path(rel).name)).as_posix()
        files[rel] = (raw_rel, stat.st_size, stat.st_mtime_ns)
    assert_unique_generated_paths(files)
    return files


def _docx_text(payload: bytes) -> list[str]:
    document = Document(io.BytesIO(payload))
    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells = ["\n".join(p.text for p in cell.paragraphs if p.text.strip()) for cell in row.cells]
                if any(cells):
                    lines.append("\t".join(cells))
            continue
        if block.text.strip():
            lines.append(block.text)
    return lines


def _classification(
    old: bytes,
    new: bytes,
    suffix: str,
) -> dict[str, Any]:
    if suffix != ".docx":
        return {"kind": "unknown", "ratio": 0.0, "hunks": 0, "reason": "no-quick-classifier"}
    try:
        old_lines, new_lines = _docx_text(old), _docx_text(new)
        hunks = line_hunks(old_lines, new_lines)
        changed_lines = sum(max(old_len, new_len) for _o, old_len, _n, new_len in hunks)
        ratio = changed_lines / max(len(old_lines), len(new_lines), 1)
        threshold = SMALL_DOCUMENT_RATIO if max(len(old_lines), len(new_lines)) < SMALL_DOCUMENT_LINES else LARGE_DOCUMENT_RATIO
        kind = "large" if ratio > threshold else "small"
        return {"kind": kind, "ratio": round(ratio, 6), "hunks": len(hunks), "reason": "ratio" if kind == "large" else "below-threshold"}
    except Exception as exc:
        return {"kind": "large", "ratio": 1.0, "hunks": 0, "reason": f"classification-failed:{type(exc).__name__}"}


def _docx_similarity(old: bytes, new: bytes) -> float:
    try:
        return SequenceMatcher(None, _docx_text(old), _docx_text(new), autojunk=False).ratio()
    except Exception:
        return 0.0


def _enqueue(
    conn: sqlite3.Connection,
    rel: str,
    raw_rel: str,
    operation: str,
    now: float,
    settle_seconds: float,
    *,
    source_id: str = "",
    from_rel: str = "",
    target_blob_oid: str = "",
    target_sha256: str = "",
    classification: str = "none",
    base_commit: str = "",
) -> None:
    lane = "fast" if operation == "delete" else "slow"
    available_at = now if lane == "fast" else now + settle_seconds
    statement = """INSERT INTO jobs(rel,raw_rel,operation,lane,available_at,created_at,updated_at,
                                     source_id,from_rel,target_blob_oid,target_sha256,classification,base_commit)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(rel) DO UPDATE SET raw_rel=excluded.raw_rel,
                      operation=excluded.operation,lane=excluded.lane,version=jobs.version+1,
                      status='queued',token='',available_at=excluded.available_at,
                      updated_at=excluded.updated_at,error='',source_id=excluded.source_id,
                      from_rel=excluded.from_rel,target_blob_oid=excluded.target_blob_oid,
                      target_sha256=excluded.target_sha256,classification=excluded.classification,
                      base_commit=excluded.base_commit"""
    values = (
        rel, raw_rel, operation, lane, available_at, now, now, source_id, from_rel,
        target_blob_oid, target_sha256, classification, base_commit,
    )
    try:
        conn.execute(statement, values)
    except sqlite3.IntegrityError:
        if operation != "move":
            raise
        # Legacy queues may still constrain operation to add/update/delete.
        conn.execute(statement, values[:2] + ("update",) + values[3:])


def _source_identity(
    project: Project,
    ledger: Any,
    rel: str,
    previous: sqlite3.Row | None = None,
    digest: str = "",
) -> str:
    identities = read_json(project.metadata / "source-identities.json", default={})
    tombstone = dict((identities.get("tombstones") or {}).get(rel) or {})
    restored = str(tombstone.get("source_id") or "") if digest and tombstone.get("source_sha256") == digest else ""
    return str(
        (previous["source_id"] if previous is not None and previous["source_id"] else "")
        or ledger.sources.get(rel, {}).get("source_id")
        or restored
        or uuid.uuid4()
    )


def scan(
    settings: Any,
    *,
    only: list[str] | None = None,
    settle_seconds: float = 10.0,
    force: bool = False,
    verify_content: bool = False,
) -> dict[str, Any]:
    """Stat the mount, stage changed bytes once, and coalesce desired state."""
    project = open_project(settings)
    base_commit = ensure_repository(project)
    errors: dict[str, str] = {}
    current = _snapshot(project.mount, errors)
    wanted = {item.strip().lstrip("/") for item in only or ()}
    if wanted:
        current = {rel: row for rel, row in current.items() if rel in wanted}
    now = time.time()
    result: dict[str, Any] = {"added": [], "updated": [], "deleted": [], "cancelled": [], "moved": [], "classification": {}, "errors": errors}
    ledger = load_ledger(project.metadata / "pipeline.json")
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = {
            str(row["rel"]): row
            for row in conn.execute("SELECT * FROM sources")
            if not wanted or str(row["rel"]) in wanted
        }
        previous_ids = {str(row["source_id"]) for row in previous.values() if row["source_id"]}
        for rel, source in ledger.sources.items():
            if (
                rel in previous or (wanted and rel not in wanted)
                or str(source.get("source_id") or "") in previous_ids
            ):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO sources
                   (rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)""",
                (
                    rel, str(source.get("raw_rel") or raw_name_for(Path(rel).name)),
                    int(source.get("size") or 0), int(source.get("mtime_ns") or 0),
                    str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                    str(source.get("source_blob_oid") or ""),
                ),
            )
            previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
        ledger_rel_by_id = {
            str(source.get("source_id")): rel
            for rel, source in ledger.sources.items()
            if source.get("source_id")
        }
        for rel, row in list(previous.items()):
            source = ledger.sources.get(rel, {})
            if source and (not row["source_id"] or not row["source_sha256"] or not row["blob_oid"]):
                conn.execute(
                    "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                    (
                        str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                        str(source.get("source_blob_oid") or ""), rel,
                    ),
                )
                previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
        queued = {str(row[0]) for row in conn.execute("SELECT rel FROM jobs")}
        changed_paths = {
            rel for rel, (_raw, size, mtime_ns) in current.items()
            if rel not in previous or force or verify_content
            or (int(previous[rel]["size"]), int(previous[rel]["mtime_ns"])) != (size, mtime_ns)
        }
        staged: dict[str, Any] = {}
        for rel in sorted(changed_paths):
            try:
                staged[rel] = stage_blob(project, project.mount / rel)
            except (FileNotFoundError, OSError, RuntimeError):
                errors[rel] = traceback.format_exc()
                continue

        disappeared = set() if errors else set(previous) - set(current)
        appeared = set(current) - set(previous)
        old_by_hash: dict[str, list[str]] = {}
        new_by_hash: dict[str, list[str]] = {}
        for rel in disappeared:
            digest = str(previous[rel]["source_sha256"] or ledger.sources.get(rel, {}).get("source_sha256") or "")
            if digest:
                old_by_hash.setdefault(digest, []).append(rel)
        for rel in appeared:
            if rel in staged:
                new_by_hash.setdefault(staged[rel].sha256, []).append(rel)
        moves: dict[str, str] = {}
        for digest in set(old_by_hash) & set(new_by_hash):
            if len(old_by_hash[digest]) == len(new_by_hash[digest]) == 1:
                old_rel, new_rel = old_by_hash[digest][0], new_by_hash[digest][0]
                source_id = str(previous[old_rel]["source_id"] or "")
                if source_id in ledger_rel_by_id and Path(old_rel).suffix.lower() == Path(new_rel).suffix.lower():
                    moves[old_rel] = new_rel
        fuzzy: dict[str, list[str]] = {}
        reverse_fuzzy: dict[str, list[str]] = {}
        for old_rel in disappeared - set(moves):
            old_oid = str(previous[old_rel]["blob_oid"] or ledger.sources.get(old_rel, {}).get("source_blob_oid") or "")
            source_id = str(previous[old_rel]["source_id"] or "")
            if source_id not in ledger_rel_by_id or Path(old_rel).suffix.lower() != ".docx" or not old_oid:
                continue
            old_payload = read_blob(project, old_oid)
            for new_rel in appeared - set(moves.values()):
                if Path(new_rel).suffix.lower() != ".docx" or new_rel not in staged:
                    continue
                if _docx_similarity(old_payload, read_blob(project, staged[new_rel].oid)) >= MOVE_SIMILARITY:
                    fuzzy.setdefault(old_rel, []).append(new_rel)
                    reverse_fuzzy.setdefault(new_rel, []).append(old_rel)
        for old_rel, candidates in fuzzy.items():
            if len(candidates) == 1 and len(reverse_fuzzy.get(candidates[0], [])) == 1:
                moves[old_rel] = candidates[0]
        for old_rel, new_rel in sorted(moves.items()):
            old = previous[old_rel]
            blob = staged[new_rel]
            source_id = _source_identity(project, ledger, old_rel, old, blob.sha256)
            origin = ledger_rel_by_id.get(source_id, old_rel)
            base_source = ledger.sources.get(origin, {})
            raw_rel = current[new_rel][0]
            conn.execute("DELETE FROM sources WHERE rel=?", (old_rel,))
            conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (old_rel, source_id))
            conn.execute(
                "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                (new_rel, raw_rel, current[new_rel][1], current[new_rel][2], source_id, blob.sha256, blob.oid),
            )
            old_oid = str(base_source.get("source_blob_oid") or old["blob_oid"] or "")
            decision = _classification(
                read_blob(project, old_oid), read_blob(project, blob.oid), Path(new_rel).suffix.lower(),
            )
            if new_rel == origin:
                if blob.sha256 == str(base_source.get("source_sha256") or ""):
                    result["cancelled"].append(old_rel)
                else:
                    _enqueue(conn, new_rel, raw_rel, "update", now, settle_seconds, source_id=source_id,
                             target_blob_oid=blob.oid, target_sha256=blob.sha256,
                             classification=str(decision["kind"]), base_commit=base_commit)
                    result["updated"].append(new_rel)
                    result["classification"][new_rel] = decision
                continue
            _enqueue(conn, new_rel, raw_rel, "move", now, settle_seconds, source_id=source_id,
                     from_rel=origin, target_blob_oid=blob.oid, target_sha256=blob.sha256,
                     classification=str(decision["kind"]), base_commit=base_commit)
            result["moved"].append({"from": old_rel, "to": new_rel})
            if blob.sha256 != str(old["source_sha256"] or ""):
                result["classification"][new_rel] = decision

        moved_old, moved_new = set(moves), set(moves.values())
        for rel, (raw_rel, size, mtime_ns) in current.items():
            if rel in moved_new:
                continue
            old = previous.get(rel)
            blob = staged.get(rel)
            content_changed = bool(blob and blob.sha256 != str(old["source_sha256"] if old is not None else ""))
            changed = old is None or force or content_changed
            pending_delete = conn.execute("SELECT lane FROM jobs WHERE rel=?", (rel,)).fetchone()
            if blob is not None and not changed and pending_delete is not None and pending_delete["lane"] == "fast":
                # The source returned before its deletion was accepted.
                conn.execute("DELETE FROM jobs WHERE rel=?", (rel,))
                queued.discard(rel)
                result["cancelled"].append(rel)
            if changed and blob is None:
                continue
            if changed:
                source_id = _source_identity(project, ledger, rel, old, blob.sha256)
                origin = ledger_rel_by_id.get(source_id, "")
                operation = "move" if origin and origin != rel else "update" if old is not None else "add"
                classification = {"kind": "none", "ratio": 0.0, "hunks": 0, "reason": "new"}
                if force and old is not None:
                    classification = {"kind": "forced", "ratio": 0.0, "hunks": 0, "reason": "forced"}
                    result["classification"][rel] = classification
                elif operation in {"update", "move"} or old is not None:
                    active = conn.execute("SELECT target_blob_oid,status FROM jobs WHERE rel=?", (rel,)).fetchone()
                    old_oid = str(
                        (active["target_blob_oid"] if active is not None and active["status"] == "running" else "")
                        or ledger.sources.get(origin or rel, {}).get("source_blob_oid")
                        or (old["blob_oid"] if old is not None else "")
                    )
                    classification = _classification(
                        read_blob(project, old_oid), read_blob(project, blob.oid), Path(rel).suffix.lower(),
                    )
                    result["classification"][rel] = classification
                _enqueue(conn, rel, raw_rel, operation, now, settle_seconds, source_id=source_id,
                         from_rel=origin if operation == "move" else "",
                         target_blob_oid=blob.oid, target_sha256=blob.sha256,
                         classification=str(classification["kind"]), base_commit=base_commit)
                result["updated" if old is not None else "added"].append(rel)
                conn.execute(
                    "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                    (rel, raw_rel, size, mtime_ns, source_id, blob.sha256, blob.oid),
                )
            else:
                if blob is not None:
                    conn.execute(
                        "UPDATE sources SET size=?,mtime_ns=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (size, mtime_ns, blob.sha256, blob.oid, rel),
                    )
                if rel in queued:
                    continue
                source = ledger.sources.get(rel, {})
                if source and (not old["source_id"] or not old["source_sha256"] or not old["blob_oid"]):
                    conn.execute(
                        "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (
                            str(source.get("source_id") or ""),
                            str(source.get("source_sha256") or ""),
                            str(source.get("source_blob_oid") or ""),
                            rel,
                        ),
                    )
                document = project.wiki_dir(raw_rel).relative_to(project.wiki).as_posix()
                linker_status = str(read_json(
                    project.wiki_dir(raw_rel) / "_planning" / "linker.json",
                    default={},
                ).get("status") or "")
                built_locally = linker_status in {
                    "pending", "failed", "render_pending", "complete", "disabled",
                }
                incomplete = (
                    not source or bool(source.get("last_error")) or not project.raw_file(raw_rel).exists()
                    or not project.wiki_dir(raw_rel).exists()
                    or (document not in ledger.published_documents and not built_locally)
                )
                if incomplete:
                    blob_oid = str(old["blob_oid"] or "")
                    digest = str(old["source_sha256"] or "")
                    if not blob_oid:
                        try:
                            staged_blob = stage_blob(project, project.mount / rel)
                        except (FileNotFoundError, OSError, RuntimeError):
                            errors[rel] = traceback.format_exc()
                            continue
                        blob_oid, digest = staged_blob.oid, staged_blob.sha256
                    operation = "update" if source else "add"
                    _enqueue(conn, rel, raw_rel, operation, now, settle_seconds,
                             source_id=_source_identity(project, ledger, rel, old, digest), target_blob_oid=blob_oid,
                             target_sha256=digest, base_commit=base_commit)
                    result["updated" if source else "added"].append(rel)

        for rel in sorted((set() if errors else set(previous) - set(current)) - moved_old):
            old = previous[rel]
            source_id = _source_identity(project, ledger, rel, old)
            origin = ledger_rel_by_id.get(source_id, rel)
            raw_rel = str(ledger.sources.get(origin, {}).get("raw_rel") or old["raw_rel"])
            conn.execute("DELETE FROM sources WHERE rel=?", (rel,))
            job = conn.execute("SELECT operation,lane FROM jobs WHERE rel=?", (rel,)).fetchone()
            known = origin in ledger.sources or any(
                str(row.get("raw_rel", "")) == raw_rel for row in ledger.published_documents.values()
            )
            if job is not None and str(job["lane"]) == "slow" and not known:
                conn.execute("DELETE FROM jobs WHERE rel=?", (rel,))
                result["cancelled"].append(rel)
            elif known:
                pending_delete = conn.execute("SELECT lane,source_id FROM jobs WHERE rel=?", (origin,)).fetchone()
                if pending_delete is not None and pending_delete["lane"] == "fast" and pending_delete["source_id"] == source_id:
                    # Repeated scans of an unchanged absence must preserve the
                    # failed/running version instead of requeueing it forever.
                    continue
                conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (rel, source_id))
                _enqueue(conn, origin, raw_rel, "delete", now, settle_seconds,
                         source_id=source_id, base_commit=base_commit)
                result["deleted"].append(rel)
    return result


def _job_from_row(row: sqlite3.Row) -> Job:
    operation = "move" if row["from_rel"] else str(row["operation"])
    return Job(
        str(row["rel"]), str(row["raw_rel"]), operation, str(row["lane"]), int(row["version"]),
        str(row["token"]), str(row["source_id"]), str(row["from_rel"]), str(row["target_blob_oid"]),
        str(row["target_sha256"]), str(row["classification"]), str(row["base_commit"]),
    )


def _resumable_transaction(project: Project) -> dict[str, Any] | None:
    """Oldest interrupted building/prepared transaction usable for --continue.

    A transaction qualifies only when its worktree directory still exists on
    disk and its base is still the current last-good (no promotion happened
    since the interruption).
    """
    try:
        base = last_good(project)
    except Exception:
        return None
    with _connect(project) as conn:
        rows = list(conn.execute("SELECT * FROM transactions ORDER BY started_at"))
    for row in rows:
        data = dict(row)
        if str(data.get("phase") or "") not in {"building", "prepared"}:
            continue
        if str(data.get("base_commit") or "") != base:
            continue
        try:
            exists = candidate_project(project, str(data.get("operation_id") or "")).root.exists()
        except (ValueError, OSError):
            continue
        if exists:
            return data
    return None


def _adopt_resumable_orphan(project: Project) -> str | None:
    """Recover a dirty same-base candidate whose transaction row was lost.

    Older workers removed the transaction row after a pre-publication pipeline
    failure even though ``keep=True`` retained the worktree.  On an explicit
    ``--continue``, adopt the newest such worktree when work for the same base
    is still pending.  Successful candidates are clean and stale-base
    candidates are rejected, so neither is mistaken for resumable progress.
    """
    base = last_good(project)
    with _connect(project) as conn:
        if conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None:
            return None
        pending = conn.execute(
            """SELECT 1 FROM jobs
               WHERE status IN ('queued','running','failed') AND base_commit=? LIMIT 1""",
            (base,),
        ).fetchone()
    if pending is None:
        return None
    candidates: list[tuple[int, str]] = []
    for operation_id in list_candidate_ids(project):
        try:
            staged = candidate_project(project, operation_id)
            head = subprocess.run(
                ["git", "-C", str(staged.root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if head != base or candidate_is_clean(staged, base):
                continue
            candidates.append((staged.root.stat().st_mtime_ns, operation_id))
        except (OSError, subprocess.CalledProcessError, ValueError):
            continue
    if not candidates:
        return None
    operation_id = max(candidates)[1]
    _transaction(project, operation_id, base, "building")
    return operation_id


def _prune_orphan_candidates(project: Project, *, keep: str | None = None) -> None:
    """Remove candidate dirs that no transaction row references.

    Successful batches leave their kept worktree behind (deferred deletion);
    the next iteration discards them here.  The resumable ``keep`` id and any
    dir still referenced by the transactions table are preserved.
    """
    with _connect(project) as conn:
        live = {str(row[0]) for row in conn.execute("SELECT operation_id FROM transactions")}
    for operation_id in list_candidate_ids(project):
        if operation_id == keep or operation_id in live:
            continue
        try:
            remove_candidate(project, operation_id)
        except (ValueError, OSError):
            continue


def resumable_operation_id(project: Project, *, adopt_orphan: bool = False) -> str | None:
    """Operation id of the kept candidate ``--continue`` would resume, if any."""
    row = _resumable_transaction(project)
    if row is None and adopt_orphan and _adopt_resumable_orphan(project) is not None:
        row = _resumable_transaction(project)
    return str(row["operation_id"]) if row is not None else None


def recover(project: Project, settings: Any | None = None, *, preserve_operation_id: str | None = None) -> int:
    """Restore interrupted publication, then return claimed work to the queue."""
    ensure_repository(project)
    with _connect(project) as conn:
        transactions = list(conn.execute("SELECT * FROM transactions ORDER BY started_at"))
        jobs = [_job_from_row(row) for row in conn.execute("SELECT * FROM jobs")]
    if any(str(row["phase"]) == "publishing" for row in transactions) and settings is None:
        raise RuntimeError("publisher settings are required to recover an interrupted GROWI publication")
    if transactions:
        from .pipeline import candidate_publication_complete, restore_publication

        for transaction in transactions:
            operation_id = str(transaction["operation_id"])
            commit = str(transaction["candidate_commit"] or "")
            phase = str(transaction["phase"])
            staged = candidate_project(project, operation_id)
            if phase == "restored":
                if not commit or last_good(project) != commit:
                    raise RuntimeError(f"cannot recover publication {operation_id}: restored commit is missing")
                _finish_transaction(project, operation_id)
                continue
            if phase == "restoring" and last_good(project) != str(transaction["base_commit"]):
                if not candidate_publication_complete(settings, project):
                    raise RuntimeError(f"cannot recover publication {operation_id}: restored state is incomplete")
                _finish_transaction(project, operation_id)
                continue
            if commit and last_good(project) == commit:
                if staged.root.exists():
                    promote(project, staged, commit)
                else:
                    restore_last_good(project)
                with _connect(project) as conn:
                    conn.execute(
                        "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                        (str(transaction["base_commit"]),),
                    )
                _finish_transaction(project, operation_id)
                continue
            if phase in {"building", "prepared"} and last_good(project) != str(transaction["base_commit"]):
                # Nothing of this operation reached GROWI, and last-good moved on (a sync
                # stopped mid-document, then `publish` checkpointed). Its work is either
                # already in last-good (promoted before the stop) or simply built again.
                if commit and is_ancestor(project, commit, last_good(project)):
                    with _connect(project) as conn:
                        conn.execute(
                            "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                            (str(transaction["base_commit"]),),
                        )
                _finish_transaction(project, operation_id)
                continue
            if last_good(project) != str(transaction["base_commit"]):
                # A publishing operation may have written pages from that older base.
                raise RuntimeError(f"cannot recover publication {operation_id}: last-good changed")
            if phase in {"publishing", "restoring"}:
                if not staged.root.exists():
                    if not commit:
                        raise RuntimeError(f"cannot recover publication {operation_id}: candidate commit is missing")
                    staged = reopen_candidate(project, operation_id, commit)
                if commit and candidate_is_clean(staged, commit) and candidate_publication_complete(settings, staged):
                    promote(project, staged, commit)
                    with _connect(project) as conn:
                        conn.execute(
                            "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                            (str(transaction["base_commit"]),),
                        )
                else:
                    _transaction(project, operation_id, str(transaction["base_commit"]), "restoring", commit)
                    restored = restore_publication(
                        settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                    )
                    _transaction(project, operation_id, str(transaction["base_commit"]), "restored", restored)
            if not (preserve_operation_id is not None
                    and operation_id == preserve_operation_id
                    and phase in {"building", "prepared"}):
                _finish_transaction(project, operation_id)
    if preserve_operation_id is not None:
        # Deferred deletion: keep the resumable worktree, discard only orphans.
        _prune_orphan_candidates(project, keep=preserve_operation_id)
    else:
        prune_candidates(project)
    if settings is not None and getattr(settings, "wiki_linker_enabled", True) and not project.linker_database.exists():
        from graph.linker.catalog import Catalog

        catalog = Catalog.open(project.linker_database, mode=str(getattr(settings, "wiki_linker_mode", "legacy")))
        try:
            catalog.sync_from_planning(project)
        finally:
            catalog.close()
    with _connect(project) as conn:
        preserved_base = ""
        if preserve_operation_id is not None:
            row = conn.execute(
                "SELECT base_commit FROM transactions WHERE operation_id=?",
                (preserve_operation_id,),
            ).fetchone()
            preserved_base = str(row[0]) if row is not None else ""
        if preserved_base:
            cursor = conn.execute(
                """UPDATE jobs SET status='queued',token='',error='',updated_at=?
                   WHERE status='running'
                      OR (status='failed' AND error LIKE 'recovery required:%')
                      OR (status='failed' AND base_commit=?)""",
                (time.time(), preserved_base),
            )
        else:
            cursor = conn.execute(
                """UPDATE jobs SET status='queued',token='',error='',updated_at=?
                   WHERE status='running' OR (status='failed' AND error LIKE 'recovery required:%')""",
                (time.time(),),
            )
    return cursor.rowcount


# Claim order: everything the parser finishes quickly (txt, md, docx, pptx, Excel, csv)
# before pdfs, smallest first within each group, so the builder starts early and the
# slow pdf parses run ahead in the background (publisher/ahead.py) in the same order.
_PRIORITY = "CASE WHEN lower(jobs.rel) LIKE '%.pdf' THEN 1 ELSE 0 END, COALESCE(sources.size, 0), jobs.created_at, jobs.rel"


def queued_jobs(project: Project, *, only: list[str] | None = None) -> list[Job]:
    """Queued document jobs in claim order, read-only (the parse-ahead worker's list)."""
    with _connect(project) as conn:
        rows = [dict(row) for row in conn.execute(
            f"""SELECT jobs.rel,jobs.raw_rel,jobs.operation,jobs.lane,jobs.version,jobs.source_id,jobs.from_rel,
                       jobs.target_blob_oid,jobs.target_sha256,jobs.classification,jobs.base_commit
                FROM jobs LEFT JOIN sources ON sources.rel=jobs.rel
                WHERE jobs.status='queued' AND jobs.lane='slow' ORDER BY {_PRIORITY}"""
        )]
    wanted = {item.strip().lstrip("/") for item in only or ()}
    return [_job_from_row(row | {"token": ""}) for row in rows if not wanted or row["rel"] in wanted]


def claim(project: Project, lane: str, *, limit: int | None = None, only: list[str] | None = None,
          exclude: list[str] | None = None) -> list[Job]:
    token = uuid.uuid4().hex
    now = time.time()
    base_commit = last_good(project)
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = list(conn.execute(
            f"""SELECT jobs.rel,jobs.raw_rel,CASE WHEN jobs.from_rel<>'' THEN 'move' ELSE jobs.operation END AS operation,
                       jobs.lane,jobs.version,jobs.source_id,jobs.from_rel,jobs.target_blob_oid,jobs.target_sha256,
                       jobs.classification,jobs.base_commit
                FROM jobs LEFT JOIN sources ON sources.rel=jobs.rel
                WHERE jobs.status='queued' AND jobs.lane=? AND jobs.available_at<=? ORDER BY {_PRIORITY}""",
            (lane, now),
        ))
        wanted = {item.strip().lstrip("/") for item in only or ()}
        if wanted:
            rows = [row for row in rows if row["rel"] in wanted or row["from_rel"] in wanted]
        rows = [row for row in rows if not any(
            rel == "." or row["rel"] == rel or row["rel"].startswith(rel.rstrip("/") + "/")
            for rel in exclude or ()
        )]
        if limit is not None:
            blocked = list(conn.execute("SELECT rel,source_id FROM jobs WHERE lane='fast' AND status='failed'"))
            rows = [row for row in rows if not any(
                row["rel"] == failed["rel"] or (row["source_id"] and row["source_id"] == failed["source_id"])
                for failed in blocked
            )]
            rows = rows[:limit]
        if rows:
            conn.executemany(
                "UPDATE jobs SET status='running',token=?,base_commit=?,updated_at=? WHERE rel=? AND version=? AND status='queued'",
                [(token, base_commit, now, row["rel"], row["version"]) for row in rows],
            )
    return [_job_from_row(dict(row) | {"token": token, "base_commit": base_commit}) for row in rows]


def supersession(project: Project, jobs: list[Job]) -> Literal["continue", "cancel"]:
    if not jobs:
        return "continue"
    with _connect(project) as conn:
        for job in jobs:
            row = conn.execute(
                "SELECT version,status,token,operation,from_rel,classification FROM jobs WHERE rel=?", (job.rel,)
            ).fetchone()
            if row is None:
                return "cancel"
            if int(row["version"]) == job.version and row["status"] == "running" and row["token"] == job.token:
                continue
            operation = "move" if row["from_rel"] else str(row["operation"])
            if job.operation in {"delete", "move"} or operation != "update" or str(row["classification"]) in {"large", "forced"}:
                return "cancel"
    return "continue"


def current(project: Project, jobs: list[Job]) -> bool:
    return supersession(project, jobs) == "continue"


def finish(project: Project, jobs: list[Job], *, error: str = "", retry: bool = False) -> None:
    status_value = "queued" if retry else "failed"
    with _connect(project) as conn:
        for job in jobs:
            if error:
                conn.execute(
                    "UPDATE jobs SET status=?,token='',updated_at=?,error=? WHERE rel=? AND version=? AND token=?",
                    (status_value, time.time(), error[:1000], job.rel, job.version, job.token),
                )
            else:
                conn.execute("DELETE FROM jobs WHERE rel=? AND version=? AND token=?", (job.rel, job.version, job.token))


def retry_failed(project: Project, *, only: list[str] | None = None,
                 versions: dict[str, int] | None = None) -> int:
    with _connect(project) as conn:
        wanted = {item.strip().lstrip("/") for item in only or ()}
        rows = list(conn.execute("SELECT rel,from_rel,version FROM jobs WHERE status='failed'"))
        count = 0
        for row in rows:
            rel = str(row["rel"])
            if wanted and rel not in wanted and row["from_rel"] not in wanted:
                continue
            if versions is not None and versions.get(rel) != int(row["version"]):
                continue
            count += conn.execute(
                "UPDATE jobs SET status='queued',error='',available_at=?,updated_at=? WHERE rel=? AND version=? AND status='failed'",
                (time.time(), time.time(), rel, row["version"]),
            ).rowcount
        return count


def status(project: Project) -> list[dict[str, Any]]:
    with _connect(project) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT rel,raw_rel,CASE WHEN from_rel<>'' THEN 'move' ELSE operation END AS operation,
                      lane,status,version,error,source_id,from_rel,target_sha256,classification,base_commit
               FROM jobs ORDER BY CASE lane WHEN 'fast' THEN 0 ELSE 1 END,created_at,rel"""
        )]


def _candidate_settings(settings: Any, staged: Project) -> Any:
    updates = {"data_root": str(staged.root.parent), "target_name": staged.root.name, "mount_path": str(staged.mount)}
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    clone = copy.copy(settings)
    for name, value in updates.items():
        setattr(clone, name, value)
    return clone


def _commit_metadata(staged: Project, jobs: list[Job], base: str, operation_id: str) -> dict[str, str]:
    output = hashlib.sha256()
    for path in sorted(staged.wiki.rglob("*")) if staged.wiki.exists() else ():
        if path.is_file():
            output.update(path.relative_to(staged.wiki).as_posix().encode())
            output.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "base": base,
        "classification": ",".join(sorted({job.classification for job in jobs})),
        "input_sha256": ",".join(job.target_sha256 for job in jobs if job.target_sha256),
        "operation": operation_id,
        "output_sha256": output.hexdigest(),
        "source_ids": ",".join(job.source_id for job in jobs if job.source_id),
    }


def _transaction(project: Project, operation_id: str, base: str, phase: str, commit: str = "") -> None:
    with _connect(project) as conn:
        conn.execute(
            """INSERT INTO transactions(operation_id,candidate_commit,base_commit,phase,started_at)
               VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET
               candidate_commit=excluded.candidate_commit,phase=excluded.phase""",
            (operation_id, commit, base, phase, time.time()),
        )


def _record_revision(project: Project, operation_id: str, page: Any) -> None:
    if not getattr(page, "page_id", "") or not getattr(page, "revision_id", ""):
        return
    with _connect(project) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO transaction_revisions(operation_id,page_id,revision_id) VALUES(?,?,?)",
            (operation_id, str(page.page_id), str(page.revision_id)),
        )


def _transaction_revisions(project: Project, operation_id: str) -> dict[str, set[str]]:
    with _connect(project) as conn:
        rows = conn.execute(
            "SELECT page_id,revision_id FROM transaction_revisions WHERE operation_id=?", (operation_id,)
        )
        revisions: dict[str, set[str]] = {}
        for row in rows:
            revisions.setdefault(str(row["page_id"]), set()).add(str(row["revision_id"]))
        return revisions


def _finish_transaction(project: Project, operation_id: str) -> None:
    with _connect(project) as conn:
        conn.execute("DELETE FROM transaction_revisions WHERE operation_id=?", (operation_id,))
        conn.execute("DELETE FROM transactions WHERE operation_id=?", (operation_id,))


def _verify_completed_jobs(staged: Project, jobs: list[Job], *, allow_unlinked: bool = False) -> None:
    """Verify either a deferred local build or a fully published result."""
    from .pipeline import _content_hash, _folders

    ledger = load_ledger(staged.metadata / "pipeline.json")
    folders = _folders(staged, allow_unlinked=allow_unlinked)
    for job in jobs:
        row = ledger.sources.get(job.rel)
        if job.operation == "delete":
            if row is not None or (job.source_id and any(
                source.get("source_id") == job.source_id for source in ledger.sources.values()
            )):
                raise RuntimeError(f"delete did not complete: {job.rel}")
            document = staged.wiki_dir(job.raw_rel).relative_to(staged.wiki).as_posix()
            if document in ledger.published_documents or any(
                path.startswith(document + "/") for path in ledger.published_pages
            ):
                raise RuntimeError(f"delete publication did not complete: {job.rel}")
            continue
        if not row or row.get("last_error") or row.get("source_id") != job.source_id or row.get("source_sha256") != job.target_sha256:
            raise RuntimeError(f"source did not complete: {job.rel}")
        document = staged.wiki_dir(job.raw_rel).relative_to(staged.wiki).as_posix()
        folder = folders.get(document)
        if allow_unlinked:
            if folder is None or not any(folder.glob("*.md")):
                raise RuntimeError(f"wiki generation did not complete: {job.rel}")
            continue
        published = ledger.published_documents.get(document, {})
        if folder is None or published.get("content_sha256") != _content_hash(folder):
            raise RuntimeError(f"wiki publication did not complete: {job.rel}")
        expected = {path.relative_to(staged.wiki).as_posix() for path in folder.glob("*.md")}
        pages = {path: page for path, page in ledger.published_pages.items() if Path(path).parent.as_posix() == document}
        if not expected or expected != set(pages) or any(
            not page.get("page_id") or not page.get("revision_id") for page in pages.values()
        ):
            raise RuntimeError(f"page publication evidence is incomplete: {job.rel}")


def _work_once_locked(settings: Any, *, on_event: Any = None, continue_run: bool = False,
                      isolated: bool = False, only: list[str] | None = None,
                      exclude: list[str] | None = None,
                      defer_linker: bool = False) -> dict[str, Any] | None:
    """Run one immutable candidate from last-good, then publish and promote it.

    With ``continue_run`` (``--continue``) an interrupted building/prepared
    candidate is reused with its LLM checkpoints intact; otherwise leftover
    worktrees are discarded up front.  Deletion is deferred: the worker keeps
    its worktree on exit (``keep=True``) and the next startup decides.
    """
    from .pipeline import delete_sources, move_sources, restore_publication, sync_once

    project = open_project(settings)
    ensure_repository(project)
    resumable_id: str | None = None
    resumable_commit = ""
    # Isolated retries always start from accepted main. A legacy interrupted
    # operation is recovered in its original scope before any candidate cleanup.
    if continue_run and not isolated:
        resumable = _resumable_transaction(project)
        if resumable is None and _adopt_resumable_orphan(project) is not None:
            resumable = _resumable_transaction(project)
        if resumable is not None:
            resumable_id = str(resumable["operation_id"])
            resumable_commit = str(resumable["candidate_commit"] or resumable["base_commit"])
    with _connect(project) as conn:
        unfinished = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None
    if unfinished:
        recover(project, settings, preserve_operation_id=resumable_id)
        if resumable_id is not None:
            refreshed = _resumable_transaction(project)
            if refreshed is None or str(refreshed["operation_id"]) != resumable_id:
                resumable_id = None
                resumable_commit = ""
            else:
                resumable_commit = str(refreshed["candidate_commit"] or refreshed["base_commit"])
    elif resumable_id is not None:
        _prune_orphan_candidates(project, keep=resumable_id)
    else:
        prune_candidates(project)
    claim_args = {"limit": 1, "only": only, "exclude": exclude} if isolated else {}
    jobs = claim(project, "fast", **claim_args)
    if not jobs:
        if not isolated:
            with _connect(project) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE lane='fast' AND status='failed' LIMIT 1").fetchone():
                    return None
        jobs = claim(project, "slow", **claim_args)
    if not jobs:
        return None
    if on_event is not None:
        on_event({"stage": "queue-claim", "lane": jobs[0].lane, "paths": [job.rel for job in jobs]})
    reuse = resumable_id is not None
    if reuse:
        assert resumable_id is not None
        operation_id = resumable_id
        base = last_good(project)
        if on_event is not None:
            on_event({"stage": "queue-resume", "operation_id": operation_id, "base_commit": base})
    else:
        operation_id = "op-" + uuid.uuid4().hex
        base = last_good(project)
        _transaction(project, operation_id, base, "building")
    is_current = lambda: supersession(project, jobs) == "continue"
    result: dict[str, Any]
    publishing = False
    try:
        if reuse:
            assert resumable_id is not None
            staged_ctx = resumed_candidate(project, operation_id, resumable_commit or base)
        else:
            staged_ctx = candidate(project, operation_id, keep=True)
        with staged_ctx as staged:
            for job in jobs:
                if job.target_blob_oid:
                    target = staged.mount / job.rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(read_blob(project, job.target_blob_oid))
            staged_settings = _candidate_settings(settings, staged)
            prepared_commit = ""

            def prepare_publish() -> None:
                nonlocal prepared_commit, publishing
                if prepared_commit:
                    return
                prepared_commit = commit_candidate(
                    staged, f"publish {operation_id}", _commit_metadata(staged, jobs, base, operation_id)
                )
                _transaction(project, operation_id, base, "prepared", prepared_commit)
                if on_event:
                    on_event({"stage": "history", "step": "candidate", "base_commit": base, "commit": prepared_commit})

            def begin_publish() -> None:
                nonlocal publishing
                if publishing:
                    return
                _transaction(project, operation_id, base, "publishing", prepared_commit)
                publishing = True

            record_revision = lambda page: _record_revision(project, operation_id, page)
            live_ledger = load_ledger(project.metadata / "pipeline.json")
            details: dict[str, dict[str, Any]] = {}
            for job in jobs:
                decision = {"kind": job.classification, "ratio": 0.0, "hunks": 0, "reason": "queued"}
                previous = live_ledger.sources.get(job.from_rel or job.rel, {})
                old_oid = str(previous.get("source_blob_oid") or "")
                if job.classification != "forced" and old_oid and job.target_blob_oid and job.operation != "move":
                    decision = _classification(
                        read_blob(project, old_oid), read_blob(project, job.target_blob_oid), Path(job.rel).suffix.lower(),
                    )
                if on_event and decision["kind"] != "none":
                    on_event({"stage": "diff-classified", "path": job.rel, "decision": decision["kind"],
                              "ratio": decision["ratio"], "hunks": decision["hunks"], "reason": decision["reason"]})
                details[job.rel] = {
                    "source_id": job.source_id,
                    "source_blob_oid": job.target_blob_oid,
                    "source_sha256": job.target_sha256,
                    "classification": decision["kind"],
                    "from_rel": job.from_rel,
                }
            if jobs[0].lane == "fast":
                result = delete_sources(staged_settings, {job.rel: job.raw_rel for job in jobs},
                                        should_continue=is_current,
                                        prepare_publish=prepare_publish, begin_publish=begin_publish,
                                        on_revision=record_revision)
            else:
                move_jobs = [job for job in jobs if job.operation == "move"]
                normal_jobs = [
                    job for job in jobs
                    if job.operation != "move"
                    or job.target_sha256 != str(live_ledger.sources.get(job.from_rel, {}).get("source_sha256") or "")
                ]
                result = {"run_id": operation_id, "done": [], "failures": [], "cancelled": False}
                if move_jobs:
                    moved = move_sources(staged_settings, move_jobs, should_continue=is_current,
                                         on_progress=on_event, prepare_publish=prepare_publish,
                                         begin_publish=begin_publish, on_revision=record_revision,
                                         defer_linker=defer_linker)
                    result["done"].extend(moved["done"])
                    result["failures"].extend(moved["failures"])
                    result["cancelled"] = moved["cancelled"]
                    result.setdefault("index_paths", []).extend(moved.get("index_paths") or [])
                if normal_jobs and not result["failures"] and not result["cancelled"]:
                    synced = sync_once(
                        staged_settings, only=[job.rel for job in normal_jobs], force=True, resume=True,
                        should_continue=is_current, include_pending=not isolated, on_progress=on_event,
                        source_details=details,
                        prepare_publish=prepare_publish,
                        begin_publish=begin_publish,
                        on_revision=record_revision,
                        retry_documents=not isolated,
                        defer_linker=defer_linker,
                    )
                    result["run_id"] = synced["run_id"]
                    result["done"].extend(synced["done"])
                    result["failures"].extend(synced["failures"])
                    if synced.get("parser_skips"):
                        result.setdefault("parser_skips", []).extend(synced["parser_skips"])
                    result["cancelled"] = synced["cancelled"]
                    result.setdefault("index_paths", []).extend(synced.get("index_paths") or [])
                result["index_paths"] = sorted(set(result.get("index_paths") or []))
            if not result.get("cancelled"):
                completed = {
                    str(row.get("path")) for row in result.get("done", [])
                    if row.get("status") in {"added", "changed", "deleted", "moved"}
                }
                omitted = sorted(job.rel for job in jobs
                                 if job.rel not in completed and job.rel not in result.get("parser_skips", []))
                if omitted:
                    result.setdefault("failures", []).append(f"pipeline omitted claimed paths: {omitted}")
            if not result.get("cancelled") and not result.get("failures"):
                if isolated:
                    _verify_completed_jobs(staged, jobs, allow_unlinked=defer_linker)
                commit = amend_candidate(staged) if prepared_commit else commit_candidate(
                    staged, f"{'build' if defer_linker else 'publish'} {operation_id}",
                    _commit_metadata(staged, jobs, base, operation_id),
                )
                _transaction(
                    project,
                    operation_id,
                    base,
                    "building" if defer_linker else "publishing",
                    commit,
                )
                promote(project, staged, commit)
                if on_event:
                    on_event({"stage": "history", "step": "promote", "base_commit": base, "commit": commit})
            elif publishing:
                _transaction(project, operation_id, base, "restoring", prepared_commit)
                restored = restore_publication(
                    settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                )
                _transaction(project, operation_id, base, "restored", restored)
                if on_event:
                    on_event({"stage": "history", "step": "rollback", "base_commit": base})
    except Exception as exc:
        log.exception("operation=%s stage=operation failed", operation_id)
        recovery_error = ""
        if publishing:
            try:
                _transaction(project, operation_id, base, "restoring", prepared_commit)
                restored = restore_publication(
                    settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                )
                _transaction(project, operation_id, base, "restored", restored)
                publishing = False
            except Exception as restore_exc:
                log.exception("operation=%s stage=recovery failed", operation_id)
                recovery_error = f"; recovery: {type(restore_exc).__name__}: {restore_exc}"
        prefix = "recovery required: " if publishing else ""
        error = f"{prefix}{type(exc).__name__}: {exc}{recovery_error}"
        finish(project, jobs, error=error)
        if not publishing:
            _finish_transaction(project, operation_id)
        return {"lane": jobs[0].lane, "jobs": len(jobs), "paths": [job.rel for job in jobs],
                "job_versions": [vars(job) for job in jobs], "operation_id": operation_id,
                "failures": [error], "cancelled": False,
                "recovery_required": publishing}
    if result.get("cancelled"):
        finish(project, jobs, error="superseded by a newer mount event", retry=True)
    elif result.get("failures"):
        finish(project, jobs, error="; ".join(result["failures"]))
    else:
        finish(project, jobs)
    _finish_transaction(project, operation_id)
    result.pop("scan", None)
    if result.get("failures") or result.get("cancelled"):
        result["done"] = []  # Generated candidate output has not been accepted.
    return {"lane": jobs[0].lane, "jobs": len(jobs), "paths": [job.rel for job in jobs],
            "job_versions": [vars(job) for job in jobs], "operation_id": operation_id, **result}


def work_once(settings: Any, *, on_event: Any = None, continue_run: bool = False,
              isolated: bool = False, only: list[str] | None = None,
              exclude: list[str] | None = None,
              defer_linker: bool = False) -> dict[str, Any] | None:
    """Promote one candidate, then reconcile derived indexes from live state."""
    from .pipeline import _lock

    with _lock(open_project(settings)):
        if isolated:
            result = _work_once_locked(settings, on_event=on_event, continue_run=continue_run,
                                       isolated=True, only=only, exclude=exclude,
                                       defer_linker=defer_linker)
        else:
            result = _work_once_locked(
                settings,
                on_event=on_event,
                continue_run=continue_run,
                defer_linker=defer_linker,
            )
        if result is None or result.get("failures") or result.get("cancelled"):
            return result
        if defer_linker:
            return result

        # metadata/index is deliberately derived and is not part of the Git commit.
        # GROWI indexes were reconciled in the candidate publication; reconcile them
        # idempotently from promoted state as well, while materializing the matching
        # local copies before another publisher can change the live tree.
        try:
            from .index import build_index

            index = build_index(
                settings,
                only=result.get("index_paths"),
                on_progress=on_event,
                locked=True,
            )
            result["index"] = {
                "updated": len(index.get("done", [])),
                "failures": list(index.get("failures", [])),
            }
        except Exception as exc:
            result["index"] = {
                "updated": 0,
                "failures": [f"index: {type(exc).__name__}: {exc}"],
            }
        return result


@contextmanager
def worker_lock(project: Project):
    import fcntl

    path = project.metadata / "watch-worker.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("queue worker already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serve(
    settings: Any,
    *,
    only: list[str] | None = None,
    interval: float = 10.0,
    growi_interval: float = 300.0,
    force: bool = False,
    on_event: Any = None,
    continue_run: bool = False,
) -> None:
    """Scan cheaply while the foreground worker runs long jobs."""
    from .pipeline import _lock, pull_growi_once, republish_if_stale

    project = open_project(settings)
    if only:
        wanted = {item.strip().lstrip("/") for item in only}
        missing = sorted(wanted - set(_snapshot(project.mount)))
        if missing:
            raise FileNotFoundError(f"not under mount/: {missing}")
    stop = threading.Event()

    def emit(kind: str, value: Any) -> None:
        if on_event is not None:
            on_event({"stage": kind, "result": value})

    def scan_loop() -> None:
        first = True
        while not stop.is_set():
            try:
                result = scan(settings, only=only, settle_seconds=interval, force=force and first)
                if first or any(result.get(key) for key in ("added", "updated", "deleted", "cancelled", "moved")):
                    emit("scan", result)
            except Exception as exc:
                emit("scan-error", f"{type(exc).__name__}: {exc}")
            first = False
            stop.wait(max(1.0, interval))

    with worker_lock(project):
        switched = republish_if_stale(settings)
        if switched is not None:
            emit("stale-republish", switched)
        with _lock(project):
            _preserved_id = resumable_operation_id(project, adopt_orphan=True) if continue_run else None
            recover(project, settings, preserve_operation_id=(
                _preserved_id
            ))
        scanner = threading.Thread(target=scan_loop, name="mount-metadata-scanner", daemon=True)
        scanner.start()
        next_growi = time.monotonic()
        try:
            while True:
                result = work_once(settings, on_event=on_event, continue_run=continue_run)
                # Only the first iteration can resume a kept worktree; later
                # batches in the same process are always fresh.
                continue_run = False
                if result is not None:
                    emit("queue", result)
                    continue
                if growi_interval > 0 and time.monotonic() >= next_growi:
                    pulled = pull_growi_once(settings)
                    emit("growi-pull", pulled)
                    if not pulled.get("failures"):
                        try:
                            from .index import build_index

                            emit("index", build_index(settings, on_progress=on_event))
                        except Exception as exc:
                            emit("index-error", f"{type(exc).__name__}: {exc}")
                    scan(settings, only=only, settle_seconds=interval, verify_content=True)
                    next_growi = time.monotonic() + growi_interval
                stop.wait(0.5)
        finally:
            stop.set()
            scanner.join(timeout=max(1.0, interval + 1.0))


__all__ = [
    "Job", "claim", "current", "finish", "recover", "resumable_operation_id", "retry_failed", "scan", "serve",
    "status", "supersession", "work_once", "worker_lock",
]
