"""Canonical, atomic text and JSON storage shared by all phases."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_of(value: Any) -> str:
    return sha256_text(json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path | str, text: str) -> Path:
    target = Path(path)
    _atomic_write(target, text.encode("utf-8"))
    return target


def write_json_atomic(path: Path | str, value: Any) -> Path:
    target = Path(path)
    _atomic_write(target, (json.dumps(_jsonable(value), indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return target


def read_json(path: Path | str, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        if default is None:
            raise FileNotFoundError(str(target))
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def read_text(path: Path | str, default: str = "") -> str:
    target = Path(path)
    return target.read_text(encoding="utf-8") if target.exists() else default


def clean_workdir(path: Path | str) -> Path:
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


__all__ = ["clean_workdir", "hash_of", "read_json", "read_text", "sha256_text", "write_json_atomic", "write_text_atomic"]
