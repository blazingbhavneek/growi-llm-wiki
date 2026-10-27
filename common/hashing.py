"""Stable hashes for persisted artifacts."""

from __future__ import annotations

import hashlib


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hash(text: str) -> str:
    return sha256_text(text)


def short_hash(text: str, length: int = 12) -> str:
    return sha256_text(text)[:length]


__all__ = ["sha256_text", "short_hash", "source_hash"]
