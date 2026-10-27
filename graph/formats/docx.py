"""docx: pandoc headings define the page tree."""

from __future__ import annotations

from typing import Any, Sequence

from graph.wiki.markdown_blocks import build_block_index
from graph.wiki.schemas import CompiledSeedPlan

from .tree import heading_tree, pages_for


def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
    try:
        tree = heading_tree(lines)
    except ValueError:
        return None
    if not tree.children:
        return None
    return CompiledSeedPlan(
        summary="heading-tree plan",
        pages=pages_for(
            tree,
            lines=lines,
            index=build_block_index(list(lines)),
            target=int(config.structure_target_lines),
            min_lines=int(config.structure_min_lines),
        ),
    )
