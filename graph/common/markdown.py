"""Markdown scanning and prompt/embedding sanitizers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .images import strip_images

LINKS_FOOTER_START = "<!-- llm-wiki-links:start -->"
LINKS_FOOTER_END = "<!-- llm-wiki-links:end -->"

MARKDOWN_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")


@dataclass
class MarkdownFenceInfo:
    line_number: int
    marker_char: str
    marker_length: int
    raw_line: str


@dataclass
class MarkdownFenceScan:
    inside_after_line: list[bool]
    openings: list[MarkdownFenceInfo]
    closings: list[MarkdownFenceInfo]
    unclosed: MarkdownFenceInfo | None


def parse_markdown_fence_marker(line: str) -> tuple[str, int, str] | None:
    match = MARKDOWN_FENCE_RE.match(line.rstrip("\n"))
    if not match:
        return None
    marker = match.group("marker")
    return marker[0], len(marker), match.group("rest") or ""


def scan_markdown_fences(source_lines: list[str]) -> MarkdownFenceScan:
    inside_after_line: list[bool] = []
    openings: list[MarkdownFenceInfo] = []
    closings: list[MarkdownFenceInfo] = []
    opened: MarkdownFenceInfo | None = None
    in_fence = False
    for number, line in enumerate(source_lines, 1):
        parsed = parse_markdown_fence_marker(line)
        if parsed:
            char, length, rest = parsed
            info = MarkdownFenceInfo(number, char, length, line)
            is_close = in_fence and opened and char == opened.marker_char and length >= opened.marker_length and not rest.strip()
            if is_close:
                closings.append(info)
                in_fence = False
                opened = None
            elif not in_fence:
                openings.append(info)
                opened = info
                in_fence = True
        inside_after_line.append(in_fence)
    return MarkdownFenceScan(inside_after_line, openings, closings, opened)


def is_fence_line(line: str) -> bool:
    return parse_markdown_fence_marker(line) is not None


def is_tableish_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") or bool(re.match(r"^\s*[-|: ]+\s*$", line))


def strip_image_media(text: str) -> str:
    """Remove any supported image representation while retaining descriptions."""

    return strip_images(text, keep_descriptions=True)


def strip_big_tables(text: str, *, max_rows: int = 40) -> str:
    def replace(match: re.Match[str]) -> str:
        table = match.group(0)
        rows = re.findall(r"<tr\b[^>]*>.*?</tr>", table, re.I | re.S)
        return table if len(rows) <= max_rows else "<table>[large table omitted]</table>"

    return re.sub(r"<table\b[^>]*>.*?</table>", replace, text, flags=re.I | re.S)


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    if size <= 0:
        raise ValueError("size must be positive")
    overlap = max(0, min(overlap, size - 1))
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    step = size - overlap
    while start < len(lines):
        end = min(len(lines), start + size)
        chunks.append((start + 1, end, "\n".join(lines[start:end])))
        if end == len(lines):
            break
        start += step
    return chunks


__all__ = [
    "LINKS_FOOTER_END", "LINKS_FOOTER_START", "MarkdownFenceInfo", "MarkdownFenceScan",
    "MARKDOWN_FENCE_RE", "chunk_text", "is_fence_line", "is_tableish_line",
    "parse_markdown_fence_marker", "scan_markdown_fences", "strip_big_tables", "strip_image_media",
]
