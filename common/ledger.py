"""Typed access to the frozen pipeline ledger format."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import _missing_reason, read_json, write_json_atomic

log = logging.getLogger(__name__)


@dataclass
class Ledger:
    sources: dict[str, dict[str, Any]]
    published_documents: dict[str, dict[str, Any]]
    published_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    endpoint: str = ""
    marker_format: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "sources": self.sources,
            "published_documents": self.published_documents,
            "published_pages": self.published_pages,
            "endpoint": self.endpoint,
            "marker_format": self.marker_format,
        }


def load_ledger(path: Path) -> Ledger:
    target = Path(path)
    if not target.exists():
        reason = _missing_reason(target)
        if reason != "not found":
            # an empty ledger on a permission error means every document looks unpublished.
            log.error("pipeline ledger could not be read file=%s reason=%s; continuing with an empty ledger, so published documents may be republished", target, reason)
        return Ledger({}, {})
    data = read_json(target)
    if not isinstance(data, dict):
        log.error("pipeline ledger is not a json object file=%s found=%s", target, type(data).__name__)
        raise ValueError(f"invalid pipeline ledger: {target}")
    version = data.get("schema_version")
    if version not in (1, 2, 3, 4):
        log.error("unsupported pipeline ledger schema_version file=%s version=%s (supported 1-4)", target, version)
        raise ValueError(f"invalid pipeline ledger: {target}")
    return Ledger(
        dict(data.get("sources") or {}),
        dict(data.get("published_documents") or {}),
        dict(data.get("published_pages") or {}),
        str(data.get("endpoint") or ""),
        str(data.get("marker_format") or ""),
    )


def save_ledger(path: Path, ledger: Ledger) -> None:
    write_json_atomic(path, ledger.as_json())


__all__ = ["Ledger", "load_ledger", "save_ledger"]
