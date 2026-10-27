"""Heading tree to safe, near-target seed ranges."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Sequence

from graph.common.markdown import scan_markdown_fences
from graph.wiki.markdown_blocks import BlockIndex
from graph.wiki.wire import SeedRange

HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")


@dataclass
class Section:
    level: int
    title: str
    start: int
    end: int
    children: list["Section"] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def heading_tree(lines: Sequence[str], *, title: str = "document") -> Section:
    fenced: set[int] = set()
    scan = scan_markdown_fences(list(lines))
    if scan.unclosed is not None:
        raise ValueError(f"unclosed fence at line {scan.unclosed.line_number}")
    for opening, closing in zip(scan.openings, scan.closings):
        fenced.update(range(opening.line_number, closing.line_number + 1))
    root = Section(0, title, 1, len(lines))
    stack = [root]
    for number, line in enumerate(lines, start=1):
        if number in fenced:
            continue
        match = HEADING_RE.match(line)
        if not match:
            continue
        level, text = len(match.group(1)), match.group(2).strip()
        if not text:
            continue
        while stack[-1].level >= level:
            stack.pop()
        node = Section(level, text, number, len(lines))
        stack[-1].children.append(node)
        stack.append(node)
    _close(root, len(lines))
    return root


def _close(node: Section, end: int) -> None:
    node.end = end
    for index, child in enumerate(node.children):
        child_end = node.children[index + 1].start - 1 if index + 1 < len(node.children) else end
        _close(child, child_end)


def heading_tree_usable(
    tree: Section,
    *,
    line_count: int,
    max_depth: int = 3,
    collapse_title: bool = False,
) -> bool:
    """Return whether headings are frequent and balanced enough to plan from."""

    candidate = tree
    if (
        collapse_title
        and len(tree.children) == 1
        and tree.children[0].start == 1
        and tree.children[0].end == line_count
    ):
        candidate = tree.children[0]
    return not (
        _count(candidate) < line_count / 300
        or _depth(candidate) > max_depth
        or any(child.size > 0.6 * line_count for child in candidate.children)
    )


def _count(node: Section) -> int:
    return len(node.children) + sum(_count(child) for child in node.children)


def _depth(node: Section) -> int:
    return 1 + max((_depth(child) for child in node.children), default=0) if node.children else 0


def lead(lines: Sequence[str], start: int, end: int, *, limit: int = 200) -> str:
    out: list[str] = []
    for line in lines[start - 1 : end]:
        text = line.strip()
        if not text or text.startswith("#") or text.startswith(("<", "|", "![")):
            continue
        out.append(text)
        if sum(len(item) for item in out) >= limit:
            break
    return " ".join(out)[:limit] or "（本文なし）"


def divisor_split(start: int, end: int, *, target: int, index: BlockIndex) -> list[tuple[int, int]]:
    size = end - start + 1
    d = 2
    while math.ceil(size / d) > target:
        d += 1
    pieces: list[tuple[int, int]] = []
    cursor = start
    for part in range(1, d + 1):
        if part == d:
            pieces.append((cursor, end))
            break
        ideal = start + round(size * part / d)
        cut = index.nearest_safe_cut(ideal, backsearch=target // 4)
        cut = max(cursor + 1, min(cut, end))
        pieces.append((cursor, cut - 1))
        cursor = cut
    return [(s, e) for s, e in pieces if e >= s]


def pages_for(
    node: Section, *, lines: Sequence[str], index: BlockIndex,
    target: int, min_lines: int, path: tuple[str, ...] = (),
) -> list[SeedRange]:
    chain = path + ((node.title,) if node.level > 0 else ())
    if node.size <= target:
        return [_page(node.title, node.start, node.end, chain, lines)]
    if not node.children:
        parts = divisor_split(node.start, node.end, target=target, index=index)
        return [_page(f"{node.title}（{i}/{len(parts)}）", s, e, chain, lines) for i, (s, e) in enumerate(parts, 1)]
    items: list[list[SeedRange]] = []
    preamble_end = node.children[0].start - 1
    if preamble_end >= node.start:
        if preamble_end - node.start + 1 <= target:
            items.append([_page(node.title, node.start, preamble_end, chain, lines)])
        else:
            parts = divisor_split(node.start, preamble_end, target=target, index=index)
            items.append([_page(f"{node.title}（{i}/{len(parts)}）", s, e, chain, lines) for i, (s, e) in enumerate(parts, 1)])
    for child in node.children:
        items.append(pages_for(child, lines=lines, index=index, target=target, min_lines=min_lines, path=chain))
    return _pack(items, target=target, min_lines=min_lines, merge=node.level >= 1)


def _page(title: str, start: int, end: int, chain: tuple[str, ...], lines: Sequence[str]) -> SeedRange:
    return SeedRange(title=title, summary=lead(lines, start, end), chapter=" › ".join(chain), source_start=start, source_end=end, path=list(chain))


def _pack(items: list[list[SeedRange]], *, target: int, min_lines: int, merge: bool = True) -> list[SeedRange]:
    # ponytail: greedy left-to-right packing; use a planner only if real documents need optimal packing.
    out: list[SeedRange] = []
    pending: SeedRange | None = None

    def size(page: SeedRange) -> int:
        return page.source_end - page.source_start + 1

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            out.append(pending)
            pending = None

    for group in items:
        if len(group) > 1:
            if pending is not None and size(pending) < min_lines:
                group[0] = group[0].model_copy(update={"source_start": pending.source_start})
                pending = None
            flush()
            out.extend(group)
            continue
        page = group[0]
        if pending is None:
            pending = page
        elif size(pending) + size(page) <= target and (merge or size(pending) < min_lines):
            pending = pending.model_copy(update={
                "title": pending.title if size(page) < min_lines else f"{pending.title} 〜 {page.title}",
                "source_end": page.source_end,
            })
        else:
            flush()
            pending = page
    flush()
    return out
