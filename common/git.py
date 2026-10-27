"""Narrow, shell-free git helpers for runner transaction code."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=Path(root),
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ref_exists(root: Path, ref: str) -> bool:
    return bool(git(root, "rev-parse", "--verify", ref, check=False))


def last_good(root: Path) -> str | None:
    value = git(root, "rev-parse", "--verify", "refs/llm-wiki/last-good", check=False)
    return value or None


__all__ = ["git", "last_good", "ref_exists"]
