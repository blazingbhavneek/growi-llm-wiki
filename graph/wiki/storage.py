"""Deterministic file IO for the wiki pipeline.

Every artifact the pipeline owns is written through here so that:

* a crash never leaves a half-written JSON/JSONL file behind;
* a rerun of the same input produces byte-identical JSON/text output;
* hashes are always taken over the same canonical encoding (UTF-8, LF).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def clean_workdir(path: Path | str) -> Path:
    """A fresh, empty working directory for one worker turn."""

    directory = Path(path)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path | str, text: str) -> Path:
    path = Path(path)
    _atomic_write(path, text)
    return path


def write_json_atomic(path: Path | str, data: Any) -> Path:
    path = Path(path)
    payload = _jsonable(data)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    _atomic_write(path, text + "\n")
    return path


def read_json(path: Path | str, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        if default is None:
            raise FileNotFoundError(str(path))
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path | str, default: str = "") -> str:
    path = Path(path)
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")
