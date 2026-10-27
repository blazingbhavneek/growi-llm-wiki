"""Markdown: use its heading tree only when the document is truly sectioned."""

from __future__ import annotations

from typing import Any, Sequence

from graph.wiki.schemas import CompiledSeedPlan


def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
    from .tree import heading_tree, heading_tree_usable

    try:
        tree = heading_tree(lines)
    except ValueError:
        return None
    if not heading_tree_usable(
        tree,
        line_count=len(lines),
        max_depth=6,
        collapse_title=True,
    ):
        return None
    from . import docx

    return docx.plan(lines, config=config)
