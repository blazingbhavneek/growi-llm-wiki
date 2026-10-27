"""Atomic state for the no-Git publisher."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph.wiki.storage import read_json, write_json_atomic


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
        raise ValueError(f"invalid pipeline ledger: {path}")
    sources = data.get("sources", {})
    published = data.get("published_documents", {})
    pages = data.get("published_pages", {})
    if not isinstance(sources, dict) or not isinstance(published, dict) or not isinstance(pages, dict):
        raise ValueError(f"invalid pipeline ledger maps: {path}")
    return Ledger(
        dict(sources),
        dict(published),
        dict(pages),
        str(data.get("endpoint") or ""),
        str(data.get("marker_format") or ""),
    )


def save_ledger(path: Path, ledger: Ledger) -> None:
    write_json_atomic(path, ledger.as_json())


__all__ = ["Ledger", "load_ledger", "save_ledger"]
