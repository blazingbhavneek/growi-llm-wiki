"""Canonical, atomic text and JSON storage shared by all phases."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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
    try:
        payload = json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        log.error("value could not be converted to json for hashing error=%s: %s", type(exc).__name__, exc, exc_info=True)
        raise
    return sha256_text(payload)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException as exc:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError as cleanup_exc:
                log.error("temp file left behind path=%s temp=%s error=%s: %s (the failed write below is the real error)",
                          path, temporary, type(cleanup_exc).__name__, cleanup_exc, exc_info=True)
                raise
        code = getattr(exc, "errno", None)
        reason = type(exc).__name__ if code is None else f"{type(exc).__name__} errno={code} ({os.strerror(code)})"
        log.error("atomic write failed path=%s error=%s: %s (disk full, permissions, or file locked?)",
                  path, reason, exc, exc_info=True)
        raise


def write_text_atomic(path: Path | str, text: str) -> Path:
    target = Path(path)
    _atomic_write(target, text.encode("utf-8"))
    return target


def write_json_atomic(path: Path | str, value: Any) -> Path:
    target = Path(path)
    try:
        payload = json.dumps(_jsonable(value), indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        log.error("json serialize failed path=%s error=%s: %s", target, type(exc).__name__, exc, exc_info=True)
        raise
    _atomic_write(target, (payload + "\n").encode("utf-8"))
    return target


def _missing_reason(target: Path) -> str:
    # exists() is also False for a permission problem; stat names which one this was.
    try:
        os.stat(target)
    except FileNotFoundError:
        return "not found"
    except OSError as exc:
        return f"not readable: {type(exc).__name__}: {exc}"
    return "reported missing but stat succeeded"


def read_json(path: Path | str, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        reason = _missing_reason(target)
        if default is None:
            # A missing file with no default is the documented cache-miss contract: the
            # caller that REQUIRES this file logs its own ERROR with that context.
            log.debug("json file is absent file=%s reason=%s", target, reason)
            raise FileNotFoundError(str(target))
        if reason != "not found":
            log.error("json file could not be read file=%s reason=%s; the default value was used instead", target, reason)
        return default
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("json file could not be read file=%s error=%s: %s", target, type(exc).__name__, exc, exc_info=True)
        raise
    try:
        return json.loads(text)
    except ValueError as exc:
        log.error("json file is corrupt, not missing file=%s error=%s: %s", target, type(exc).__name__, exc, exc_info=True)
        raise


def read_text(path: Path | str, default: str = "") -> str:
    target = Path(path)
    if not target.exists():
        reason = _missing_reason(target)
        if reason != "not found":
            log.error("text file could not be read file=%s reason=%s; the default value was used instead", target, reason)
        return default
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("text file could not be read file=%s error=%s: %s", target, type(exc).__name__, exc, exc_info=True)
        raise


def clean_workdir(path: Path | str) -> Path:
    target = Path(path)
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            log.error("workdir delete failed path=%s error=%s: %s (the directory may be half-deleted)",
                      target, type(exc).__name__, exc, exc_info=True)
            raise
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("workdir create failed path=%s error=%s: %s (the old contents are already deleted: half-deleted state)",
                  target, type(exc).__name__, exc, exc_info=True)
        raise
    return target


__all__ = ["clean_workdir", "hash_of", "read_json", "read_text", "sha256_text", "write_json_atomic", "write_text_atomic"]
