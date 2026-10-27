"""Stable hashes used by persisted factory artifacts."""

from __future__ import annotations

import hashlib


def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hash(text: str) -> str:
    return sha256_text(text)
