"""Stable content IDs and safe page slugs."""

from __future__ import annotations

import re
import unicodedata

from graph.common.hashing import short_hash

from .storage import sha256_text

_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]+")
_TRAILING = re.compile(r"[.\-_\s]+$")
_COLLAPSE = re.compile(r"[\s_]+")


def document_id(source_text: str) -> str:
    return f"doc-{sha256_text(source_text)[:16]}"


def window_id(document: str, source_start: int, source_end: int) -> str:
    return f"win-{short_hash(f'{document}|window|{source_start}|{source_end}', 12)}"


def image_id(unit_hash: str, occurrence: str = "") -> str:
    return f"img-{short_hash(f'{unit_hash}|{occurrence}', 12)}"


def slugify(text: str, fallback: str = "untitled") -> str:
    text = unicodedata.normalize("NFKC", (text or "").strip())
    text = _UNSAFE.sub("-", text)
    text = _COLLAPSE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = _TRAILING.sub("", text)
    if not text or text.casefold() in {".", "..", "con", "prn", "aux", "nul"}:
        return fallback
    return text[:120]
