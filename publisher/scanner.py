"""Content-addressed mount scanner."""

from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_module
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from common.paths import SUPPORTED_SOURCE_SUFFIXES as SUPPORTED

log = logging.getLogger(__name__)

IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                "__pycache__", "site-packages", "dist-packages"}


@dataclass(frozen=True)
class SourceFile:
    rel: str
    source_sha256: str
    size: int
    mtime_ns: int
    parser: str


@dataclass(frozen=True)
class Scan:
    files: dict[str, SourceFile]
    added: list[str]
    changed: list[str]
    deleted: list[str]
    errors: dict[str, str] = field(default_factory=dict)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        # Correct to drop, but until now invisible: an unreadable resolve or a symlink out of the
        # mount is the difference between "the mount is empty" and "the mount is elsewhere".
        log.debug("入力フォルダー外のため、走査対象から除外します: 入力フォルダー=%s、パス=%s", root, path)
        return False
    return True


def discover_mount(root: Path, errors: dict[str, str]) -> dict[str, os.stat_result]:
    """Report inaccessible entries; an incomplete scan must never imply deletion."""
    root = Path(root)
    if not root.is_dir():
        raise OSError(f"source mount is unavailable: {root}")
    files: dict[str, os.stat_result] = {}

    def failed(exc: OSError) -> None:
        path = Path(exc.filename or root)
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = "."
        # Callers may ignore the errors dict, so the unreadable entry is reported here.
        log.error("走査対象を読み込めません: パス=%s、理由=%s、エラー=%s: %s",
                  path, exc.strerror or "", type(exc).__name__, exc, exc_info=True)
        errors[rel] = "".join(traceback.format_exception(exc))

    for directory, dirs, names in os.walk(root, onerror=failed):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        for name in sorted(names):
            path = Path(directory) / name
            if name in IGNORED_NAMES or name.startswith("~$") or path.suffix.lower() not in SUPPORTED:
                continue
            try:
                info = path.stat()
                if stat_module.S_ISREG(info.st_mode) and _inside(path, root):
                    files[path.relative_to(root).as_posix()] = info
            except OSError as exc:
                failed(exc)
    return files


def scan_mount(root: Path, previous: dict[str, dict[str, object]] | None = None) -> Scan:
    root = Path(root)
    previous = previous or {}
    files: dict[str, SourceFile] = {}
    errors: dict[str, str] = {}
    for rel, stat in discover_mount(root, errors).items():
        path = root / rel
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            log.error("走査中にファイルを読み込めません: ファイル=%s、理由=%s、エラー=%s: %s",
                      path, exc.strerror or "", type(exc).__name__, exc, exc_info=True)
            errors[rel] = traceback.format_exc()
            continue
        suffix = path.suffix.lower()
        files[rel] = SourceFile(rel, digest, stat.st_size, stat.st_mtime_ns, "md" if suffix == ".md" else suffix[1:])
    current = set(files)
    known = set(previous)
    added = sorted(current - known)
    changed = sorted(rel for rel in current & known if str(previous[rel].get("source_sha256", "")) != files[rel].source_sha256)
    if errors:
        log.warning("走査が未完了のため、今回の削除検出を無効にします: エラー件数=%d", len(errors))
    deleted = [] if errors else sorted(known - current)
    return Scan(files, added, changed, deleted, errors)


__all__ = ["Scan", "SourceFile", "SUPPORTED", "scan_mount"]
