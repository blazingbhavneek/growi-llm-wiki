"""Format-specific wiki planning and tabular output."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

KINDS = {"docx", "pptx", "xlsx", "csv", "pdf", "md"}
TABULAR = {"xlsx", "csv"}


def kind_of(document_name: str) -> str:
    stem = PurePosixPath(document_name).stem
    _base, sep, ext = stem.rpartition("_")
    ext = ext.lower()
    aliases = {"xlsm": "xlsx", "xls": "xlsx", "doc": "docx", "txt": "pdf"}
    return aliases.get(ext, ext) if sep and (ext in KINDS or ext in aliases) else "md"


def is_tabular(kind: str) -> bool:
    return kind in TABULAR


def supports_page_updates(kind: str) -> bool:
    """Formats whose wiki pages can be patched or regenerated one at a time."""

    return not is_tabular(kind)


async def structural_seed_plan(
    lines: list[str], *, kind: str, config: Any, model: Any,
    on_progress=None, stop_check=None,
):
    if kind == "docx":
        from . import docx

        return docx.plan(lines, config=config)
    if kind == "pptx":
        from . import pptx

        return await pptx.plan(
            lines, config=config, model=model, on_progress=on_progress, stop_check=stop_check
        )
    if kind == "pdf":
        from . import pdf

        return pdf.plan(lines, config=config)
    if kind == "md":
        from . import md

        return md.plan(lines, config=config)
    return None
