"""Pure helpers for section-wise page writing.

No file IO and no model calls live here, so every function is testable with
plain strings.  Python decides section boundaries, what must survive a
rewrite verbatim, where cross-page facts go, and which titles get linked.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from typing import Callable, Sequence
import unicodedata

from .markdown_blocks import atomic_windows, build_block_index
from .wire import ReferenceFact
from graph.common.markdown import strip_image_media
from graph.common.images import strip_images

HEADING_RE = re.compile(r"^#{1,4} \S")
# The third alternative covers the short letter+digit constants (``T1``, ``P0``, ``30Wh``)
# that the >=3-char and must-start-with-a-letter rules above both skip; they are exactly
# the kind of tuned value a small edit changes.  Requiring one letter and one digit keeps
# ordinary words and bare numbers out, which the existing filter already rejects anyway.
CODE_TOKEN_RE = re.compile(
    r"0[xX][0-9A-Fa-f]+|[A-Za-z_][A-Za-z0-9_]{2,}"
    r"|(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{2,}"
)
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[ァ-ヶー]{3,}|[一-龯]{2,}")
REFERENCE_MARKER_RE = re.compile(r"（参照元:\s*原文\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?行）")
READER_REFERENCE_RE = re.compile(
    r"（(?:参照元:\s*(?:\[[^\]\n]*\]\([^\n]*?\)\s*)?)?原文\s*\d+\s*(?:[-–—]\s*\d+\s*)?行）"
)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^\n]*?\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\]")
# Model-visible scaffolding that names an image id.  ``sanitized()`` swaps an image
# line for ``<media payload omitted: img-…>`` and prompts carry ``[IMAGE img-…]``
# markers; a wiki page holds the restored unit instead, so either one left in the
# text makes ``code_tokens`` demand an identifier the page can never contain.
IMAGE_MARKER_RE = re.compile(r"<media payload omitted:[^>]*>|\[IMAGE [^\]]*\]")
# doc-parser output backslash-escapes CommonMark punctuation in prose (e.g.
# ``mpi\_aware``). A faithful rewrite naturally drops that escape, which used
# to make code_tokens() see "_aware" as a token the draft "lost". Strip the
# escape before tokenizing so both sides compare the same identifier.
MD_ESCAPE_RE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])")
# "GPUs"/"VEs" in English prose become "GPU"/"VE" in the rewrite; compare stems.
PLURAL_ACRONYM_RE = re.compile(r"\b([A-Z0-9]{2,})s\b")


def is_small_document(text: str) -> bool:
    """Under 10k readable characters; keep descriptions, exclude media and markup."""

    text = strip_image_media(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return len(html.unescape(text).strip()) < 10_000


def _nonblank(lines: Sequence[str], start: int, end: int) -> int:
    return sum(1 for line in lines[start - 1 : end] if line.strip())


def split_sections(
    lines: Sequence[str],
    start: int,
    end: int,
    *,
    target: int = 80,
    min_lines: int = 8,
) -> list[tuple[int, int]]:
    """Cut one page's owned range into writer-sized sections.

    Cuts happen before Markdown headings (never inside a fence, table or
    image unit); anything longer than ``target`` is split on atomic-block
    boundaries; adjacent pieces are packed while they fit in ``target`` lines,
    and anything with fewer than ``min_lines`` non-blank lines is merged into
    its neighbour regardless.  The result tiles ``start..end`` exactly.
    """

    page = list(lines[start - 1 : end])
    total = len(page)
    if total == 0:
        return []
    index = build_block_index(page)
    cuts = [
        number
        for number, line in enumerate(page, start=1)
        if number > 1 and HEADING_RE.match(line) and index.cut_is_safe(number)
    ]
    bounds = [1, *cuts, total + 1]
    ranges = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]

    split: list[tuple[int, int]] = []
    for s, e in ranges:
        if e - s + 1 > target:
            for ws, we in atomic_windows(page[s - 1 : e], target=target):
                split.append((s + ws - 1, s + we - 1))
        else:
            split.append((s, e))

    merged: list[list[int]] = []
    for s, e in split:
        if merged and (
            _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines
            or e - merged[-1][0] + 1 <= target
        ):
            merged[-1][1] = e
        else:
            merged.append([s, e])
    if len(merged) > 1 and _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines:
        merged[-2][1] = merged[-1][1]
        merged.pop()

    result = [(start + s - 1, start + e - 1) for s, e in merged]
    assert result[0][0] == start and result[-1][1] == end
    assert all(result[i][1] + 1 == result[i + 1][0] for i in range(len(result) - 1))
    return result


def opaque_free(text: str) -> str:
    """Source text with payloads a rewrite cannot reproduce reduced to nothing.

    ``[[NEO-IMAGE:...]]`` tokens and every supported inline image spelling
    (``<image-unit>``, ``<img>``, ``<embed>``, or Markdown image) have to go
    before tokenizing, or ``code_tokens`` invents "identifiers" out of base64
    alphabet and the lossless check becomes unsatisfiable. ``strip_image_media``
    keeps ``<image-description>`` prose, so only the media stops producing tokens.
    """

    cleaned = strip_image_media(text or "")
    cleaned = IMAGE_MARKER_RE.sub(" ", PLACEHOLDER_RE.sub(" ", cleaned))
    # The parser writes constants in full-width (``Ｔ１分``, ``３０Ｗｈ``) and a rewrite
    # may emit either width, so compare one canonical form.  Without this the
    # identifier check is blind to every full-width constant, which is most of what
    # a Japanese source document actually changes.  NFKC leaves plain ASCII alone.
    return unicodedata.normalize("NFKC", MD_ESCAPE_RE.sub(r"\1", cleaned))


def code_tokens(text: str, *, prepare: Callable[[str], str] | None = None) -> set[str]:
    """Identifier-like tokens that a lossless rewrite must keep verbatim."""

    # ponytail: bare numbers are too noisy; add units-aware numeric checks if needed.
    found: set[str] = set()
    cleaned = opaque_free(text)
    if prepare is not None:  # a policy hook (common/policy.py: Policy.code_tokens)
        cleaned = prepare(cleaned)
    cleaned = PLURAL_ACRONYM_RE.sub(r"\1", cleaned)
    for token in CODE_TOKEN_RE.findall(cleaned):
        if (
            token[:2].lower() == "0x"
            or "_" in token
            or any(char.isdigit() for char in token)
            or any(char.isupper() for char in token[1:])
        ):
            # OCR breaks macros like ``__FILE__`` into ``\_ \_FILE\_ \_``,
            # which tokenizes as "_FILE_"; the rewrite correctly writes
            # "__FILE__". Strip edge underscores so both compare equal.
            found.add(token.strip("_") or token)
    return found


def word_tokens(text: str) -> set[str]:
    """Coarse vocabulary used only to rank reference candidates."""

    cleaned = opaque_free(text)
    return set(WORD_RE.findall(cleaned))


def verbatim_blocks(
    lines: Sequence[str], start: int, end: int
) -> list[tuple[str, int, int]]:
    """Fences and tables inside ``start..end`` as (kind, abs_start, abs_end)."""

    page = list(lines[start - 1 : end])
    index = build_block_index(page)
    return [
        (block.kind, start + block.start - 1, start + block.end - 1)
        for block in index.blocks
        if block.kind in ("fence", "table")
    ]


def _fence_flags(lines: Sequence[str]) -> list[bool]:
    """True for every line that is a fence delimiter or inside a fence."""

    flags: list[bool] = []
    inside = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            flags.append(True)
            inside = not inside
            continue
        flags.append(inside)
    return flags


# Public alias used by the linker's line-wise Markdown splitter.
fence_flags = _fence_flags


def demote_h1(text: str) -> str:
    """Turn ``# `` into ``## `` outside fences; the page owns the only H1."""

    lines = text.splitlines()
    flags = _fence_flags(lines)
    return "\n".join(
        ("## " + line[2:]) if not flags[i] and line.startswith("# ") else line
        for i, line in enumerate(lines)
    )


def normalize_draft(raw: str) -> str:
    """Strip model noise (thinking, a whole-output fence) and demote H1."""

    text = THINK_RE.sub("", raw or "").strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        first = lines[0].strip()
        fence_count = sum(1 for line in lines if line.lstrip().startswith("```"))
        if first in ("```markdown", "```md") or (first == "```" and fence_count % 2 == 1):
            lines = lines[1:-1]
    text = demote_h1("\n".join(line.rstrip() for line in lines))
    return text.strip() + "\n"


def table_row_key(line: str) -> str:
    return re.sub(r"\s*\|\s*", "|", line.strip())


# Mechanical quality guard rails for the wording the writer authors itself.
# Fenced blocks and image payloads are excluded: the lossless checks copy those
# verbatim, so junk inside them is a convert-stage problem, not writer feedback.
# Simplified-Chinese codepoints that have a distinct Japanese shinjitai codepoint,
# so the test cannot fire on correct Japanese text (値 U+5024 vs 值 U+503C).
CHINESE_ONLY_GLYPHS = frozenset("值压单项关变应杀酱查运总发图录")
JUNK_MARKERS = ("<nl>", "<fcel>", "<lcel>", "<ecel>", "#REF!", "text_image", "YZYZYZ")
REFUSAL_RE = re.compile(r"出力することができません|原文に内容が(含まれて|存在し)てい")
LOOP_SCAN_LIMIT = 6000  # a degenerate line repeats inside its first kilobytes
HEADING_LINE_RE = re.compile(r"^#{2,6} \S")


def repeated_loop_fragment(line: str) -> str:
    """Return a fragment repeated until degeneration, or ``""`` for a normal line.

    Window counting instead of a backreference regex: a backreference costs
    seconds on the 30 kB lines that OCR produces.
    """

    scan = line[:LOOP_SCAN_LIMIT]
    if len(scan) < 60:
        return ""
    for window in (24, 12, 6):
        counts = Counter(scan[i:i + window] for i in range(len(scan) - window + 1))
        fragment, count = counts.most_common(1)[0]
        if count >= 8 and count * window >= 0.6 * len(scan) and fragment.strip():
            return fragment
    return ""


def authored_prose(text: str) -> str:
    """Writer-authored text: image payloads removed, fenced blocks collapsed to
    one placeholder line, and line numbers preserved."""

    without_images = strip_images(text or "", keep_descriptions=False)
    return re.sub(
        r"```[^`]*```",
        lambda match: "<fence>" + "\n" * max(0, match.group(0).count("\n") - 1),
        without_images,
        flags=re.S,
    )


def _worded_lines(text: str) -> list[tuple[int, str]]:
    """Numbered authored lines that carry wording (no markup-only or table lines)."""

    numbered = [
        (index + 1, line.strip())
        for index, line in enumerate(authored_prose(text).splitlines())
        if line.strip()
    ]
    return [
        (index, line)
        for index, line in numbered
        if not line.startswith(("|", "<")) and len(re.findall(r"[\w\u3040-\u30ff]", line)) >= 6
    ]


def _longest_runs(text: str) -> dict[str, int]:
    """Longest consecutive run per line, so a source-faithful repeat is invisible."""

    runs: dict[str, int] = {}
    previous = None
    length = 0
    for _, line in _worded_lines(text):
        length = length + 1 if line == previous else 1
        runs[line] = max(runs.get(line, 0), length)
        previous = line
    return runs


def _heading_counts(text: str) -> dict[str, int]:
    return Counter(
        line.strip() for line in authored_prose(text).splitlines() if HEADING_LINE_RE.match(line.strip())
    )


def quality_defects(text: str, *, source_text: str = "") -> list[str]:
    """Repetition, wrong-script and self-invented junk in a written section.

    Every returned string is writer feedback.  The guiding rule is that the
    writer is never blamed for the source: a repeat, a heading or a junk token
    that the converted source already contains is left alone, because the
    lossless checks make the writer copy it.  Thresholds stay conservative so a
    source-faithful repeated table row is never reported.
    """

    defects: list[str] = []
    numbered = [
        (index + 1, line.strip())
        for index, line in enumerate(authored_prose(text).splitlines())
        if line.strip()
    ]
    prose = _worded_lines(text)
    source_runs = _longest_runs(source_text)
    source_headings = _heading_counts(source_text)

    position = 0
    while position < len(prose):
        line_number, line = prose[position]
        last = position
        while (
            last + 1 < len(prose)
            and prose[last + 1][1] == line
            and prose[last + 1][0] == prose[last][0] + 1
        ):
            last += 1
        run = last - position + 1
        if run >= 3 and run > source_runs.get(line, 0):
            defects.append(
                f"{line_number}行目から同じ行を{run}回繰り返している。原文にない重複であり、"
                "同じ文・行・表を2回以上書かないこと: " + line[:60]
            )
        position = last + 1

    source_loops = {
        fragment for _, line in _worded_lines(source_text) if (fragment := repeated_loop_fragment(line))
    }
    for line_number, line in prose:
        fragment = repeated_loop_fragment(line)
        if fragment and fragment not in source_loops:
            defects.append(
                f"{line_number}行目で「{fragment[:30]}」が繰り返して途切れている。"
                "生成が暴走した形で原文にこんな形はない。繰り返しを取り除くこと。"
            )

    for glyph in sorted({c for _, line in numbered for c in line} & CHINESE_ONLY_GLYPHS):
        first = next((line_number for line_number, line in numbered if glyph in line), 0)
        defects.append(
            f"{first}行目に中国語簡体字「{glyph}」が混じっている。出力言語で書き、"
            "中国語混じりの語や表ヘッダ・誤字は原文の正しい表記へ直すこと。"
        )

    source_blob = source_text or ""
    junk = [
        marker
        for marker in JUNK_MARKERS
        if any(marker in line for _, line in numbered if not line.startswith("<"))
        and marker not in source_blob
    ]
    if junk:
        defects.append(
            "本文に変換ゴミを自分で書き足している: " + "、".join(junk)
            + "。これらの記号・内部名・エラー値を書かず、読める表か注記に直すこと。"
        )
    if REFUSAL_RE.search("\n".join(line for _, line in numbered)) and not REFUSAL_RE.search(source_blob):
        defects.append(
            "「出力できない」などの断り書きを本文に書いてはいけない。"
            "内容が本当に無いなら見出しと1行の注記に留め、推測で埋めないこと。"
        )

    for heading, count in sorted(Counter(_heading_lines(numbered)).items(),
                                 key=lambda item: -item[1]):
        if count >= 3 and count > source_headings.get(heading, 0):
            defects.append(
                f"見出し {heading} を{count}回書いている（原文は{source_headings.get(heading, 0)}回）。"
                "同じ見出しを繰り返さず、内容のない見出しは作らないこと。"
            )

    empty = [
        line for position, (_, line) in enumerate(numbered[:-1])
        if HEADING_LINE_RE.match(line)
        and HEADING_LINE_RE.match(numbered[position + 1][1])
        and _heading_level(numbered[position + 1][1]) <= _heading_level(line)
    ]
    if empty:
        defects.append(
            "直後に内容がない見出しがある（例: " + empty[0] + "）。"
            "見出しを書いたら必ずその内容を書くこと。"
        )
    return defects


def _heading_lines(numbered: Sequence[tuple[int, str]]) -> list[str]:
    return [line for _, line in numbered if HEADING_LINE_RE.match(line)]


def _heading_level(heading: str) -> int:
    return len(heading) - len(heading.lstrip("#"))


def check_section(
    draft: str,
    *,
    lines: Sequence[str],
    source_text: str,
    block_ranges: Sequence[tuple[str, int, int]],
    placeholders: Sequence[str],
    facts: Sequence[ReferenceFact],
    check_identifiers: bool = True,
    tokens: Callable[[str], set[str]] = code_tokens,
) -> list[str]:
    """Mechanical lossless checks. Every returned string is writer feedback."""

    errors: list[str] = []
    if not draft.strip():
        return ["出力が空である。節の本文をMarkdownで書くこと。"]
    draft_lines = draft.splitlines()
    draft_compact = "\n".join(line.rstrip() for line in draft_lines)
    draft_rows = {table_row_key(line) for line in draft_lines if line.strip().startswith("|")}

    for kind, s, e in block_ranges:
        block = [lines[number - 1] for number in range(s, e + 1)]
        if kind == "fence":
            inner = "\n".join(line.rstrip() for line in block[1:-1])
            if inner.strip() and inner not in draft_compact:
                errors.append(
                    f"原文 {s}-{e}行のコードブロックが一字一句同じ形で含まれていない。"
                    "中身を変えず、そのまま貼ること。"
                )
        else:
            missing = [
                line for line in block
                if line.strip().startswith("|") and table_row_key(line) not in draft_rows
            ]
            if missing:
                errors.append(
                    f"原文 {s}-{e}行の表から次の行が欠けている（表は全行そのまま写す）: "
                    + missing[0].strip()[:80]
                )

    for placeholder in placeholders:
        count = draft.count(placeholder)
        if count != 1:
            errors.append(f"画像トークン {placeholder} は必ず1回だけ置くこと（現在{count}回）。")

    missing_tokens = (
        sorted(tokens(source_text) - tokens(draft))
        if check_identifiers
        else []
    )
    if missing_tokens:
        errors.append(
            "次の識別子・定数が本文から消えている。省略や言い換えをせず必ず書くこと: "
            + ", ".join(missing_tokens[:40])
            + "。検証器は原文と出力から機械的に抽出したトークンを比較するため、"
            "原文と同じ綴り・区切りを保つこと。`_` に隣接する短い識別子や数値は"
            "前後の `_` を分離せず、原文で別トークンになっている識別子と数字を"
            "1語へ結合しないこと。"
        )

    return errors + quality_defects(draft, source_text=source_text)


def assign_facts(
    facts: Sequence[ReferenceFact], sections: Sequence[tuple[int, int]]
) -> list[list[ReferenceFact]]:
    """Bucket each fact into the section that owns its ``target_line``."""

    buckets: list[list[ReferenceFact]] = [[] for _ in sections]
    if not sections:
        return buckets
    for fact in facts:
        index = next(
            (i for i, (s, e) in enumerate(sections) if s <= fact.target_line <= e), 0
        )
        buckets[index].append(fact)
    return buckets


def _link_once(line: str, title: str, filename: str) -> str | None:
    at = line.find(title)
    while at >= 0:
        before = line[:at]
        if before.count("[") == before.count("]") and before.count("`") % 2 == 0:
            return before + f"[{title}]({filename})" + line[at + len(title):]
        at = line.find(title, at + 1)
    return None


def link_titles(markdown: str, targets: Sequence[tuple[str, str]]) -> str:
    """Wrap the first plain occurrence of each other page's title in a link.

    Skips fences, headings, tables, HTML/image lines, inline code and existing
    link text.  Idempotent: a title already linked to its file is left alone.
    """

    # ponytail: exact-title links only; add identifier ownership when under-linking matters.
    lines = markdown.splitlines()
    flags = _fence_flags(lines)
    for title, filename in sorted(targets, key=lambda item: -len(item[0])):
        title = title.strip()
        if len(title) < 2 or f"]({filename})" in "\n".join(lines):
            continue
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if flags[i] or stripped.startswith(("#", "|", "<", "[[NEO-IMAGE", "![")):
                continue
            linked = _link_once(line, title, filename)
            if linked is not None:
                lines[i] = linked
                break
    return "\n".join(lines).rstrip() + "\n"


def strip_reader_references(markdown: str) -> str:
    """Remove obsolete reader-facing source-line annotations."""

    return READER_REFERENCE_RE.sub("", markdown)


def _ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def link_entity_mentions(markdown: str, title: str, filename: str, *, max_links: int = 3) -> str:
    """Link 1–3 well-spaced plain mentions without adding any prose."""

    title = title.strip()
    if len(title) < 2 or max_links < 1:
        return markdown
    lines = markdown.splitlines()
    flags = _fence_flags(lines)
    existing = sum(line.count(f"[{title}]({filename})") for line in lines)
    remaining = max(0, max_links - existing)
    matches: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("<!-- wiki-links:start -->"):
            break
        if flags[i] or stripped.startswith(("#", "|", "<", "[[NEO-IMAGE", "![")) or "前のページ:" in line or "次のページ:" in line:
            continue
        blocked = [match.span() for regex in (MARKDOWN_LINK_RE, INLINE_CODE_RE) for match in regex.finditer(line)]
        at = line.find(title)
        while at >= 0:
            end = at + len(title)
            left_ok = not _ascii_word(title[0]) or at == 0 or not _ascii_word(line[at - 1])
            right_ok = not _ascii_word(title[-1]) or end == len(line) or not _ascii_word(line[end])
            if left_ok and right_ok and not any(start <= at < stop for start, stop in blocked):
                matches.append((i, at))
            at = line.find(title, at + len(title))
    if not matches or not remaining:
        return markdown
    count = min(remaining, 3 if len(matches) >= 5 else 2 if len(matches) >= 2 else 1)
    indexes = [0] if count == 1 else [0, len(matches) - 1] if count == 2 else [0, len(matches) // 2, len(matches) - 1]
    selected = [matches[index] for index in dict.fromkeys(indexes)]
    for line_index, at in reversed(selected):
        line = lines[line_index]
        lines[line_index] = line[:at] + f"[{title}]({filename})" + line[at + len(title):]
    return "\n".join(lines).rstrip() + "\n"
