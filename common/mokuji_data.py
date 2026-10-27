"""The search data block at the bottom of every 目次 page: one format, one parser.

The builder writes it (publisher/index.py) and growi-search reads it, so the two sides
cannot drift apart. It is JSON Lines inside a fenced block, one record per line, which
keeps GROWI's editor and revision diffs usable on big documents::

    <details>
    <summary>検索用データ</summary>

    ```llm-wiki-data
    {"hash":"…","level":"document","name":"…","type":"block","version":1}
    {"type":"page",…}
    {"type":"section",…}
    ```

    </details>

Records by level:
    document  page (one per content page), section (one per H2 section of that page),
              and child records when the document folder also holds sub-folders
    folder    child (a document or folder below it)
    root      child

Every child record carries the ``hash`` of that child's own block, and a block's hash
covers its child hashes, so a change anywhere changes every hash on the way up to the
project root. growi-search polls only the root and walks down where hashes differ.

Stdlib only: growi-search imports this module without the builder's dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from common.markdown import LINKS_FOOTER_END, LINKS_FOOTER_START

FENCE_INFO = "llm-wiki-data"
VERSION = 1
SUMMARY = "検索用データ"

_BLOCK_RE = re.compile(r"^```" + re.escape(FENCE_INFO) + r"[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\([^)\n]*\)")
_PUBLISHER_MARKER_RE = re.compile(
    r"^<!-- (?:chunk(?:-end)?: [^\n]*|llm-wiki-bot-ref:[A-Za-z0-9_-]+|llm-wiki-index:[A-Za-z0-9_-]+) *-->$"
)  # the publisher's ownership stamp (current and previous form)


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def block_hash(records: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(_line(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:32]


def render(level: str, name: str, records: list[dict[str, Any]]) -> str:
    """Markdown for the block; deterministic, so an unchanged 目次 is never rewritten."""
    head = {"type": "block", "version": VERSION, "level": level, "name": name, "hash": block_hash(records)}
    lines = [_line(head), *(_line(record) for record in records)]
    # JSON escapes newlines, so every line starts with "{" and can never close the fence.
    return "\n".join(["<details>", f"<summary>{SUMMARY}</summary>", "", f"```{FENCE_INFO}", *lines, "```",
                      "", "</details>", ""])


@dataclass
class MokujiData:
    level: str
    name: str
    hash: str
    version: int
    records: list[dict[str, Any]] = field(default_factory=list)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("type") == kind]

    @property
    def pages(self) -> list[dict[str, Any]]:
        return self.of("page")

    @property
    def sections(self) -> list[dict[str, Any]]:
        return self.of("section")

    @property
    def children(self) -> list[dict[str, Any]]:
        return self.of("child")


def parse(markdown: str) -> MokujiData | None:
    """The block of one 目次 page, or None when the page has none (an older builder)."""
    match = _BLOCK_RE.search((markdown or "").replace("\r\n", "\n"))
    if not match:
        return None
    records: list[dict[str, Any]] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:  # a hand edit broke one line: keep the rest
            continue
        if isinstance(value, dict):
            records.append(value)
    head = next((record for record in records if record.get("type") == "block"), None)
    if head is None:
        return None
    body = [record for record in records if record.get("type") != "block"]
    return MokujiData(level=str(head.get("level") or ""), name=str(head.get("name") or ""),
                      hash=str(head.get("hash") or block_hash(body)), version=int(head.get("version") or 0),
                      records=body)


def _without_nav(text: str) -> str:
    marker = text.rfind("\n---\n")
    if marker >= 0 and ("前のページ" in text[marker:] or "次のページ" in text[marker:]):
        text = text[:marker]
    return text


def search_sections(body: str) -> list[tuple[int, str, str]]:
    """(ordinal, heading, text) of a published page, split the way the builder split it.

    Mirrors graph/linker/chunks.py ``split_page`` (H2 outside fences, empty parts skipped),
    after dropping the publisher's chunk markers and the linker's links footer and unwrapping
    inline links, so section N here is section N in the page's data block and the text is the
    pre-link text again.
    """
    text = (body or "").replace("\r\n", "\n")
    start = text.find(LINKS_FOOTER_START)
    end = text.find(LINKS_FOOTER_END, start + 1) if start >= 0 else -1
    if start >= 0 and end >= 0:
        text = text[:start] + text[end + len(LINKS_FOOTER_END):]
    lines = [line for line in text.splitlines() if not _PUBLISHER_MARKER_RE.match(line.strip())]
    inside = False
    starts: list[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            inside = not inside
            continue
        if inside:
            continue
        lines[i] = _LINK_RE.sub(r"\1", line)
        if line.startswith("## "):
            starts.append(i)
    bounds = [0, *starts, len(lines)]
    out: list[tuple[int, str, str]] = []
    for s, e in zip(bounds, bounds[1:]):
        part = "\n".join(lines[s:e]).strip("\n")
        if not part.strip():
            continue
        heading = lines[s][3:].strip() if s in starts else ""
        out.append((len(out), heading, _without_nav(part)))
    return out


__all__ = ["FENCE_INFO", "MokujiData", "SUMMARY", "VERSION", "block_hash", "parse", "render", "search_sections"]
