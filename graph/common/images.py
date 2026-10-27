"""Representation-independent parsing of images embedded in Markdown/HTML.

The parser has emitted several equivalent spellings over time: ``<embed>``,
plain ``<img>``, Markdown images, and the richer ``<image-unit>`` wrapper.  This
module is the single low-level reader for those spellings.  When image bytes are
available, identity is always the SHA-256 of the decoded bytes; presentation
attributes, descriptions, wrapper choice, and base64 whitespace are ignored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
from bisect import bisect_right
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Callable


IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>.*?</image-unit>", re.IGNORECASE | re.DOTALL
)
IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)
HTML_IMAGE_RE = re.compile(
    r"<(?P<tag>img|embed)\b(?P<attrs>[^<>]*?)\s*/?>",
    re.IGNORECASE | re.DOTALL,
)
SRC_RE = re.compile(
    r"\bsrc\s*=\s*(?:[\"'](?P<quoted>.*?)[\"']|(?P<bare>[^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)
ALT_RE = re.compile(
    r"\balt\s*=\s*(?:[\"'](?P<quoted>.*?)[\"']|(?P<bare>[^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)
MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*(?:<(?P<angled>[^>\n]+)>|(?P<src>[^)\s]+))(?:\s+[\"'][^\n]*[\"'])?\s*\)",
    re.IGNORECASE,
)
DATA_IMAGE_RE = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ImageMarkup:
    """One complete image occurrence and its representation-free identity."""

    start: int
    end: int
    raw: str
    kind: str
    src: str = ""
    mime: str = ""
    alt: str = ""
    description: str = ""
    identity: str = ""
    media_sha256: str = ""
    source_start: int = 1
    source_end: int = 1
    start_column: int = 0
    end_column: int = 0


def _attribute(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.search(value)
    if not match:
        return ""
    return html.unescape(match.group("quoted") or match.group("bare") or "").strip()


def image_source_identity(src: str) -> tuple[str, str, str]:
    """Return ``(identity, media_sha256, mime)`` for an image source.

    Data URIs are identified by decoded bytes.  A non-inline source has no
    recoverable bytes, so its normalized reference is its identity.  This is
    deliberately conservative: a historical path is never guessed to equal an
    unrelated inline payload.
    """

    source = html.unescape(str(src or "")).strip()
    data = DATA_IMAGE_RE.match(source)
    if data:
        compact = "".join(data.group("data").split())
        try:
            payload = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error):
            # Invalid media must remain distinguishable, but should not crash a
            # diff or prompt sanitizer before the publishing integrity gate.
            digest = hashlib.sha256(compact.encode("ascii", "replace")).hexdigest()
            return f"invalid-sha256:{digest}", "", data.group("mime").lower()
        digest = hashlib.sha256(payload).hexdigest()
        return f"sha256:{digest}", digest, data.group("mime").lower()

    normalized = source.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return (f"ref-sha256:{digest}", "", "") if normalized else ("", "", "")


def _from_html(match: re.Match[str], *, kind: str | None = None) -> ImageMarkup:
    raw = match.group(0)
    src = _attribute(SRC_RE, raw)
    identity, media_sha256, mime = image_source_identity(src)
    return ImageMarkup(
        start=match.start(),
        end=match.end(),
        raw=raw,
        kind=kind or match.groupdict().get("tag", "image").lower(),
        src=src,
        mime=mime,
        alt=_attribute(ALT_RE, raw),
        identity=identity,
        media_sha256=media_sha256,
    )


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def find_images(text: str) -> list[ImageMarkup]:
    """Find image units and unwrapped image forms without double-counting."""

    value = text or ""
    found: list[ImageMarkup] = []
    occupied: list[tuple[int, int]] = []

    for unit in IMAGE_UNIT_RE.finditer(value):
        raw = unit.group(0)
        media = HTML_IMAGE_RE.search(raw)
        src = _attribute(SRC_RE, media.group(0)) if media else ""
        identity, media_sha256, mime = image_source_identity(src)
        description = IMAGE_DESCRIPTION_RE.search(raw)
        found.append(
            ImageMarkup(
                start=unit.start(),
                end=unit.end(),
                raw=raw,
                kind="image-unit",
                src=src,
                mime=mime,
                alt=_attribute(ALT_RE, media.group(0)) if media else "",
                description=(description.group("description").strip() if description else ""),
                identity=identity,
                media_sha256=media_sha256,
            )
        )
        occupied.append((unit.start(), unit.end()))

    for match in HTML_IMAGE_RE.finditer(value):
        if not _overlaps(match.start(), match.end(), occupied):
            found.append(_from_html(match))
            occupied.append((match.start(), match.end()))

    for match in MARKDOWN_IMAGE_RE.finditer(value):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        src = html.unescape(match.group("angled") or match.group("src") or "").strip()
        identity, media_sha256, mime = image_source_identity(src)
        found.append(
            ImageMarkup(
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                kind="markdown",
                src=src,
                mime=mime,
                alt=html.unescape(match.group("alt") or ""),
                identity=identity,
                media_sha256=media_sha256,
            )
        )
        occupied.append((match.start(), match.end()))

    ordered = sorted(found, key=lambda item: (item.start, item.end))
    line_starts = [0] + [index + 1 for index, char in enumerate(value) if char == "\n"]
    ranged: list[ImageMarkup] = []
    for image in ordered:
        start_line = bisect_right(line_starts, image.start) - 1
        end_line = bisect_right(line_starts, image.end) - 1
        ranged.append(
            dataclass_replace(
                image,
                source_start=start_line + 1,
                source_end=end_line + 1,
                start_column=image.start - line_starts[start_line],
                end_column=image.end - line_starts[end_line],
            )
        )
    return ranged


def replace_images_preserving_lines(
    text: str,
    replacement: Callable[[ImageMarkup], str],
) -> str:
    """Replace complete images while retaining exactly the same line count.

    For a multi-line image unit, text following the closing tag on its final
    line is moved beside the replacement on the first line.  The consumed lines
    become blank.  Thus ``<td><embed .../></td>`` and a multi-line image unit in
    that same cell normalize to the same meaningful line while range mapping
    can still account for parser-added lines.
    """

    value = text or ""
    lines = value.split("\n")
    for image in reversed(find_images(value)):
        start_line = image.source_start - 1
        end_line = image.source_end - 1
        start_col = image.start_column
        end_col = image.end_column
        marker = replacement(image)
        if start_line == end_line:
            lines[start_line] = (
                lines[start_line][:start_col] + marker + lines[start_line][end_col:]
            )
            continue
        suffix = lines[end_line][end_col:]
        lines[start_line] = lines[start_line][:start_col] + marker + suffix
        for number in range(start_line + 1, end_line + 1):
            lines[number] = ""
    return "\n".join(lines)


def canonicalize_images(text: str) -> str:
    """Normalize every image spelling to a token based only on image identity."""

    def marker(image: ImageMarkup) -> str:
        identity = image.identity or "opaque-sha256:" + hashlib.sha256(
            image.raw.encode("utf-8")
        ).hexdigest()
        return f"[[LLM-WIKI-IMAGE:{identity}]]"

    return replace_images_preserving_lines(text, marker)


def strip_images(text: str, *, keep_descriptions: bool = True) -> str:
    """Remove image payloads/references, retaining image-unit prose if requested."""

    return replace_images_preserving_lines(
        text or "",
        lambda image: image.description.strip()
        if keep_descriptions and image.description.strip()
        else "",
    )


__all__ = [
    "ALT_RE",
    "DATA_IMAGE_RE",
    "HTML_IMAGE_RE",
    "IMAGE_DESCRIPTION_RE",
    "IMAGE_UNIT_RE",
    "ImageMarkup",
    "canonicalize_images",
    "find_images",
    "image_source_identity",
    "replace_images_preserving_lines",
    "strip_images",
]
