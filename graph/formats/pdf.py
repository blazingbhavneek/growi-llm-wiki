"""pdf: headings are opt-in because MinerU headings are not reliable."""

from __future__ import annotations

from typing import Any, Sequence

from graph.wiki.schemas import CompiledSeedPlan


def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
    if not getattr(config, "pdf_use_headings", False):
        return None
    from .tree import heading_tree, heading_tree_usable

    try:
        tree = heading_tree(lines)
    except ValueError:
        return None
    if not heading_tree_usable(tree, line_count=len(lines)):
        return None
    from . import docx

    return docx.plan(lines, config=config)
