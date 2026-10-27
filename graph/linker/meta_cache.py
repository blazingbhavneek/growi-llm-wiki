"""Chunk metadata by (meta version, chunk text hash), outside the tracked project tree.

sync's link-ahead worker (publisher/ahead.py) describes built documents while later
ones are still building; link_document then reads these entries for exactly the
chunks it was going to describe, so its result is the same as describing them itself.
Off when ``settings.cache_dir`` is empty.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

from graph.wiki.storage import write_json_atomic

from .wire import ChunkMeta

log = logging.getLogger(__name__)


def _root(settings: Any, meta_version: str) -> Path | None:
    root = str(getattr(settings, "cache_dir", "") or "")
    return Path(root) / "linker" / re.sub(r"[^A-Za-z0-9._-]", "_", meta_version) if root else None


def load(settings: Any, meta_version: str, hashes: Iterable[str]) -> dict[str, ChunkMeta]:
    root = _root(settings, meta_version)
    if root is None:
        return {}
    found: dict[str, ChunkMeta] = {}
    unreadable: list[str] = []
    for digest in set(hashes):
        path = root / digest[:2] / f"{digest}.json"
        if path.exists():
            try:
                found[digest] = ChunkMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                unreadable.append(f"{type(exc).__name__}: {exc}")
                continue
    if unreadable:
        log.warning("chunk metadata cache entries unreadable root=%s count=%d first=%s (the cache rots: those chunks cost another model call)",
                    root, len(unreadable), unreadable[0][:120])
    return found


def store(settings: Any, meta_version: str, metas: dict[str, ChunkMeta]) -> None:
    root = _root(settings, meta_version)
    if root is None:
        return
    for digest, meta in metas.items():
        path = root / digest[:2] / f"{digest}.json"
        try:
            write_json_atomic(path, meta.model_dump(mode="json"))
        except Exception as exc:
            log.error("chunk metadata cache write failed path=%s error=%s: %s (the link-ahead worker dies on this error)",
                      path, type(exc).__name__, exc, exc_info=True)
            raise
