"""Deterministic file IO for the wiki pipeline.

Every artifact the pipeline owns is written through here so that:

* a crash never leaves a half-written JSON/JSONL file behind;
* a rerun of the same input produces byte-identical JSON/text output;
* hashes are always taken over the same canonical encoding (UTF-8, LF).
"""

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


def clean_workdir(path: Path | str) -> Path:
    """A fresh, empty working directory for one worker turn."""

    directory = Path(path)
    if directory.exists():
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            log.error("作業フォルダーを削除できません。一部だけ削除されている場合があります: パス=%s、エラー=%s: %s",
                      directory, type(exc).__name__, exc, exc_info=True)
            raise
    try:
        directory.mkdir(parents=True)
    except OSError as exc:
        log.error("作業フォルダーを作成できません。以前の内容は削除済みです: パス=%s、エラー=%s: %s",
                  directory, type(exc).__name__, exc, exc_info=True)
        raise
    return directory


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(data: Any) -> Any:
    if hasattr(data, "model_dump"):
        return _jsonable(data.model_dump(mode="json"))
    if isinstance(data, dict):
        return {str(key): _jsonable(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_jsonable(value) for value in data]
    if isinstance(data, Path):
        return str(data)
    return data


def canonical_json(data: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace differences."""

    return json.dumps(_jsonable(data), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_of(data: Any) -> str:
    return sha256_text(canonical_json(data))


# --------------------------------------------------------------------------
# Canonical source text
# --------------------------------------------------------------------------


def split_source_lines(text: str) -> list[str]:
    """Source lines without terminators; the only representation we number."""

    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def normalize_source(text: str) -> str:
    """Canonical source form used for the reconstruction gate.

    Only newline shape and the final newline are normalized. Nothing else may
    change, or the reconstruction hash would be unprovable.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def slice_text(lines: list[str], source_start: int, source_end: int) -> str:
    """Exact original bytes of an inclusive 1-based line range."""

    if source_start < 1 or source_end < source_start:
        raise ValueError(f"invalid source range {source_start}-{source_end}")
    return "\n".join(lines[source_start - 1 : source_end])


# --------------------------------------------------------------------------
# Atomic writes
# --------------------------------------------------------------------------


def _atomic_write(path: Path, payload: str | bytes) -> None:
    temporary = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        with os.fdopen(handle, "wb") as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException as exc:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError as cleanup_exc:
                log.error("一時ファイルが残っています。元の書き込みエラーは後続のログを確認してください: パス=%s、一時ファイル=%s、エラー=%s: %s",
                          path, temporary, type(cleanup_exc).__name__, cleanup_exc, exc_info=True)
                raise
        code = getattr(exc, "errno", None)
        reason = type(exc).__name__ if code is None else f"{type(exc).__name__} errno={code} ({os.strerror(code)})"
        # mkdir/mkstemp/write/flush/fsync/os.replace all land here: the errno names which step failed.
        log.error("ファイルを安全に書き込めません: パス=%s、エラー=%s: %s（容量・権限・ファイルの使用状況を確認してください）",
                  path, reason, exc, exc_info=True)
        raise


def write_text_atomic(path: Path | str, text: str) -> Path:
    path = Path(path)
    _atomic_write(path, text)
    return path


def write_json_atomic(path: Path | str, data: Any) -> Path:
    path = Path(path)
    payload = _jsonable(data)
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        log.error("JSONに変換できません: パス=%s、エラー=%s: %s", path, type(exc).__name__, exc, exc_info=True)
        raise
    _atomic_write(path, text + "\n")
    return path


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
    path = Path(path)
    if not path.exists():
        reason = _missing_reason(path)
        if default is None:
            # A missing file with no default is the documented cache-miss contract: the
            # caller that REQUIRES this file logs its own ERROR with that context.
            log.debug("JSONファイルがありません: ファイル=%s、理由=%s", path, reason)
            raise FileNotFoundError(str(path))
        if reason != "not found":
            log.error("JSONファイルを読めないため既定値を使います: ファイル=%s、理由=%s", path, reason)
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("JSONファイルを読み込めません: ファイル=%s、エラー=%s: %s", path, type(exc).__name__, exc, exc_info=True)
        raise
    try:
        return json.loads(text)
    except ValueError as exc:
        log.error("JSONファイルが破損しています: ファイル=%s、エラー=%s: %s", path, type(exc).__name__, exc, exc_info=True)
        raise


def read_text(path: Path | str, default: str = "") -> str:
    path = Path(path)
    if not path.exists():
        reason = _missing_reason(path)
        if reason != "not found":
            log.error("テキストファイルを読めないため既定値を使います: ファイル=%s、理由=%s", path, reason)
        return default
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("テキストファイルを読み込めません: ファイル=%s、エラー=%s: %s", path, type(exc).__name__, exc, exc_info=True)
        raise
