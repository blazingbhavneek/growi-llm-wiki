"""Small acceptance helper for comparing standard and fast phase runs."""

from __future__ import annotations

import hashlib
from pathlib import Path


def artifact_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(root).rglob("*"))
        if path.is_file() and ".venv" not in path.parts and "__pycache__" not in path.parts
    }


def compare_trees(standard: Path, fast: Path) -> dict[str, list[str]]:
    left, right = artifact_hashes(standard), artifact_hashes(fast)
    return {
        "only_standard": sorted(set(left) - set(right)),
        "only_fast": sorted(set(right) - set(left)),
        "different": sorted(name for name in set(left) & set(right) if left[name] != right[name]),
    }


__all__ = ["artifact_hashes", "compare_trees"]
