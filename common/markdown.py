"""Small markdown primitives shared by phase boundaries."""

from __future__ import annotations

import re

LINKS_FOOTER_START = "<!-- llm-wiki-links:start -->"
LINKS_FOOTER_END = "<!-- llm-wiki-links:end -->"
MARKDOWN_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")


def normalize_source(text: str) -> str:
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    return value + "\n" if value and not value.endswith("\n") else value


def split_source_lines(text: str) -> list[str]:
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def parse_markdown_fence_marker(line: str) -> tuple[str, int, str] | None:
    match = MARKDOWN_FENCE_RE.match(line.rstrip("\n"))
    if not match:
        return None
    marker = match.group("marker")
    return marker[0], len(marker), match.group("rest") or ""


def is_tableish_line(line: str) -> bool:
    stripped = line.strip()
    return (stripped.startswith("|") and stripped.endswith("|")) or bool(re.match(r"^\s*[-|: ]+\s*$", line))


def strip_big_tables(text: str, *, max_rows: int = 40) -> str:
    def replace(match: re.Match[str]) -> str:
        table = match.group(0)
        rows = re.findall(r"<tr\b[^>]*>.*?</tr>", table, re.I | re.S)
        return table if len(rows) <= max_rows else "<table>[large table omitted]</table>"

    return re.sub(r"<table\b[^>]*>.*?</table>", replace, text, flags=re.I | re.S)


__all__ = ["LINKS_FOOTER_END", "LINKS_FOOTER_START", "is_tableish_line", "normalize_source", "parse_markdown_fence_marker", "split_source_lines", "strip_big_tables"]
