"""Atomic state for the no-Git publisher."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph.wiki.storage import read_json, write_json_atomic

log = logging.getLogger(__name__)


@dataclass
class Ledger:
    sources: dict[str, dict[str, Any]]
    published_documents: dict[str, dict[str, Any]]
    published_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The GROWI endpoint every stored page ID was resolved against.
    endpoint: str = ""
    # The ownership-marker format every published page carries (see graph.growi.client).
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
    if not Path(path).exists():
        return Ledger({}, {})
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2, 3, 4):
        log.error("pipeline ledger has no usable schema file=%s found=%r expected=1,2,3,4",
                  path, data.get("schema_version") if isinstance(data, dict) else type(data).__name__)
        raise ValueError(f"invalid pipeline ledger: {path}")
    sources = data.get("sources", {})
    published = data.get("published_documents", {})
    pages = data.get("published_pages", {})
    if not isinstance(sources, dict) or not isinstance(published, dict) or not isinstance(pages, dict):
        log.error("pipeline ledger maps are not objects file=%s sources=%s published_documents=%s published_pages=%s",
                  path, type(sources).__name__, type(published).__name__, type(pages).__name__)
        raise ValueError(f"invalid pipeline ledger maps: {path}")
    return Ledger(
        dict(sources),
        dict(published),
        dict(pages),
        str(data.get("endpoint") or ""),
        str(data.get("marker_format") or ""),
    )


def save_ledger(path: Path, ledger: Ledger) -> None:
    try:
        write_json_atomic(path, ledger.as_json())
    except Exception as exc:  # noqa: BLE001 - the caller cannot tell a lost state write from a saved one
        log.error("publisher state write failed file=%s error=%s: %s; the next run may create duplicate or orphan pages",
                  path, type(exc).__name__, exc, exc_info=True)
        raise


__all__ = ["Ledger", "load_ledger", "save_ledger"]
