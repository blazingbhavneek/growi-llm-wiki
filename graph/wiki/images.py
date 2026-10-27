"""Images are immutable evidence (plan section 10).

An image belongs to its owning source chunk; it is never optional decoration.
``<image-unit>``, ``<img>``, ``<embed>``, and Markdown image spellings share a
content identity, while two internal types keep source and prompt text apart:

``ImageUnit.raw``
    Exact source markup, including any base64 media payload.  Deterministic
    restoration uses it, while integrity comparisons use decoded-media hashes.

``SanitizedSource`` / ``ImageUnit.prompt_marker``
    Stable ID, mime, alt text and description with the payload omitted.  Only
    prompt builders consume it, and it is never persisted as content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Callable, Iterable, Sequence

from graph.common.images import (
    IMAGE_UNIT_RE,
    ImageMarkup,
    find_images,
    image_source_identity,
    replace_images_preserving_lines,
)

from .ids import image_id
from .markdown_blocks import build_block_index
from .schemas import ImageRecord
from .storage import sha256_text, slice_text

_MEDIA_RE = re.compile(
    r"""src\s*=\s*["']data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]*)["']""",
    re.IGNORECASE,
)
_ALT_RE = re.compile(r"""alt=["'](?P<alt>[^"']*)["']""", re.IGNORECASE)
_DESC_RE = re.compile(
    r"<image-description\b[^>]*>(?P<desc>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)
_UNIT_RE = IMAGE_UNIT_RE
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


@dataclass(frozen=True)
class ImageUnit:
    """Raw form of one image block plus representation-independent identity."""

    image_id: str
    source_start: int
    source_end: int
    raw: str
    mime: str = ""
    alt: str = ""
    description: str = ""
    unit_sha256: str = ""
    media_sha256: str = ""

    @property
    def prompt_marker(self) -> str:
        parts = [self.image_id]
        if self.mime:
            parts.append(f"type: {self.mime}")
        if self.alt:
            parts.append(f"alt: {self.alt}")
        if self.description:
            parts.append(f"description: {' '.join(self.description.split())}")
        return "[IMAGE " + "; ".join(parts) + "]"

    @property
    def placeholder(self) -> str:
        """Deterministic stand-in used in drafts and rewrite prompts."""

        return f"[[NEO-IMAGE:{self.image_id}]]"


@dataclass(frozen=True)
class SanitizedSource:
    """Model-visible text. Never write this to a wiki page or a slice file."""

    text: str
    line_count: int

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"SanitizedSource({self.line_count} lines, base64 removed)"


def _build_unit(
    raw: str,
    start: int,
    end: int,
    *,
    identity: str = "",
    media_sha256: str = "",
    mime: str = "",
    alt_text: str = "",
    description_text: str = "",
    occurrence: int = 0,
) -> ImageUnit:
    # ``unit_sha256`` is intentionally representation-independent.  It is used
    # by incremental integrity checks, where an <embed>, <img>, Markdown image,
    # and <image-unit> containing the same source must be interchangeable.
    unit_hash = sha256_text(identity or raw)
    return ImageUnit(
        # Occurrence, rather than physical line range, keeps the placeholder ID
        # stable when a parser changes only the number of wrapper lines.
        image_id=image_id(unit_hash, str(occurrence)),
        source_start=start,
        source_end=end,
        raw=raw,
        mime=mime,
        alt=alt_text,
        description=" ".join(description_text.split()),
        unit_sha256=unit_hash,
        media_sha256=media_sha256,
    )


def extract_image_units(lines: Sequence[str]) -> list[ImageUnit]:
    """Find all supported image spellings with exact source bytes and ranges."""

    text = "\n".join(lines)
    # Preserve the old hard failure for malformed rich units.  Plain <img> and
    # <embed> tags are self-contained and need no corresponding close marker.
    opened = len(re.findall(r"<image-unit\b", text, re.IGNORECASE))
    closed = len(re.findall(r"</image-unit>", text, re.IGNORECASE))
    if opened != closed:
        raise ValueError("unclosed or unmatched <image-unit> block")

    units: list[ImageUnit] = []
    for occurrence, image in enumerate(find_images(text), start=1):
        units.append(
            _build_unit(
                image.raw,
                image.source_start,
                image.source_end,
                identity=image.identity,
                media_sha256=image.media_sha256,
                mime=image.mime,
                alt_text=image.alt,
                description_text=image.description,
                occurrence=occurrence,
            )
        )
    return units


def reuse_image_descriptions(
    previous: str,
    current: str,
    describe: Callable[[str, str], str],
    *,
    repeat_descriptions: bool = True,
) -> str:
    """Reuse descriptions by media hash and describe only unseen image bytes."""

    cached: dict[str, str] = {}
    for image in find_images(previous):
        if image.media_sha256 and image.description:
            cached.setdefault(image.media_sha256, image.description)

    generated: dict[str, str] = {}
    emitted: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        media = _MEDIA_RE.search(block)
        description = _DESC_RE.search(block)
        if not media or not description:
            return block
        _identity, key, _mime = image_source_identity(
            f'data:{media.group("mime")};base64,{media.group("data")}'
        )
        if not key:
            return block
        if not repeat_descriptions:
            if key in emitted:
                start = description.start("desc")
                end = description.end("desc")
                return block[:start] + block[end:]
            emitted.add(key)
        if key in cached:
            value = cached[key]
        else:
            if key not in generated:
                alt = _ALT_RE.search(block)
                data_url = f'data:{media.group("mime")};base64,{media.group("data")}'
                generated[key] = describe(
                    data_url,
                    unescape(alt.group("alt")) if alt else "",
                ).strip().replace(
                    "</image-description>", "&lt;/image-description&gt;"
                )
            value = generated[key]
        start = description.start("desc")
        end = description.end("desc")
        return block[:start] + value + block[end:]

    return _UNIT_RE.sub(replace, current)


def neutralize_image_descriptions(text: str) -> str:
    """Remove description wording from diffs without changing line numbers."""

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        start = match.start("desc") - match.start()
        end = match.end("desc") - match.start()
        return block[:start] + ("\n" * match.group("desc").count("\n")) + block[end:]

    return _DESC_RE.sub(replace, text)


def block_units(lines: Sequence[str]) -> list[ImageUnit]:
    """Fences and tables as placeholder units: the writer places a token,
    Python restores the exact source lines, so code is never retyped."""

    return [
        ImageUnit(
            image_id=f"{block.kind}-{block.start}-{block.end}",
            source_start=block.start,
            source_end=block.end,
            raw=slice_text(lines, block.start, block.end),
            description=f"{block.kind}: {lines[block.start - 1].strip()[:80]}",
        )
        for block in build_block_index(list(lines)).blocks
        if block.kind in ("fence", "table")
    ]


# --------------------------------------------------------------------------
# Prompt-side helpers
# --------------------------------------------------------------------------


def image_records(units: Iterable[ImageUnit]) -> list[ImageRecord]:
    """JSON-safe ledger entries: hashes and ranges, never the payload."""

    return [
        ImageRecord(
            image_id=unit.image_id,
            source_start=unit.source_start,
            source_end=unit.source_end,
            unit_sha256=unit.unit_sha256,
            media_sha256=unit.media_sha256,
            mime=unit.mime,
            alt=unit.alt,
            description=unit.description,
            prompt_marker=unit.prompt_marker,
        )
        for unit in units
    ]


def sanitize_lines(lines: Sequence[str], units: Sequence[ImageUnit]) -> list[str]:
    """Replace image media with a stable marker, keeping line numbering intact."""

    text = "\n".join(lines)
    parsed = find_images(text)
    by_start = {
        image.start: unit for image, unit in zip(parsed, units) if image.raw == unit.raw
    }

    def marker(image: ImageMarkup) -> str:
        unit = by_start.get(image.start)
        return unit.prompt_marker if unit is not None else "[IMAGE]"

    return replace_images_preserving_lines(text, marker).split("\n")


def sanitized_source(
    lines: Sequence[str], units: Sequence[ImageUnit]
) -> SanitizedSource:
    return SanitizedSource(
        text="\n".join(sanitize_lines(lines, units)), line_count=len(lines)
    )


def numbered_prompt_block(
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    source_start: int,
    source_end: int,
) -> SanitizedSource:
    """Numbered, sanitized slice for a prompt; line numbers are the real ones."""

    body = sanitize_lines(lines, units)[source_start - 1 : source_end]
    text = "\n".join(
        f"{number}: {line}" for number, line in enumerate(body, source_start)
    )
    return SanitizedSource(text=text, line_count=source_end - source_start + 1)


# --------------------------------------------------------------------------
# Substitution and integrity
# --------------------------------------------------------------------------


def placeholder_pattern(image_id_value: str) -> re.Pattern[str]:
    return re.compile(re.escape(f"[[NEO-IMAGE:{image_id_value}]]"))


def placeholders_in(markdown: str) -> list[str]:
    return re.findall(r"\[\[NEO-IMAGE:([A-Za-z0-9_-]+)\]\]", markdown or "")


def restore_images(
    markdown: str, units: Sequence[ImageUnit]
) -> tuple[str, list[str]]:
    """Deterministically place original image units back into a rewrite.

    Returns the final markdown plus any placeholder that has no known unit --
    an unresolved placeholder is a hard gate failure, never a silent drop.
    """

    by_placeholder = {unit.placeholder: unit for unit in units}
    result = markdown
    for placeholder, unit in by_placeholder.items():
        if placeholder in result:
            result = result.replace(placeholder, unit.raw)

    unresolved = [
        token
        for token in placeholders_in(result)
        if f"[[NEO-IMAGE:{token}]]" not in by_placeholder
    ]
    return result, unresolved


def missing_image_ids(markdown: str, units: Sequence[ImageUnit]) -> list[str]:
    """Images the writer dropped entirely (placeholder neither present nor raw)."""

    missing = []
    for unit in units:
        if unit.placeholder in markdown or unit_intact(markdown, unit):
            continue
        missing.append(unit.image_id)
    return missing


def unit_intact(markdown: str, unit: ImageUnit) -> bool:
    """At least one equivalent image occurs, regardless of wrapper spelling."""

    return count_equivalent_units(markdown, unit) >= 1


def count_equivalent_units(markdown: str, unit: ImageUnit) -> int:
    """Count images with the same byte/reference identity as ``unit``."""

    if not unit.unit_sha256:
        return (markdown or "").count(unit.raw)
    return sum(
        1
        for image in find_images(markdown or "")
        if sha256_text(image.identity or image.raw) == unit.unit_sha256
    )


def image_identity_counts(markdown: str) -> dict[str, int]:
    """Count image occurrences by the same identity used by ``ImageUnit``."""

    counts: dict[str, int] = {}
    for image in find_images(markdown or ""):
        key = sha256_text(image.identity or image.raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


def count_units(markdown: str, units: Sequence[ImageUnit]) -> dict[str, int]:
    return {unit.image_id: count_equivalent_units(markdown, unit) for unit in units}


def scrub_base64(text: str) -> str:
    """Logs must never contain image data (plan 14)."""

    scrubbed = _MEDIA_RE.sub(
        lambda match: f'src="data:{match.group("mime")};base64,<omitted>"', text
    )
    return _BASE64_RUN_RE.sub("<omitted-binary>", scrubbed)
