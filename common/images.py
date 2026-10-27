"""Image identity helpers shared by conversion and metadata code."""

from __future__ import annotations

import hashlib
from pathlib import Path


def identity(data: bytes) -> str:
    """Return the stable content identity used for an image payload."""

    return hashlib.sha256(data).hexdigest()


def identity_path(path: Path) -> str:
    return identity(path.read_bytes())


__all__ = ["identity", "identity_path"]
