"""Durable activity cursor and correctness-first GROWI inventory."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from graph.growi.client import GrowiActivity, GrowiPage, _page_stamps
from graph.wiki.storage import read_json, write_json_atomic

log = logging.getLogger(__name__)

CURSOR_VERSION = 1
PAGE_ACTION_PARTS = ("page", "create", "update", "edit", "rename", "move", "delete")


def _parse_time(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _inside(path: str, boundary: str) -> bool:
    normalized = "/" + posixpath.normpath(path or "/").lstrip("/")
    root = "/" + posixpath.normpath(boundary or "/").lstrip("/")
    return root == "/" or normalized == root or normalized.startswith(root.rstrip("/") + "/")


@dataclass
class DetectionBatch:
    events: list[GrowiActivity] = field(default_factory=list)
    selected_page_ids: set[str] = field(default_factory=set)
    unknown_page_ids: set[str] = field(default_factory=set)
    cursor: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""


@dataclass
class InventoryResult:
    classifications: list[dict[str, Any]] = field(default_factory=list)
    selected_page_ids: set[str] = field(default_factory=set)
    discovered_pages: list[GrowiPage] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class ActivityDetector:
    def __init__(self, project: Any, *, endpoint: str, boundary: str, overlap_seconds: int = 60) -> None:
        self.project = project
        self.endpoint = endpoint.rstrip("/")
        self.boundary = "/" + boundary.strip("/")
        self.overlap_seconds = max(0, int(overlap_seconds))
        self.path = Path(project.metadata) / "human-sync" / "activity-cursor.json"

    @property
    def endpoint_identity(self) -> str:
        return hashlib.sha256((self.endpoint + "\0" + self.boundary).encode()).hexdigest()

    def cursor(self) -> dict[str, Any]:
        data = read_json(self.path, default={})
        if not data:
            return {
                "schema_version": CURSOR_VERSION,
                "endpoint_identity": self.endpoint_identity,
                "last_processed_at": "",
                "ids_at_last_timestamp": [],
                "recent_ids": [],
                "last_sequence": None,
            }
        if data.get("schema_version") != CURSOR_VERSION:
            raise ValueError("unsupported activity cursor schema")
        return data

    def poll(self, client: Any, owned_pages: dict[str, dict[str, Any]]) -> DetectionBatch:
        """Return a proposed cursor. The caller commits it after durable classification."""

        try:
            cursor = self.cursor()
        except (OSError, ValueError, TypeError) as exc:
            log.error("activity cursor is unusable, detection restarts from nothing path=%s reason=malformed_cursor error=%s: %s",
                      self.path, type(exc).__name__, exc, exc_info=True)
            return DetectionBatch(fallback_reason="malformed_cursor", metrics={"cursor_resets": 1})
        if cursor.get("endpoint_identity") != self.endpoint_identity:
            log.error("activity cursor belongs to another endpoint, events across the change are unprocessed path=%s reason=endpoint_changed boundary=%s",
                      self.path, self.boundary)
            return DetectionBatch(fallback_reason="endpoint_changed", metrics={"cursor_resets": 1})
        known_ids = set(cursor.get("recent_ids") or []) | set(cursor.get("ids_at_last_timestamp") or [])
        checkpoint = _parse_time(cursor["last_processed_at"]) if cursor.get("last_processed_at") else None
        floor = checkpoint - timedelta(seconds=self.overlap_seconds) if checkpoint else None
        scanned: list[GrowiActivity] = []
        offset = 0
        last_total = 0
        try:
            while True:
                rows, total = asyncio.run(client.list_activities(offset=offset, limit=100))
                last_total = total
                if not rows:
                    if offset < total:
                        log.error("activity pages stopped before the reported total, events were missed path=%s reason=activity_pagination_gap offset=%d total=%d",
                                  self.path, offset, total)
                        return DetectionBatch(fallback_reason="activity_pagination_gap",
                                              metrics={"cursor_resets": 1})
                    break
                scanned.extend(rows)
                oldest = min(_parse_time(row.created_at) for row in rows)
                offset += len(rows)
                if (floor is not None and oldest < floor) or offset >= total:
                    break
        except ValueError as exc:
            log.error("activity timestamps are unparsable, events may be missed path=%s reason=malformed_activity_response error=%s: %s",
                      self.path, type(exc).__name__, exc, exc_info=True)
            return DetectionBatch(
                fallback_reason="malformed_activity_response",
                metrics={"cursor_resets": 1, "reason": type(exc).__name__},
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            log.error("activity feed request failed, events may be missed path=%s reason=%s status=%s boundary=%s error=%s: %s",
                      self.path,
                      "activity_permission_gap" if status in {401, 403} else "activity_api_unavailable",
                      status, self.boundary, type(exc).__name__, exc, exc_info=True)
            return DetectionBatch(
                fallback_reason="activity_permission_gap" if status in {401, 403} else "activity_api_unavailable",
                metrics={"cursor_resets": 1 if status in {401, 403} else 0, "reason": type(exc).__name__},
            )
        unique = {row.activity_id: row for row in scanned}
        if checkpoint and known_ids and unique and not (set(unique) & known_ids):
            oldest_seen = min(_parse_time(row.created_at) for row in unique.values())
            if offset >= last_total and oldest_seen > checkpoint:
                log.error("activity cursor is older than the events GROWI still returns, missed events are possible path=%s reason=cursor_too_old oldest_seen=%s checkpoint=%s",
                          self.path, oldest_seen.isoformat(), checkpoint.isoformat())
                return DetectionBatch(fallback_reason="cursor_too_old", metrics={"cursor_resets": 1})
        if any(_parse_time(row.created_at) > datetime.now(timezone.utc) + timedelta(seconds=max(300, self.overlap_seconds))
               for row in unique.values()):
            log.error("activity timestamps are in the future, this host or GROWI has a broken clock path=%s reason=clock_skew boundary=%s now=%s",
                      self.path, self.boundary, datetime.now(timezone.utc).isoformat())
            return DetectionBatch(fallback_reason="clock_skew", metrics={"cursor_resets": 1})
        ordered = sorted(unique.values(), key=lambda row: (_parse_time(row.created_at), row.activity_id))
        events = [
            row for row in ordered
            if row.activity_id not in known_ids
            and (floor is None or _parse_time(row.created_at) >= floor)
            and any(part in row.action.lower() for part in PAGE_ACTION_PARTS)
        ]
        sequences = [row.sequence for row in events if row.sequence is not None]
        previous_sequence = cursor.get("last_sequence")
        if sequences and previous_sequence is not None and min(sequences) > int(previous_sequence) + 1:
            log.error("activity sequence jumped, events were missed path=%s reason=sequence_gap last_sequence=%s first_seen=%s",
                      self.path, previous_sequence, min(sequences))
            return DetectionBatch(fallback_reason="sequence_gap", metrics={"cursor_resets": 1})
        owned_ids = {str(row.get("page_id") or "") for row in owned_pages.values()}
        selected = {row.page_id for row in events if row.page_id in owned_ids}
        unknown = {row.page_id for row in events if row.page_id and row.page_id not in owned_ids}
        all_seen = list(dict.fromkeys([
            *list(cursor.get("recent_ids") or []),
            *list(cursor.get("ids_at_last_timestamp") or []),
            *(row.activity_id for row in ordered),
        ]))[-5000:]
        if unique:
            latest = max(_parse_time(row.created_at) for row in unique.values())
            latest_text = max((row.created_at for row in unique.values() if _parse_time(row.created_at) == latest), default="")
            ids_at_latest = sorted(row.activity_id for row in unique.values() if _parse_time(row.created_at) == latest)
        else:
            latest_text = str(cursor.get("last_processed_at") or "")
            ids_at_latest = list(cursor.get("ids_at_last_timestamp") or [])
        proposed = {
            "schema_version": CURSOR_VERSION,
            "endpoint_identity": self.endpoint_identity,
            "last_processed_at": latest_text,
            "ids_at_last_timestamp": ids_at_latest,
            "recent_ids": all_seen,
            "last_sequence": max(sequences) if sequences else previous_sequence,
        }
        return DetectionBatch(
            events=events,
            selected_page_ids=selected,
            unknown_page_ids=unknown,
            cursor=proposed,
            metrics={
                "events_scanned": len(scanned),
                "events_deduplicated": len(scanned) - len(unique),
                "events_new": len(events),
                "owned_candidates": len(selected),
                "unknown_candidates": len(unknown),
                "pages_fetched": 0,
                "cursor_resets": 0,
            },
        )

    def commit(self, batch: DetectionBatch) -> None:
        if batch.fallback_reason or not batch.cursor:
            return
        try:
            write_json_atomic(self.path, batch.cursor)
        except Exception as exc:  # noqa: BLE001 - the storage layer names the errno, only here knows the events at risk
            log.error("activity cursor commit failed path=%s error=%s: %s; the next poll reprocesses these events or misses them",
                      self.path, type(exc).__name__, exc, exc_info=True)
            raise

    def inventory(self, client: Any, owned_pages: dict[str, dict[str, Any]]) -> InventoryResult:
        """Compare the complete managed boundary without mutating remote state."""

        started = time.monotonic()
        try:
            remote = asyncio.run(client.list_all_pages(self.boundary))
        except Exception as exc:  # noqa: BLE001 - a partial listing cannot be mistaken for the whole boundary
            log.error("GROWI inventory listing failed boundary=%s error=%s: %s", self.boundary, type(exc).__name__, exc, exc_info=True)
            raise
        if any(not _inside(page.path, self.boundary) for page in remote):
            log.error("GROWI inventory returned a page outside the configured boundary boundary=%s pages=%d",
                      self.boundary, len(remote))
            raise ValueError("inventory returned a page outside the configured boundary")
        remote_by_id = {page.page_id: page for page in remote if page.page_id}
        owned_by_id = {str(row.get("page_id") or ""): (path, row) for path, row in owned_pages.items() if row.get("page_id")}
        result = InventoryResult()
        fetched = 0
        for page_id, (local_path, row) in sorted(owned_by_id.items()):
            page = remote_by_id.get(page_id)
            if page is None:
                kind = "deleted_owned"
                result.selected_page_ids.add(page_id)
            elif row.get("growi_path") and page.path != row["growi_path"]:
                kind = "moved_owned"
                result.selected_page_ids.add(page_id)
            elif page.revision_id != row.get("revision_id"):
                kind = "changed_owned"
                result.selected_page_ids.add(page_id)
            else:
                kind = "unchanged_owned"
            result.classifications.append({"classification": kind, "page_id": page_id,
                                           "local_path": local_path, "path": page.path if page else ""})
        marker_owners: dict[str, list[str]] = {}
        for page_id, page in sorted(remote_by_id.items()):
            if page_id in owned_by_id:
                marker = str(owned_by_id[page_id][1].get("marker_id") or "")
                if marker:
                    marker_owners.setdefault(marker, []).append(page_id)
                continue
            full = page
            if not full.body:
                try:
                    full = asyncio.run(client.get_page(page_id=page_id))
                except Exception as exc:  # noqa: BLE001 - the ownership marker of this page stays unknown
                    log.error("GROWI page fetch failed during inventory page_id=%s path=%s error=%s: %s",
                              page_id, page.path, type(exc).__name__, exc, exc_info=True)
                    raise
                fetched += 1
            stamps = _page_stamps(full.body) if full else []
            if len(stamps) == 1:
                marker = stamps[0].group("id")
                marker_owners.setdefault(marker, []).append(page_id)
                result.classifications.append({"classification": "newly_discovered_owned",
                                               "page_id": page_id, "path": page.path, "marker_id": marker})
                result.discovered_pages.append(full)
            elif len(stamps) > 1:
                result.classifications.append({"classification": "ambiguous",
                                               "page_id": page_id, "path": page.path,
                                               "reason": "multiple_ownership_markers"})
            else:
                result.classifications.append({"classification": "unmanaged_foreign",
                                               "page_id": page_id, "path": page.path})
        for marker, page_ids in marker_owners.items():
            if len(page_ids) > 1:
                result.classifications.append({"classification": "duplicate_ownership",
                                               "marker_id": marker, "page_ids": sorted(page_ids)})
        counts: dict[str, int] = {}
        for row in result.classifications:
            key = str(row["classification"])
            counts[key] = counts.get(key, 0) + 1
        result.metrics = {
            **counts,
            "pages_listed": len(remote),
            "pages_fetched": fetched,
            "inventory_duration_ms": round((time.monotonic() - started) * 1000),
        }
        return result


__all__ = ["ActivityDetector", "DetectionBatch", "InventoryResult"]
