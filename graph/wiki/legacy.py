from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from graph.common.images import find_images

log = logging.getLogger(__name__)

# region Config and Models


# ---------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------

GENERATION_LINES = 10
VERIFICATION_LINES = 25
MAX_CHUNK_EXTRA = 50

# Concurrency inside verification for one file.
CONCURRENCY = 20

TEMPERATURE = 0.7
TIMEOUT = 300

CLEAN_OUTPUT = True

PARTITION_RETRY_ATTEMPTS = 6
PARTITION_RETRY_BACKOFF_SECONDS = 2.0
CHUNK_FALLBACK_MECHANICAL = True


class ChunkPlanningError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass

# ---------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------


class FileRef(BaseModel):
    title: str = ""
    filename: str = ""


class NewFileRef(BaseModel):
    title: str = ""
    filename: str = ""
    summary: str = ""


class GenerationDecision(BaseModel):
    action: Literal["append", "new", "split", "ignore"]
    current_file: FileRef = Field(default_factory=FileRef)
    new_file: NewFileRef = Field(default_factory=NewFileRef)

    # Inclusive source line ranges, using real source line numbers shown to the LLM.
    # Example: [101, 150]
    current_source_range: Optional[list[int]] = None
    new_source_range: Optional[list[int]] = None

    reason: str = ""


class VerificationResult(BaseModel):
    answer: Literal["YES", "NO"]
    missing_facts: list[str] = Field(default_factory=list)
    hallucinations: list[str] = Field(default_factory=list)
    reason: str = ""


class RepairResult(BaseModel):
    markdown_patch: str = ""
    reason: str = ""


class ChunkSummary(BaseModel):
    """A factual description of one source chunk; it never owns source text."""

    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    suggested_heading: str = ""


class TopicRange(BaseModel):
    """A named, contiguous source range used at one level of the wiki tree."""

    title: str = ""
    summary: str = ""
    source_range: Optional[list[int]] = None


class H1Plan(BaseModel):
    sections: list[TopicRange] = Field(default_factory=list)


class H1Layout(BaseModel):
    """Sections are H2 folders when use_h2_folders is true, otherwise leaf pages."""

    use_h2_folders: bool = False
    sections: list[TopicRange] = Field(default_factory=list)


class LeafPagePlan(BaseModel):
    pages: list[TopicRange] = Field(default_factory=list)


@dataclass
class CurrentFileState:
    title: str
    filename: str
    summary: str
    line_count: int


# endregion Config and Models


# region Utils


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------


def range_to_markdown(
    source_lines: list[str],
    source_range: Optional[list[int]],
) -> str:
    """
    Convert an inclusive 1-based source range [start, end] to raw Markdown.

    Important:
    - Uses original source lines.
    - Does NOT include line number prefixes.
    """
    if not source_range or len(source_range) != 2:
        return ""

    start, end = int(source_range[0]), int(source_range[1])

    if start > end:
        return ""

    # Convert 1-based inclusive to Python slice.
    return "\n".join(source_lines[start - 1 : end])


def clamp_range_to_chunk(
    source_range: Optional[list[int]],
    chunk_start: int,
    chunk_end: int,
) -> Optional[list[int]]:
    """
    Clamp an LLM-provided inclusive source range to the current chunk.
    """
    if not source_range or len(source_range) != 2:
        return None

    start, end = int(source_range[0]), int(source_range[1])

    start = max(start, chunk_start)
    end = min(end, chunk_end)

    if start > end:
        return None

    return [start, end]


def full_chunk_range(source_start: int, source_end: int) -> list[int]:
    return [source_start, source_end]


def split_chunk_ranges(
    source_start: int, source_end: int
) -> tuple[list[int], list[int]]:
    midpoint = source_start + ((source_end - source_start) // 2)
    return [source_start, midpoint], [midpoint + 1, source_end]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def count_file_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def ensure_md_suffix(filename: str) -> str:
    filename = filename.strip()
    if not filename.endswith(".md"):
        filename += ".md"
    return filename


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-.")
    return text or "untitled"


def clean_filename_hint(filename: str, title: str) -> str:
    filename = filename.strip()
    if filename:
        stem = Path(filename).stem
    else:
        stem = title

    stem = re.sub(r"^\d{3,}-", "", stem)
    stem = slugify(stem)

    return ensure_md_suffix(stem)


def make_numbered_filename(index: int, filename_hint: str, title: str) -> str:
    clean = clean_filename_hint(filename_hint, title)
    stem = Path(clean).stem
    return f"{index:03d}-{stem}.md"


def get_next_file_index(out_dir: Path) -> int:
    max_index = 0
    for path in out_dir.glob("*.md"):
        match = re.match(r"^(\d{3,})-", path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1


def make_unique_filename(
    out_dir: Path, index: int, filename_hint: str, title: str
) -> str:
    filename = make_numbered_filename(index, filename_hint, title)
    path = out_dir / filename

    if not path.exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix

    counter = 2
    while True:
        candidate = f"{stem}-{counter}{suffix}"
        if not (out_dir / candidate).exists():
            return candidate
        counter += 1


def append_markdown(path: Path, markdown: str) -> int:
    markdown = markdown.strip()
    if not markdown:
        return 0

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + separator + markdown + "\n", encoding="utf-8")

    return len(markdown.splitlines())


def numbered_source_lines(lines: list[str], start_line_number: int) -> str:
    cleaned_lines = []
    for line in lines:
        # Strip base64 data from img tags before sending to the LLM
        if "data:image/" in line:
            line = re.sub(r'src=["\']data:image/[^"\']+["\']', 'src=""', line)
        cleaned_lines.append(line)

    return "\n".join(
        f"{start_line_number + i}: {line}" for i, line in enumerate(cleaned_lines)
    )


def last_n_lines_from_file(path: Path, n: int = 100) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-n:])


def extract_json_from_text(text: str) -> Any:
    """
    Fallback parser for models that return JSON wrapped in prose or fences.
    """

    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    first = text.find("{")
    last = text.rfind("}")

    if first != -1 and last != -1 and last > first:
        return json.loads(text[first : last + 1])

    raise ValueError("Could not extract valid JSON from LLM response.")


# ---------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------


def is_fence_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def is_tableish_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False

    if stripped.startswith("|") and "|" in stripped[1:]:
        return True

    if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", stripped):
        return True

    return False


def chunk_source_lines_preserving_tables(
    lines: list[str],
    target_size: int = 100,
    max_extra: int = 30,
) -> list[tuple[int, int]]:
    """
    Returns zero-based half-open ranges: [(start, end), ...]

    Tries to avoid cutting inside fenced code blocks, Markdown tables,
    and <image-unit> blocks.
    """

    chunks: list[tuple[int, int]] = []
    n = len(lines)
    start = 0

    # Precompute states to correctly handle blocks spanning across chunk boundaries
    fence_state = []
    in_f = False
    for line in lines:
        if is_fence_line(line):
            in_f = not in_f
        fence_state.append(in_f)

    img_state = [False] * n
    in_i = False
    for index, line in enumerate(lines):
        lowered = line.lower()
        if re.search(r"<image-unit\b", lowered):
            in_i = True
        img_state[index] = in_i
        if "</image-unit>" in lowered:
            in_i = False
            img_state[index] = False
    source = "\n".join(lines)
    for image in find_images(source):
        first = image.source_start - 1
        last = image.source_end - 1
        for index in range(first, last):
            img_state[index] = True

    while start < n:
        end = min(start + target_size, n)

        extra = 0

        while end < n and extra < max_extra:
            cut_inside_table = end > start and (
                is_tableish_line(lines[end - 1]) or is_tableish_line(lines[end])
            )

            # Check if we are inside a fence or an image unit at the proposed cut point.
            # The cut point is after line `end - 1`.
            inside_block = fence_state[end - 1] or img_state[end - 1]

            if not inside_block and not cut_inside_table:
                break

            end += 1
            extra += 1

        chunks.append((start, end))
        start = end

    return chunks


def fixed_windows(lines: list[str], window_size: int = 25) -> list[tuple[int, int]]:
    return [
        (i, min(i + window_size, len(lines))) for i in range(0, len(lines), window_size)
    ]


# ---------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------


def init_manifest(source_path: Path) -> dict[str, Any]:
    return {
        "source": str(source_path),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "files": [],
        "chunks": [],
        "planning": {},
        "coverage": [],
        "verification_flags": [],
    }


def find_file_record(
    manifest: dict[str, Any], filename: str
) -> Optional[dict[str, Any]]:
    for record in manifest["files"]:
        if record["filename"] == filename:
            return record
    return None


def add_or_update_file_record(
    manifest: dict[str, Any],
    filename: str,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
) -> None:
    record = find_file_record(manifest, filename)
    new_range = [source_start, source_end]

    if record is None:
        manifest["files"].append(
            {
                "filename": filename,
                "title": title,
                "summary": summary,
                "source_ranges": [new_range],
                "_merged_source_ranges": [new_range],
            }
        )
    else:
        record["source_ranges"].append(new_range)
        record["source_ranges"].sort(key=lambda r: r[0])

        # Merge overlapping or adjacent ranges
        merged = []
        for start, end in record["source_ranges"]:
            if not merged:
                merged.append([start, end])
            else:
                last = merged[-1]
                if start <= last[1] + 1:
                    last[1] = max(last[1], end)
                else:
                    merged.append([start, end])
                    log.debug(
                        f"[WARNING] Discontinuous source ranges in {filename}: {record['source_ranges']}"
                    )

        record["_merged_source_ranges"] = merged


def update_markdown_frontmatter(path: Path, new_merged_ranges: list[list[int]]) -> None:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return  # unexpected format

    # Split into frontmatter and body
    parts = content.split("---", 2)
    if len(parts) < 3:
        return

    _, front_str, body = parts

    # Parse existing frontmatter (simple YAML-like)
    lines = front_str.strip().splitlines()
    new_front_lines = []
    for line in lines:
        if line.startswith("source_lines:"):
            # Replace this line
            new_front_lines.append(f"source_lines: {json.dumps(new_merged_ranges)}")
        else:
            new_front_lines.append(line)

    new_front_str = "\n".join(new_front_lines)
    new_content = f"---\n{new_front_str}\n---\n{body}"

    path.write_text(new_content, encoding="utf-8")


def create_markdown_file(
    path: Path,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
    body: str,
) -> None:
    title = title.strip() or "Untitled"
    summary = summary.strip()
    # Keep `body` unchanged: leading and trailing blank source lines are owned
    # by this page just as much as non-blank source lines are.

    content = f"""---
title: {yaml_quote(title)}
summary: {yaml_quote(summary)}
source_lines: [[{source_start}, {source_end}]]
---

# {title}

{body}
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def add_chunk_record(
    manifest: dict[str, Any],
    source_start: int,
    source_end: int,
    action: str,
    targets: list[dict[str, Any]],
    reason: str,
) -> None:
    manifest["chunks"].append(
        {
            "source_start": source_start,
            "source_end": source_end,
            "action": action,
            "targets": targets,
            "reason": reason,
        }
    )
    manifest["updated_at"] = utc_now_iso()


def overlap_size(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def find_best_target_for_source_window(
    manifest: dict[str, Any],
    source_start: int,
    source_end: int,
) -> Optional[str]:
    """
    Returns the filename with the largest overlap for this source window.
    """

    best_filename: Optional[str] = None
    best_overlap = 0

    for chunk in manifest.get("chunks", []):
        for target in chunk.get("targets", []):
            t_start = int(target["source_start"])
            t_end = int(target["source_end"])
            size = overlap_size(source_start, source_end, t_start, t_end)

            if size > best_overlap:
                best_overlap = size
                best_filename = target["filename"]

    return best_filename

# Fence helpers

# ---------------------------------------------------------------------
# Markdown fence scan helpers
# ---------------------------------------------------------------------


@dataclass
class MarkdownFenceInfo:
    line_number: int
    marker_char: str
    marker_length: int
    raw_line: str


@dataclass
class MarkdownFenceScan:
    """
    inside_after_line is zero-based.

    Example:
    - inside_after_line[0] means: after source line 1, are we inside a fence?
    - A split before line X is a cut after line X - 1.
    """

    inside_after_line: list[bool]
    openings: list[MarkdownFenceInfo]
    closings: list[MarkdownFenceInfo]
    unclosed: MarkdownFenceInfo | None


MARKDOWN_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<rest>.*)$"
)

BOUNDARY_BEFORE_LINE_RE = re.compile(r"Boundary before line (\d+)")


def parse_markdown_fence_marker(line: str) -> tuple[str, int, str] | None:
    """
    Parse a Markdown fenced-code marker candidate.

    Returns:
        (marker_char, marker_length, rest_of_line)

    This only detects fenced code blocks using:
    - ``` or longer
    - ~~~ or longer

    It does NOT treat single backticks like `code` as fences.
    """

    match = MARKDOWN_FENCE_RE.match(line.rstrip("\n"))

    if not match:
        return None

    marker = match.group("marker")
    rest = match.group("rest") or ""

    return marker[0], len(marker), rest


def scan_markdown_fences(source_lines: list[str]) -> MarkdownFenceScan:
    """
    Stateful Markdown fence scanner.

    Better than simple odd/even toggling because it tracks:
    - backtick fence vs tilde fence
    - opening fence length
    - valid matching close fence

    Example:
    ````markdown
    ````python
    ```text
    this does not close the 4-backtick fence
    ```
    ````
    ````

    A simple toggle would get confused there.
    """

    inside_after_line: list[bool] = []
    openings: list[MarkdownFenceInfo] = []
    closings: list[MarkdownFenceInfo] = []

    in_fence = False
    open_marker_char: str | None = None
    open_marker_length = 0
    open_info: MarkdownFenceInfo | None = None

    for line_number, line in enumerate(source_lines, start=1):
        parsed = parse_markdown_fence_marker(line)

        if parsed is not None:
            marker_char, marker_length, rest = parsed

            if not in_fence:
                # CommonMark-ish rule:
                # An opening backtick fence's info string should not contain backticks.
                if marker_char == "`" and "`" in rest:
                    inside_after_line.append(in_fence)
                    continue

                in_fence = True
                open_marker_char = marker_char
                open_marker_length = marker_length
                open_info = MarkdownFenceInfo(
                    line_number=line_number,
                    marker_char=marker_char,
                    marker_length=marker_length,
                    raw_line=line.rstrip("\n"),
                )
                openings.append(open_info)

            else:
                # Closing fence:
                # - same marker char
                # - length >= opening length
                # - only whitespace after marker
                if (
                    marker_char == open_marker_char
                    and marker_length >= open_marker_length
                    and rest.strip() == ""
                ):
                    close_info = MarkdownFenceInfo(
                        line_number=line_number,
                        marker_char=marker_char,
                        marker_length=marker_length,
                        raw_line=line.rstrip("\n"),
                    )
                    closings.append(close_info)

                    in_fence = False
                    open_marker_char = None
                    open_marker_length = 0
                    open_info = None

        inside_after_line.append(in_fence)

    return MarkdownFenceScan(
        inside_after_line=inside_after_line,
        openings=openings,
        closings=closings,
        unclosed=open_info if in_fence else None,
    )


def assert_no_unclosed_markdown_fences(
    source_lines: list[str],
    label: str = "source",
) -> None:
    """
    Fail early if the whole file has an unclosed fenced code block.

    This catches the "odd number of fences globally" situation before the LLM
    gets stuck retrying impossible/poisoned boundaries.
    """

    scan = scan_markdown_fences(source_lines)

    if scan.unclosed is None:
        return

    raise RuntimeError(
        f"{label}: unclosed fenced code block. "
        f"Opening fence at line {scan.unclosed.line_number}: "
        f"{scan.unclosed.raw_line!r}. "
        f"Fence openings={len(scan.openings)}, closings={len(scan.closings)}."
    )


def cut_is_inside_fence_by_scan(
    source_lines: list[str],
    split_line: int,
) -> bool:
    """
    split_line is the first line of the next concept.

    The cut is between split_line - 1 and split_line.
    """

    if split_line <= 1:
        return False

    scan = scan_markdown_fences(source_lines)

    if scan.unclosed is not None:
        raise RuntimeError(
            f"Source has an unclosed fenced code block starting at line "
            f"{scan.unclosed.line_number}: {scan.unclosed.raw_line!r}."
        )

    cut_after_line = split_line - 1

    if cut_after_line <= 0:
        return False

    if cut_after_line > len(scan.inside_after_line):
        return False

    return scan.inside_after_line[cut_after_line - 1]


def basic_cut_is_inside_image_unit(
    source_lines: list[str],
    split_line: int,
) -> bool:
    if split_line <= 1:
        return False

    source = "\n".join(source_lines)
    cut = sum(len(line) + 1 for line in source_lines[: split_line - 1])
    if any(image.start < cut < image.end for image in find_images(source)):
        return True
    in_image_unit = False
    for line in source_lines[: split_line - 1]:
        lowered = line.lower()
        if re.search(r"<image-unit\b", lowered):
            in_image_unit = True
        if "</image-unit>" in lowered:
            in_image_unit = False
    return in_image_unit


def basic_cut_is_inside_table(
    source_lines: list[str],
    split_line: int,
) -> bool:
    if split_line <= 1 or split_line > len(source_lines):
        return False

    previous_line = source_lines[split_line - 2]
    current_line = source_lines[split_line - 1]

    return is_tableish_line(previous_line) and is_tableish_line(current_line)


def basic_boundary_is_safe(
    source_lines: list[str],
    split_line: int,
) -> bool:
    """
    Same safety policy as validate_safe_internal_boundary, but independent.

    Used only for generating suggestions for the LLM.
    """

    if cut_is_inside_fence_by_scan(source_lines, split_line):
        return False

    if basic_cut_is_inside_image_unit(source_lines, split_line):
        return False

    if basic_cut_is_inside_table(source_lines, split_line):
        return False

    return True


def nearby_safe_boundary_candidates(
    *,
    source_lines: list[str],
    bad_split_line: int,
    source_start: int,
    source_end: int,
    radius: int = 80,
    limit: int = 8,
) -> dict[str, list[int]]:
    """
    Return safe alternative split lines around a rejected boundary.

    split_line means boundary before that source line.
    """

    before: list[int] = []
    after: list[int] = []

    min_line = max(source_start + 1, bad_split_line - radius)
    max_line = min(source_end, bad_split_line + radius)

    for candidate in range(bad_split_line - 1, min_line - 1, -1):
        if basic_boundary_is_safe(source_lines, candidate):
            before.append(candidate)

            if len(before) >= limit:
                break

    for candidate in range(bad_split_line + 1, max_line + 1):
        if basic_boundary_is_safe(source_lines, candidate):
            after.append(candidate)

            if len(after) >= limit:
                break

    return {
        "before": before,
        "after": after,
    }


def enrich_boundary_error_with_suggestions(
    *,
    error: str,
    source_lines: list[str],
    source_start: int,
    source_end: int,
) -> str:
    """
    If error contains 'Boundary before line X', append actionable correction info.
    """

    match = BOUNDARY_BEFORE_LINE_RE.search(error or "")

    if not match:
        return error

    bad_line = int(match.group(1))

    candidates = nearby_safe_boundary_candidates(
        source_lines=source_lines,
        bad_split_line=bad_line,
        source_start=source_start,
        source_end=source_end,
        radius=80,
        limit=8,
    )

    return (
        f"{error}\n\n"
        f"Rejected boundary: before line {bad_line}\n"
        f"Safe boundary candidates before that line: {candidates['before']}\n"
        f"Safe boundary candidates after that line: {candidates['after']}\n\n"
        "Correction instructions:\n"
        f"- Do not reuse boundary before line {bad_line}.\n"
        "- A fenced code block means a block opened by triple-or-longer backticks or tildes, such as ``` or ~~~.\n"
        "- A single inline backtick like `code` is not a fenced code block.\n"
        "- If the code block belongs with the previous topic, move the split after the closing fence.\n"
        "- If the code block belongs with the next topic, move the split before the opening fence.\n"
        "- Prefer one of the safe boundary candidates listed above if it still preserves the topic structure."
    )

# endregion Utils


# region LLM


# ---------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------


def make_llm(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = TEMPERATURE,
    timeout: int = 300,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        reasoning_effort="low",
        # Temporary: disable thinking for faster summaries. Comment out to re-enable.
        # model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )


async def structured_ainvoke(
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None = None,
    *,
    temperature: float | None = None,
) -> BaseModel:
    # New wiki model ports provide the same structured seam while also
    # serializing fast writer calls on a shared GPU. Keep raw ChatOpenAI
    # support below for older callers.
    if hasattr(llm, "structured"):
        return await llm.structured(
            schema_cls,
            messages,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
    request_timeout = getattr(llm, "request_timeout", None)
    try:
        hard_timeout = float(request_timeout) if request_timeout is not None else None
    except (TypeError, ValueError):
        hard_timeout = None

    async def invoke(runnable: Any, payload: list[Any]) -> Any:
        operation = runnable.ainvoke(payload)
        if hard_timeout is None or hard_timeout <= 0:
            return await operation
        return await asyncio.wait_for(operation, timeout=hard_timeout)

    bind_kwargs: dict[str, Any] = {}
    # TEMPORARILY DISABLED: restore this cap when legacy structured responses
    # should be bounded again.
    # if max_output_tokens is not None:
    #     bind_kwargs["max_tokens"] = max_output_tokens
    if temperature is not None:
        bind_kwargs["temperature"] = temperature
    call_llm = llm.bind(**bind_kwargs) if bind_kwargs else llm

    try:
        structured_kwargs = {
            "temperature": temperature,
        } if temperature is not None else {}
        # TEMPORARILY DISABLED: restore the max_tokens entry when needed.
        # if max_output_tokens is not None:
        #     structured_kwargs["max_tokens"] = max_output_tokens
        structured = llm.with_structured_output(schema_cls, **structured_kwargs)
        result = await invoke(structured, messages)

        if isinstance(result, schema_cls):
            return result

        return schema_cls.model_validate(result)

    except Exception as structured_error:
        # A timed-out prompt will time out again; re-sending it twice more only
        # triples the loss. Let the caller's retry policy decide.
        if isinstance(structured_error, TimeoutError) or "Timeout" in type(structured_error).__name__:
            raise
        schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
        last_error: Exception = structured_error

        # A thinking/streaming model can still return malformed JSON after the
        # structured-output call fails. Give it one additional bounded, concise
        # request; do not create an unbounded retry loop.
        for retry in range(2):
            fallback_messages = list(messages)
            fallback_messages.append(
                HumanMessage(
                    content=(
                        (
                            "The previous response was invalid. Return the smallest "
                            "valid JSON object that satisfies this schema. Use empty "
                            "arrays or strings when uncertain."
                            if retry
                            else "Return ONLY valid JSON matching this JSON Schema. "
                            "Be concise. Do not include extra prose."
                        )
                        + "\n\n"
                        + schema_json
                    )
                )
            )
            try:
                raw = await invoke(call_llm, fallback_messages)
                text = raw.content if hasattr(raw, "content") else str(raw)
                data = extract_json_from_text(text)
                return schema_cls.model_validate(data)
            except Exception as fallback_error:
                last_error = fallback_error

        raise last_error from structured_error


# endregion LLM


# region Planning


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class ConceptFilePlan(BaseModel):
    """
    One concept/topic file assignment.

    source_start/source_end are 1-based inclusive source line numbers.
    """

    title: str = Field(
        description="日本語の、人間が読んで分かりやすい概念/トピックのタイトル。"
    )
    filename: str = Field(
        description=(
            "Markdownファイル名のみ。ディレクトリは含めない。"
            "日本語の内容を反映したファイル名にする。例: 機能概要.md"
        )
    )
    source_start: int = Field(
        description="このファイルが対象とする最初のソース行番号。1始まり、両端含む。"
    )
    source_end: int = Field(
        description="このファイルが対象とする最後のソース行番号。1始まり、両端含む。"
    )
    summary: str = Field(
        default="",
        description="このファイルに含まれる内容の短い日本語要約。",
    )


class ConceptSplitResult(BaseModel):
    files: list[ConceptFilePlan] = Field(
        description=(
            "Ordered list of concept files. Must exactly cover the requested "
            "source range with no gaps and no overlaps."
        )
    )


# ---------------------------------------------------------------------
# Filename / text helpers
# ---------------------------------------------------------------------


def concept_range_text(plan: ConceptFilePlan) -> str:
    return f"{plan.source_start}-{plan.source_end}"


def normalize_filename(filename: str, title: str) -> str:
    """
    Normalize model-provided filename into a safe local Markdown filename.

    Japanese filenames are preserved.
    Directories and unsafe filesystem characters are removed.
    """

    raw_name = Path((filename or "").strip()).name
    raw_stem = Path(raw_name).stem

    if not raw_stem:
        raw_stem = title or "ドキュメント"

    clean_stem = raw_stem.strip()

    # Replace whitespace with underscores.
    clean_stem = re.sub(r"\s+", "_", clean_stem)

    # Remove unsafe filename characters.
    clean_stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", clean_stem)

    # Avoid awkward leading/trailing punctuation.
    clean_stem = clean_stem.strip("._- ")

    if not clean_stem:
        clean_stem = "ドキュメント"

    return f"{clean_stem}.md"


def numbered_output_filename(
    *,
    index: int,
    total: int,
    filename: str,
) -> str:
    """
    Add stable source-order prefix to final document filename.

    Example:
        001-scn-modedata-txt.md
        002-scn-condata-txt.md
    """

    clean_name = Path(filename).name
    stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix or ".md"

    width = max(3, len(str(total)))

    return f"{index:0{width}d}-{stem}{suffix}"


def make_unique_output_path(out_dir: Path, filename: str) -> Path:
    """
    Avoid overwriting duplicate filenames.

    Usually the numeric prefix already prevents collisions, but this keeps the
    renderer safe if output exists for some reason.
    """

    out_dir.mkdir(parents=True, exist_ok=True)

    filename = Path(filename).name
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".md"

    candidate = out_dir / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate

    counter = 2

    while True:
        candidate = out_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate

        counter += 1


def join_original_source_lines(lines: list[str]) -> str:
    """
    Preserve source text as closely as possible.

    Supports both:
    - read_lines() returning newline-terminated strings
    - read_lines() returning newline-stripped strings
    """

    if not lines:
        return ""

    if any(line.endswith("\n") for line in lines):
        text = "".join(lines)
    else:
        text = "\n".join(lines)

    if text and not text.endswith("\n"):
        text += "\n"

    return text


# ---------------------------------------------------------------------
# Boundary safety
# ---------------------------------------------------------------------

def cut_is_inside_fence(source_lines: list[str], split_line: int) -> bool:
    """
    split_line is the first line of the next concept.

    The cut is between split_line - 1 and split_line.
    Returns True if that cut is inside a fenced code block.

    Fence means:
    - triple-or-longer backtick fence, e.g. ```
    - triple-or-longer tilde fence, e.g. ~~~

    Single inline backticks are NOT fences.
    """

    return cut_is_inside_fence_by_scan(source_lines, split_line)

def cut_is_inside_image_unit(source_lines: list[str], split_line: int) -> bool:
    """
    Returns True if the cut before split_line is inside an <image-unit> block.
    """

    if split_line <= 1:
        return False

    return basic_cut_is_inside_image_unit(source_lines, split_line)


def cut_is_inside_table(source_lines: list[str], split_line: int) -> bool:
    """
    Returns True if the cut before split_line is inside a Markdown table.

    This rejects a cut where both sides look table-ish.
    """

    if split_line <= 1 or split_line > len(source_lines):
        return False

    previous_line = source_lines[split_line - 2]
    current_line = source_lines[split_line - 1]

    return is_tableish_line(previous_line) and is_tableish_line(current_line)


def validate_safe_internal_boundary(
    *,
    source_lines: list[str],
    split_line: int,
) -> str | None:
    """
    Validate that a concept boundary before split_line does not break a block.
    """

    if cut_is_inside_fence(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside a fenced code block."

    if cut_is_inside_image_unit(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside an <image-unit> block."

    if cut_is_inside_table(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside a Markdown table."

    return None


# ---------------------------------------------------------------------
# Coverage validation
# ---------------------------------------------------------------------


def normalize_plan_item(item: ConceptFilePlan) -> ConceptFilePlan:
    title = (item.title or "").strip()

    filename = normalize_filename(
        filename=item.filename,
        title=title or "ドキュメント",
    )

    if not title:
        title = (
            Path(filename).stem.replace("-", " ").replace("_", " ").strip()
            or "無題のトピック"
        )

    return ConceptFilePlan(
        title=title,
        filename=filename,
        source_start=int(item.source_start),
        source_end=int(item.source_end),
        summary=(item.summary or "").strip(),
    )

def copy_plan_with_range(
    item: ConceptFilePlan,
    *,
    source_start: int,
    source_end: int,
) -> ConceptFilePlan:
    """
    Return the same concept item with only source_start/source_end changed.
    """

    return ConceptFilePlan(
        title=item.title,
        filename=item.filename,
        source_start=source_start,
        source_end=source_end,
        summary=item.summary,
    )


def markdown_fence_block_for_cut(
    source_lines: list[str],
    split_line: int,
) -> tuple[int, int] | None:
    """
    Return the fenced block containing the cut before split_line.

    Returns:
        (opening_line, closing_line)

    Example:
    - fence opens at 1179
    - fence closes at 1187
    - split_line=1180 cuts inside it
    - returns (1179, 1187)
    """

    if split_line <= 1:
        return None

    scan = scan_markdown_fences(source_lines)

    if scan.unclosed is not None:
        raise RuntimeError(
            f"Source has an unclosed fenced code block starting at line "
            f"{scan.unclosed.line_number}: {scan.unclosed.raw_line!r}."
        )

    cut_after_line = split_line - 1

    for opening, closing in zip(scan.openings, scan.closings):
        if opening.line_number <= cut_after_line < closing.line_number:
            return opening.line_number, closing.line_number

    return None


def choose_repair_boundary_for_adjacent_files(
    *,
    source_lines: list[str],
    previous_file: ConceptFilePlan,
    current_file: ConceptFilePlan,
    source_start: int,
    source_end: int,
) -> int | None:
    """
    Choose a safe replacement boundary between two adjacent files.

    If current_file starts at an unsafe line, this returns a new source_start
    for current_file. The caller must also set:

        previous_file.source_end = new_boundary - 1
        current_file.source_start = new_boundary

    This is the key part missing from the LLM retry approach.
    """

    bad_boundary = current_file.source_start

    min_boundary = max(source_start + 1, previous_file.source_start + 1)
    max_boundary = min(source_end, current_file.source_end)

    candidates: list[int] = []

    fence_block = markdown_fence_block_for_cut(
        source_lines=source_lines,
        split_line=bad_boundary,
    )

    if fence_block is not None:
        fence_start, fence_end = fence_block

        # Option 1:
        # Put the whole fenced block with the current file.
        # Example:
        # previous ends 1178, current starts 1179.
        candidates.append(fence_start)

        # Option 2:
        # Put the whole fenced block with the previous file.
        # Example:
        # previous ends 1187, current starts 1188.
        candidates.append(fence_end + 1)

    nearby = nearby_safe_boundary_candidates(
        source_lines=source_lines,
        bad_split_line=bad_boundary,
        source_start=source_start,
        source_end=source_end,
        radius=80,
        limit=8,
    )

    candidates.extend(nearby["before"])
    candidates.extend(nearby["after"])

    valid_candidates: list[int] = []

    for candidate in candidates:
        if candidate < min_boundary:
            continue

        if candidate > max_boundary:
            continue

        if not basic_boundary_is_safe(source_lines, candidate):
            continue

        # Make sure neither adjacent range becomes empty.
        if previous_file.source_start > candidate - 1:
            continue

        if candidate > current_file.source_end:
            continue

        valid_candidates.append(candidate)

    if not valid_candidates:
        return None

    # Prefer the closest safe boundary.
    # Tie-breaker: prefer moving backward, because in your actual case
    # line 1179 is the fence opener and should usually travel with the code block.
    valid_candidates = sorted(
        set(valid_candidates),
        key=lambda line: (
            abs(line - bad_boundary),
            0 if line < bad_boundary else 1,
            line,
        ),
    )

    return valid_candidates[0]


def try_auto_repair_concept_partition_boundaries(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    source_start: int,
    source_end: int,
    label: str,
) -> tuple[list[ConceptFilePlan] | None, str | None]:
    """
    Deterministically repair unsafe internal boundaries produced by the LLM.

    This fixes cases like:

        file #3: 941-1179
        file #4: 1180-1203

    where line 1179 is ```c.

    Correct repaired version:

        file #3: 941-1178
        file #4: 1179-1203

    or, depending on nearest safe boundary:

        file #3: 941-1187
        file #4: 1188-1203

    The important point:
    both adjacent ranges must be changed together.
    """

    working = [normalize_plan_item(item) for item in files]

    max_repairs = max(1, len(working) * 2)

    for _ in range(max_repairs):
        accepted, error = validate_concept_partition(
            files=working,
            source_lines=source_lines,
            source_start=source_start,
            source_end=source_end,
            label=label,
        )

        if accepted is not None:
            return accepted, None

        match = BOUNDARY_BEFORE_LINE_RE.search(error or "")

        if not match:
            return None, error

        bad_boundary = int(match.group(1))

        current_index: int | None = None

        for index, item in enumerate(working):
            if index > 0 and item.source_start == bad_boundary:
                current_index = index
                break

        if current_index is None:
            return None, error

        previous_item = working[current_index - 1]
        current_item = working[current_index]

        repaired_boundary = choose_repair_boundary_for_adjacent_files(
            source_lines=source_lines,
            previous_file=previous_item,
            current_file=current_item,
            source_start=source_start,
            source_end=source_end,
        )

        if repaired_boundary is None:
            return None, error

        if repaired_boundary == bad_boundary:
            return None, error

        log.debug(
            f"[Planning] {label}: auto-repairing unsafe boundary "
            f"before line {bad_boundary} -> before line {repaired_boundary}"
        )

        working[current_index - 1] = copy_plan_with_range(
            previous_item,
            source_start=previous_item.source_start,
            source_end=repaired_boundary - 1,
        )

        working[current_index] = copy_plan_with_range(
            current_item,
            source_start=repaired_boundary,
            source_end=current_item.source_end,
        )

    return None, f"{label}: auto-repair exceeded maximum repair attempts."

def validate_concept_partition(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    source_start: int,
    source_end: int,
    label: str,
) -> tuple[list[ConceptFilePlan] | None, str | None]:
    """
    Validates that the model output covers exactly source_start-source_end.

    Rules:
    - first file starts exactly at source_start
    - last file ends exactly at source_end
    - no gaps
    - no overlaps
    - every line is covered, including blank lines
    - no invalid ranges
    - no cuts inside code fences, Markdown tables, or image blocks
    """

    if source_start > source_end:
        if files:
            return None, f"{label}: empty range cannot contain files."

        return [], None

    if not files:
        return None, (
            f"{label}: model returned no files, but it must cover "
            f"lines {source_start}-{source_end}."
        )

    normalized: list[ConceptFilePlan] = []
    expected_start = source_start

    for index, raw_item in enumerate(files, start=1):
        item = normalize_plan_item(raw_item)

        if item.source_start != expected_start:
            return None, (
                f"{label}: file #{index} starts at line {item.source_start}, "
                f"but expected line {expected_start}. This creates a gap or overlap."
            )

        if item.source_end < item.source_start:
            return None, (
                f"{label}: file #{index} has invalid range "
                f"{item.source_start}-{item.source_end}."
            )

        if item.source_start < source_start:
            return None, (
                f"{label}: file #{index} starts before requested range. "
                f"Got {item.source_start}, minimum is {source_start}."
            )

        if item.source_end > source_end:
            return None, (
                f"{label}: file #{index} ends after requested range. "
                f"Got {item.source_end}, maximum is {source_end}."
            )

        if index > 1:
            boundary_error = validate_safe_internal_boundary(
                source_lines=source_lines,
                split_line=item.source_start,
            )

            if boundary_error:
                return None, f"{label}: {boundary_error}"

        normalized.append(item)
        expected_start = item.source_end + 1

    if expected_start != source_end + 1:
        return None, (
            f"{label}: partition ended at line {expected_start - 1}, "
            f"but must end exactly at line {source_end}. Missing lines "
            f"{expected_start}-{source_end}."
        )

    return normalized, None


def assert_concept_coverage(
    *,
    files: list[ConceptFilePlan],
    source_line_count: int,
) -> None:
    """
    Final hard assertion for the whole source document.
    """

    if source_line_count == 0:
        if files:
            raise RuntimeError("Empty source cannot have concept files.")

        return

    expected_start = 1

    for index, item in enumerate(files, start=1):
        if item.source_start != expected_start:
            raise RuntimeError(
                f"Coverage error at file #{index}: expected start line "
                f"{expected_start}, got {item.source_start}."
            )

        if item.source_end < item.source_start:
            raise RuntimeError(
                f"Coverage error at file #{index}: invalid range "
                f"{item.source_start}-{item.source_end}."
            )

        if item.source_end > source_line_count:
            raise RuntimeError(
                f"Coverage error at file #{index}: ends at line "
                f"{item.source_end}, but source has only {source_line_count} lines."
            )

        expected_start = item.source_end + 1

    if expected_start != source_line_count + 1:
        raise RuntimeError(
            f"Coverage incomplete: stopped at line {expected_start - 1}; "
            f"expected coverage through line {source_line_count}."
        )


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------


def build_concept_split_prompt(
    *,
    source_start: int,
    source_end: int,
    source_block: str,
    pending: ConceptFilePlan | None,
    last_error: str | None,
) -> list[Any]:
    pending_text = ""

    if pending is not None:
        pending_text = (
            "前のチャンクから引き継がれた未確定のコンセプトが1件あります。\n"
            "これはまだ保存されていません。\n"
            "このコンセプトを継続、リネーム、維持、または分割して構いませんが、"
            "返却する範囲は必ず下記の要求範囲全体を過不足なくカバーしてください。\n\n"
            f"保留中コンセプトのタイトル: {pending.title}\n"
            f"保留中コンセプトのファイル名: {pending.filename}\n"
            f"保留中コンセプトのソース範囲: "
            f"{pending.source_start}-{pending.source_end}\n\n"
        )

    correction_text = ""

    if last_error:
        correction_text = (
            "前回の回答は無効でした。\n"
            "修正して、有効な分割結果のみを返してください。\n\n"
            f"検証エラー:\n{last_error}\n\n"
        )

    return [
        SystemMessage(
            content=(
                "あなたは番号付きのMarkdown/ソーステキストを、概念ごとのファイルに分割する専門家です。"
                "構造化出力のみを返してください。"
                "title、filename、summary は必ず日本語で作成してください。"
            )
        ),
        HumanMessage(
            content=(
                f"{correction_text}"
                "タスク: 指定された番号付きソース行を、独立した概念/トピック単位のMarkdownファイルに分割してください。\n\n"
                f"要求ソース範囲: {source_start}-{source_end}\n\n"
                f"{pending_text}"
                "出力言語の必須条件:\n"
                "- title は自然な日本語にしてください。\n"
                "- summary は自然な日本語で簡潔に書いてください。\n"
                "- filename は内容を反映した日本語のMarkdownファイル名にしてください。\n"
                "- filename はファイル名のみで、ディレクトリを含めないでください。\n"
                "- filename は必ず .md で終わるようにしてください。\n"
                "- 英語の汎用名、連番だけの名前、意味の薄い名前は避けてください。\n\n"
                "厳格な要件:\n"
                "- 1件以上のファイルを返してください。\n"
                "- 各ファイルには title、filename、source_start、source_end、summary を必ず含めてください。\n"
                "- source_start/source_end は1始まりの両端含むソース行番号です。\n"
                "- 最初のファイルは、要求された source_start から正確に開始してください。\n"
                "- 最後のファイルは、要求された source_end で正確に終了してください。\n"
                "- すべての行をちょうど1回ずつカバーしてください。\n"
                "- 空行も実在する行として扱い、必ずカバーしてください。\n"
                "- 空行を飛ばさないでください。\n"
                "- ギャップを作らないでください。\n"
                "- 重複を作らないでください。\n"
                "- 存在しない行番号を作らないでください。\n"
                "- fenced code block の途中で分割しないでください。\n"
                "- Markdownテーブルの途中で分割しないでください。\n"
                "- <image-unit> ブロックの途中で分割しないでください。\n"
                "- 見出し、関数/クラス/API境界、ひとまとまりのテーブル、明確な概念の切り替わりで分割することを優先してください。\n"
                "- filename は通常のMarkdownファイル名のみです。例: 機能概要.md、APIリファレンス.md\n"
                "- filename にディレクトリを含めないでください。\n\n"
                "引き継ぎルール:\n"
                "- 最後のコンセプトが未完で、次のチャンクに続く可能性が高い場合でも、最後の返却ファイルとして含めてください。\n"
                "- 呼び出し側は最後の返却ファイルを保留し、次のチャンクで再度提示します。\n\n"
                "番号付きソース:\n"
                f"{source_block}"
            )
        ),
    ]


# ---------------------------------------------------------------------
# LLM call with retry until valid
# ---------------------------------------------------------------------

async def split_window_until_valid(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    source_start: int,
    source_end: int,
    pending: ConceptFilePlan | None,
    label: str,
    max_output_tokens: int = 8000,
    stop_check: Callable[[], bool] | None = None,
) -> list[ConceptFilePlan]:
    """
    Calls the model until it returns exact coverage for source_start-source_end.

    This version keeps your existing retry behavior, but adds:
    - global unclosed fence detection before retry loop
    - deterministic boundary repair before asking the LLM again
    - safe boundary suggestions only if repair fails
    """

    assert_no_unclosed_markdown_fences(
        source_lines,
        label="planning source",
    )

    source_block = numbered_source_lines(
        source_lines[source_start - 1 : source_end],
        source_start,
    )

    last_error: str | None = None

    for attempt in range(1, PARTITION_RETRY_ATTEMPTS + 1):
        if stop_check and stop_check():
            raise JobCancelled("chunk planning cancelled")

        log.debug(
            f"[Planning] {label}: split attempt {attempt}, "
            f"source lines {source_start}-{source_end}"
        )

        messages = build_concept_split_prompt(
            source_start=source_start,
            source_end=source_end,
            source_block=source_block,
            pending=pending,
            last_error=last_error,
        )

        try:
            raw_result = await structured_ainvoke(
                llm,
                ConceptSplitResult,
                messages,
                max_output_tokens=max_output_tokens,
                temperature=TEMPERATURE if attempt > 1 else None,
            )

            result = ConceptSplitResult.model_validate(raw_result)

            accepted, error = validate_concept_partition(
                files=result.files,
                source_lines=source_lines,
                source_start=source_start,
                source_end=source_end,
                label=label,
            )

            if accepted is not None:
                return accepted

            repaired, repair_error = try_auto_repair_concept_partition_boundaries(
                files=result.files,
                source_lines=source_lines,
                source_start=source_start,
                source_end=source_end,
                label=label,
            )

            if repaired is not None:
                log.debug(
                    f"[Planning] {label}: accepted after automatic boundary repair."
                )
                return repaired

            last_error = enrich_boundary_error_with_suggestions(
                error=repair_error or error or "Unknown validation error.",
                source_lines=source_lines,
                source_start=source_start,
                source_end=source_end,
            )

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        log.debug(
            f"[Planning] {label}: invalid split on attempt {attempt}; retrying. "
            f"Reason: {last_error}"
        )

        if attempt < PARTITION_RETRY_ATTEMPTS:
            await asyncio.sleep(
                min(30.0, PARTITION_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            )

    if CHUNK_FALLBACK_MECHANICAL:
        fallback_title = f"{label} 自動分割"
        log.debug(
            f"[Planning] {label}: using mechanical fallback after "
            f"{PARTITION_RETRY_ATTEMPTS} attempts. Last error: {last_error}"
        )
        return [
            ConceptFilePlan(
                title=fallback_title,
                filename=normalize_filename(f"{label}.md", fallback_title),
                source_start=source_start,
                source_end=source_end,
                summary="LLMによる分割に失敗したため、元の範囲を機械的に保持しました。",
            )
        ]

    raise ChunkPlanningError(
        f"{label}: could not produce a valid concept split after "
        f"{PARTITION_RETRY_ATTEMPTS} attempts. Last error: {last_error}"
    )

# ---------------------------------------------------------------------
# Streaming concept planning
# ---------------------------------------------------------------------


async def plan_concept_files_streaming(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    target_lines: int = 100,
    max_extra: int = 30,
    concurrency: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> list[ConceptFilePlan]:
    """
    Main planner.

    Behavior:
    - Break source into approximately target_lines chunks.
    - Existing chunker avoids cutting tables/fences/image blocks.
    - For each chunk, ask the model to split into concept files.
    - Commit every returned file except the last one.
    - Keep the last one pending and include it in the next prompt.
    - At the final chunk, commit everything.
    - Validate complete 1-N coverage at the end.
    """

    source_line_count = len(source_lines)

    if source_line_count == 0:
        return []
    
    assert_no_unclosed_markdown_fences(
        source_lines,
        label="planning source",
    )

    chunk_ranges = chunk_source_lines_preserving_tables(
        source_lines,
        target_size=target_lines,
        max_extra=max_extra,
    )

    global_chunks: list[tuple[int, int]] = [
        # Convert zero-based half-open ranges to one-based inclusive ranges.
        (start_idx + 1, end_idx)
        for start_idx, end_idx in chunk_ranges
    ]

    # Sequential on purpose: the last concept of each window is carried into the
    # next prompt so a topic that straddles a window boundary is planned once,
    # whole. The parallel version (b739a17) dropped this and cut topics in half.
    # ``concurrency`` is accepted for API compatibility and intentionally unused.
    committed: list[ConceptFilePlan] = []
    pending: ConceptFilePlan | None = None

    for chunk_index, (chunk_start, chunk_end) in enumerate(global_chunks, start=1):
        if stop_check and stop_check():
            raise JobCancelled("chunk planning cancelled")

        is_final_chunk = chunk_index == len(global_chunks)

        if pending is not None:
            prompt_start = pending.source_start
        else:
            prompt_start = chunk_start

        prompt_end = chunk_end
        label = f"chunk {chunk_index}/{len(global_chunks)}"

        split = await split_window_until_valid(
            llm=llm,
            source_lines=source_lines,
            source_start=prompt_start,
            source_end=prompt_end,
            pending=pending,
            label=label,
            stop_check=stop_check,
        )

        if not split:
            raise RuntimeError(f"{label}: valid split unexpectedly returned no files.")

        if is_final_chunk:
            committed.extend(split)
            pending = None
        else:
            committed.extend(split[:-1])
            pending = split[-1]

            log.debug(
                "[Planning] Carrying pending concept forward: "
                f"{pending.title} [{pending.source_start}-{pending.source_end}]"
            )

    if pending is not None:
        committed.append(pending)

    accepted, error = validate_concept_partition(
        files=committed,
        source_lines=source_lines,
        source_start=1,
        source_end=source_line_count,
        label="final full-source concept plan",
    )

    if accepted is None:
        raise RuntimeError(f"Final concept plan failed validation: {error}")

    assert_concept_coverage(
        files=accepted,
        source_line_count=source_line_count,
    )

    return accepted


# ---------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------


def write_concept_plan_document(
    *,
    path: Path,
    source_path: Path,
    source_line_count: int,
    files: list[ConceptFilePlan],
) -> None:
    """
    Writes a human-readable Markdown planning file.

    This is stored in _planning only.
    It is not one of the final docs.
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# 概念ファイル計画",
        "",
        f"- ソース: `{source_path}`",
        f"- ソース行数: `{source_line_count}`",
        f"- 概念ファイル数: `{len(files)}`",
        "",
        "## ファイル一覧",
        "",
    ]

    for index, item in enumerate(files, start=1):
        summary = f" — {item.summary}" if item.summary else ""

        lines.append(
            f"{index}. `{item.source_start}-{item.source_end}` → "
            f"`{item.filename}` — **{item.title}**{summary}"
        )

    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------


def manifest_add_file_record(
    *,
    manifest: dict[str, Any],
    filename: str,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
    order: int,
) -> None:
    """
    Store document metadata in manifest.json.

    This is where title, summary, range, and doc order live.
    The generated Markdown doc itself remains raw source content only.
    """

    record = {
        "order": order,
        "filename": filename,
        "title": title,
        "summary": summary,
        "source_start": source_start,
        "source_end": source_end,
        "updated_at": utc_now_iso(),
    }

    files = manifest.setdefault("files", [])

    if isinstance(files, list):
        files.append(record)
    elif isinstance(files, dict):
        files[filename] = record
    else:
        manifest["files"] = [record]


def manifest_add_chunk_record(
    *,
    manifest: dict[str, Any],
    filename: str,
    source_start: int,
    source_end: int,
    order: int,
) -> None:
    """
    Store source coverage assignment in manifest.json.
    """

    record = {
        "order": order,
        "source_start": source_start,
        "source_end": source_end,
        "action": "concept_file_created",
        "targets": [
            {
                "filename": filename,
                "source_start": source_start,
                "source_end": source_end,
            }
        ],
        "reason": "ストリーミング方式による概念単位の分割。",
        "updated_at": utc_now_iso(),
    }

    chunks = manifest.setdefault("chunks", [])

    if isinstance(chunks, list):
        chunks.append(record)
    else:
        manifest["chunks"] = [record]


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------


def write_concept_markdown_file(
    *,
    path: Path,
    source_body: str,
) -> None:
    """
    Writes one final Markdown doc.

    IMPORTANT:
    - No frontmatter.
    - No generated heading.
    - No source line text.
    - No summary.
    - Only the original source slice.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_body, encoding="utf-8")


def render_concept_files(
    *,
    docs_dir: Path,
    source_lines: list[str],
    files: list[ConceptFilePlan],
    manifest: dict[str, Any],
    output_root: Path,
) -> list[dict[str, Any]]:
    """
    Render final concept files to docs/.

    Filenames get source-order numeric prefixes.

    Returns coverage records.
    """

    docs_dir.mkdir(parents=True, exist_ok=True)

    ordered_files = sorted(
        files,
        key=lambda item: (item.source_start, item.source_end),
    )

    total = len(ordered_files)
    coverage: list[dict[str, Any]] = []

    for index, item in enumerate(ordered_files, start=1):
        normalized = normalize_plan_item(item)

        base_filename = normalize_filename(
            filename=normalized.filename,
            title=normalized.title,
        )

        final_filename = numbered_output_filename(
            index=index,
            total=total,
            filename=base_filename,
        )

        output_path = make_unique_output_path(docs_dir, final_filename)
        relative_filename = output_path.relative_to(output_root).as_posix()

        selected_lines = source_lines[
            normalized.source_start - 1 : normalized.source_end
        ]

        source_body = join_original_source_lines(selected_lines)

        write_concept_markdown_file(
            path=output_path,
            source_body=source_body,
        )

        summary = (
            normalized.summary
            or f"ソース行 {normalized.source_start}-{normalized.source_end}。"
        )

        manifest_add_file_record(
            manifest=manifest,
            filename=relative_filename,
            title=normalized.title,
            summary=summary,
            source_start=normalized.source_start,
            source_end=normalized.source_end,
            order=index,
        )

        manifest_add_chunk_record(
            manifest=manifest,
            filename=relative_filename,
            source_start=normalized.source_start,
            source_end=normalized.source_end,
            order=index,
        )

        coverage.append(
            {
                "order": index,
                "source_start": normalized.source_start,
                "source_end": normalized.source_end,
                "filename": relative_filename,
                "title": normalized.title,
                "summary": summary,
            }
        )

    return coverage


# ---------------------------------------------------------------------
# Enrichment Schemas & Prompts (Sequential Headers & Global Filename)
# ---------------------------------------------------------------------


class FileHeader(BaseModel):
    header: str = Field(
        description="このチャンクに対して推定した日本語の論理見出し。最大2階層程度。"
    )


class EnrichmentResult(BaseModel):
    inferred_file_name: str = Field(
        description="文書全体を表す、内容に即した日本語のMarkdownファイル名。必ず .md で終わる。例: 設定管理マニュアル.md"
    )
    files: list[FileHeader] = Field(
        description="入力チャンクと完全に同じ順序の、日本語の推定見出し一覧。"
    )


class ChunkHeader(BaseModel):
    header: str = Field(
        description="このチャンクの日本語の論理見出し。前のセクションの続きなら前回と完全に同じ見出しを返す。新しいセクションなら新しい日本語見出しを返す。最大2階層程度。"
    )


class GlobalName(BaseModel):
    inferred_file_name: str = Field(
        description="文書全体を表す、内容に即した日本語のMarkdownファイル名。必ず .md で終わる。"
    )


def build_first_chunk_prompt(
    original_filename: str, current: ConceptFilePlan
) -> list[Any]:
    return [
        SystemMessage(
            content=(
                "あなたは文書チャンクに論理見出しを付与する専門的なテクニカルライターです。"
                "回答は必ず日本語にしてください。"
            )
        ),
        HumanMessage(
            content=(
                f"元の文書名: '{original_filename}'\n\n"
                f"最初のチャンク情報:\n"
                f"- ファイル名: {current.filename}\n"
                f"- タイトル: {current.title}\n"
                f"- 要約: {current.summary}\n\n"
                "タスク: この最初のチャンクに対して、論理的な日本語見出しを付けてください。"
                "見出しは最大2階層程度にしてください。"
                "例: '文書管理', 'APIリファレンス', '設定手順'。"
                "見出しには '1.' や '2.' のような連番を使わないでください。"
            )
        ),
    ]


def build_subsequent_chunk_prompt(
    original_filename: str,
    current: ConceptFilePlan,
    prev: ConceptFilePlan,
    prev_header: str,
) -> list[Any]:
    return [
        SystemMessage(
            content=(
                "あなたは文書チャンクに順番に論理見出しを付与する専門的なテクニカルライターです。"
                "回答は必ず日本語にしてください。"
            )
        ),
        HumanMessage(
            content=(
                f"元の文書名: '{original_filename}'\n\n"
                f"前のチャンク情報:\n"
                f"- タイトル: {prev.title}\n"
                f"- 要約: {prev.summary}\n"
                f"- 割り当て済み見出し: {prev_header}\n\n"
                f"現在のチャンク情報:\n"
                f"- ファイル名: {current.filename}\n"
                f"- タイトル: {current.title}\n"
                f"- 要約: {current.summary}\n\n"
                "タスク: 現在のチャンクの論理見出しを決定してください。\n"
                "1. 現在のチャンクが前のチャンクと同じ論理セクションの続きである場合、"
                "前回と完全に同じ見出しを出力してください。"
                "例: 同じAPIリファレンス内の別関数、同じ定義ファイル群、同一章の続きなど。\n"
                "2. 新しい論理セクションが始まる場合は、新しい説明的な日本語見出しを出力してください。"
                "見出しは最大2階層程度にしてください。\n"
                "3. '1.' や '2.' のような連番は使わないでください。"
            )
        ),
    ]


def build_global_name_prompt(
    original_filename: str, files: list[ConceptFilePlan]
) -> list[Any]:
    summaries = "\n".join(f"- {f.title}: {f.summary}" for f in files)
    return [
        SystemMessage(
            content=(
                "あなたは専門的なテクニカルライターです。"
                "回答は必ず日本語にしてください。"
            )
        ),
        HumanMessage(
            content=(
                f"元のアップロードファイル名: '{original_filename}'\n\n"
                "以下は文書チャンクのタイトルと要約の一覧です:\n"
                f"{summaries}\n\n"
                "タスク: 文書全体を表す、単一の説明的な日本語Markdownファイル名を推定してください。"
                "必ず .md で終わるファイル名にしてください。"
                "元のファイル名だけでなく、実際の内容を反映してください。"
                "ディレクトリは含めず、ファイル名のみを返してください。"
                "例: 設定管理マニュアル.md、API仕様書.md、運用手順書.md"
            )
        ),
    ]


async def enrich_concept_plan(
    *,
    llm: ChatOpenAI,
    original_filename: str,
    files: list[ConceptFilePlan],
    stop_check: Callable[[], bool] | None = None,
) -> EnrichmentResult:
    """
    Sequentially infers headers for each file and a global filename.
    """
    if not files:
        return EnrichmentResult(inferred_file_name="document.md", files=[])

    log.debug("[Enrichment] Inferring global name...")
    try:
        global_name_raw = await structured_ainvoke(
            llm,
            GlobalName,
            build_global_name_prompt(original_filename, files),
            max_output_tokens=100,
        )
        global_name = GlobalName.model_validate(global_name_raw).inferred_file_name
    except Exception as e:
        log.debug(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
        global_name = "ドキュメント.md"

    if not global_name.endswith(".md"):
        global_name += ".md"
    global_name = normalize_filename(global_name, "ドキュメント")

    inferred_headers = []

    # First chunk
    log.debug(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    if stop_check and stop_check():
        raise JobCancelled("chunk enrichment cancelled")
    try:
        first_raw = await structured_ainvoke(
            llm,
            ChunkHeader,
            build_first_chunk_prompt(original_filename, files[0]),
            max_output_tokens=100,
        )
        first_header = ChunkHeader.model_validate(first_raw).header
    except Exception as e:
        log.debug(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
        first_header = "一般"
    inferred_headers.append(first_header)

    # Subsequent chunks
    for i in range(1, len(files)):
        if stop_check and stop_check():
            raise JobCancelled("chunk enrichment cancelled")
        log.debug(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
        prompt = build_subsequent_chunk_prompt(
            original_filename=original_filename,
            current=files[i],
            prev=files[i - 1],
            prev_header=inferred_headers[-1],
        )
        try:
            raw = await structured_ainvoke(llm, ChunkHeader, prompt, max_output_tokens=100)
            header = ChunkHeader.model_validate(raw).header
        except Exception as e:
            log.debug(
                f"[Enrichment] Failed to infer header for chunk {i+1}: {e}. "
                "Using fallback."
            )
            header = "一般"
        inferred_headers.append(header)

    return EnrichmentResult(
        inferred_file_name=global_name,
        files=[FileHeader(header=h) for h in inferred_headers],
    )


# ---------------------------------------------------------------------
# New JSON Writers
# ---------------------------------------------------------------------


def write_coverage_json(
    *,
    path: Path,
    source_line_count: int,
    files: list[ConceptFilePlan],
    headers: list[str],
) -> None:
    """
    Writes coverage.json.
    Contains detailed source mapping, summaries, and inferred headers.
    """
    file_records = []
    for item, header in zip(files, headers):
        record = item.model_dump()
        record["header"] = header
        file_records.append(record)

    write_json(
        path,
        {
            "source_line_count": source_line_count,
            "file_count": len(file_records),
            "files": file_records,
        },
    )


def write_metadata_json(
    *,
    path: Path,
    original_file_name: str,
    inferred_file_name: str,
    files: list[ConceptFilePlan],
    headers: list[str],
) -> None:
    """
    Writes metadata.json.
    Lightweight mapping for graphing/renaming.
    """
    file_records = [
        {
            "name": item.filename,
            "header": header,
        }
        for item, header in zip(files, headers)
    ]

    write_json(
        path,
        {
            "original_file_name": original_file_name,
            "inferred_file_name": inferred_file_name,
            "files": file_records,
        },
    )


# endregion Planning


# region Phases (Main)


def assert_rendered_docs_match_source(
    *,
    source_lines: list[str],
    coverage: list[dict[str, Any]],
    output_root: Path,
) -> None:
    source_line_count = len(source_lines)

    if source_line_count == 0:
        if coverage:
            raise RuntimeError("Empty source cannot have rendered coverage records.")
        return

    ordered = sorted(
        coverage,
        key=lambda item: (
            int(item["source_start"]),
            int(item["source_end"]),
        ),
    )

    expected_start = 1

    for index, item in enumerate(ordered, start=1):
        source_start = int(item["source_start"])
        source_end = int(item["source_end"])
        filename = str(item["filename"])

        if source_start != expected_start:
            raise RuntimeError(
                f"Rendered coverage gap/overlap at record #{index}: "
                f"expected source_start={expected_start}, got {source_start}."
            )

        if source_end < source_start:
            raise RuntimeError(
                f"Rendered coverage invalid range at record #{index}: "
                f"{source_start}-{source_end}."
            )

        if source_end > source_line_count:
            raise RuntimeError(
                f"Rendered coverage out of bounds at record #{index}: "
                f"ends at {source_end}, but source has {source_line_count} lines."
            )

        output_path = output_root / filename

        if not output_path.exists():
            raise RuntimeError(
                f"Rendered file missing for source lines "
                f"{source_start}-{source_end}: {filename}"
            )

        expected_text = join_original_source_lines(
            source_lines[source_start - 1 : source_end]
        )

        actual_text = output_path.read_text(encoding="utf-8")

        if actual_text != expected_text:
            raise RuntimeError(
                f"Rendered file does not exactly match source slice: {filename} "
                f"for source lines {source_start}-{source_end}."
            )

        expected_start = source_end + 1

    if expected_start != source_line_count + 1:
        raise RuntimeError(
            f"Rendered coverage incomplete: stopped at line {expected_start - 1}; "
            f"expected coverage through line {source_line_count}."
        )


def run_chunk_pipeline(
    *,
    source_text: str,
    document_name: str,
    out_dir: Path,
    llm: Any = None,
    concurrency: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> SimpleNamespace:
    return _run_async_blocking(
        arun_chunk_pipeline(
            source_text=source_text,
            document_name=document_name,
            out_dir=out_dir,
            llm=llm,
            concurrency=concurrency,
            on_progress=on_progress,
            stop_check=stop_check,
        )
    )


async def arun_chunk_pipeline(
    *,
    source_text: str,
    document_name: str,
    out_dir: Path,
    llm: Any = None,
    concurrency: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> SimpleNamespace:
    source_path = Path(document_name)
    docs_dir = out_dir / "docs"
    planning_dir = out_dir / "_planning"

    if CLEAN_OUTPUT and out_dir.exists():
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)

    source_lines = source_text.splitlines()
    source_line_count = len(source_lines)

    if stop_check and stop_check():
        raise JobCancelled("chunk pipeline cancelled")

    manifest = init_manifest(source_path)

    plan_md_path = planning_dir / "concept-plan.md"
    coverage_json_path = planning_dir / "coverage.json"
    metadata_json_path = planning_dir / "metadata.json"
    manifest_json_path = planning_dir / "manifest.json"

    if llm is None:
        raise ValueError("legacy wiki pipeline requires an LLM")

    if on_progress:
        on_progress(
            {
                "stage": "チャンク分割",
                "step": "planning",
                "source_line_count": source_line_count,
            }
        )

    if source_line_count == 0:
        concept_files = []
        coverage = []
        inferred_headers = []
        inferred_global_name = normalize_filename(
            source_path.with_suffix(".md").name,
            "ドキュメント",
        )

    else:
        concept_files = await plan_concept_files_streaming(
            llm=llm,
            source_lines=source_lines,
            target_lines=100,
            max_extra=MAX_CHUNK_EXTRA,
            concurrency=concurrency,
            stop_check=stop_check,
        )

        assert_concept_coverage(
            files=concept_files,
            source_line_count=source_line_count,
        )

        coverage = render_concept_files(
            docs_dir=docs_dir,
            source_lines=source_lines,
            files=concept_files,
            manifest=manifest,
            output_root=out_dir,
        )

        assert_rendered_docs_match_source(
            source_lines=source_lines,
            coverage=coverage,
            output_root=out_dir,
        )

        if on_progress:
            on_progress(
                {
                    "stage": "チャンク分割",
                    "step": "metadata",
                    "file_count": len(concept_files),
                }
            )

        enrichment_result = await enrich_concept_plan(
            llm=llm,
            original_filename=source_path.name,
            files=concept_files,
            stop_check=stop_check,
        )

        inferred_headers = [f.header for f in enrichment_result.files]
        inferred_global_name = enrichment_result.inferred_file_name

    ordered_concept_files = sorted(
        concept_files,
        key=lambda item: (
            item.source_start,
            item.source_end,
        ),
    )

    if source_line_count > 0:
        paired = sorted(
            zip(concept_files, inferred_headers),
            key=lambda x: (x[0].source_start, x[0].source_end),
        )
        ordered_concept_files = [p[0] for p in paired]
        inferred_headers = [p[1] for p in paired]

    manifest.setdefault("planning", {})
    manifest["planning"]["strategy"] = "streaming_concept_file_split"
    manifest["planning"]["target_lines"] = 100
    manifest["planning"]["max_chunk_extra"] = MAX_CHUNK_EXTRA
    manifest["planning"]["docs_dir"] = "docs"
    manifest["planning"]["file_count"] = len(ordered_concept_files)
    manifest["planning"]["coverage_verified_at"] = utc_now_iso()
    manifest["planning"]["render_integrity"] = "exact_source_slice_match"
    manifest["planning"]["inferred_global_name"] = inferred_global_name
    manifest["planning"]["files"] = [
        {
            "order": index,
            "header": header,
            **item.model_dump(),
        }
        for index, (item, header) in enumerate(
            zip(ordered_concept_files, inferred_headers),
            start=1,
        )
    ]

    manifest["coverage"] = coverage
    manifest["updated_at"] = utc_now_iso()

    write_concept_plan_document(
        path=plan_md_path,
        source_path=source_path,
        source_line_count=source_line_count,
        files=ordered_concept_files,
    )

    write_coverage_json(
        path=coverage_json_path,
        source_line_count=source_line_count,
        files=ordered_concept_files,
        headers=inferred_headers,
    )

    write_metadata_json(
        path=metadata_json_path,
        original_file_name=source_path.name,
        inferred_file_name=inferred_global_name,
        files=ordered_concept_files,
        headers=inferred_headers,
    )

    write_json(manifest_json_path, manifest)

    if on_progress:
        on_progress(
            {
                "stage": "チャンク分割",
                "step": "done",
                "file_count": len(ordered_concept_files),
            }
        )

    return SimpleNamespace(
        file_count=len(ordered_concept_files),
        out_dir=out_dir,
        docs_dir=docs_dir,
        planning_dir=planning_dir,
        source_line_count=source_line_count,
        coverage_path=coverage_json_path,
        metadata_path=metadata_json_path,
        manifest_path=manifest_json_path,
    )


def _run_async_blocking(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in error_box:
        raise error_box["error"]

    return result_box["result"]


# endregion Phases (Main)
