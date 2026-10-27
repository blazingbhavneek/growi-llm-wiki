"""pptx: slides are atoms and one deck-level call chooses sections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, Field

from graph.wiki.markdown_blocks import IMAGE_UNIT_CLOSE, IMAGE_UNIT_OPEN
from graph.wiki.schemas import CompiledSeedPlan
from graph.wiki.wire import SeedRange

from .tree import lead


@dataclass
class Slide:
    number: int
    start: int
    end: int
    title: str
    text_lines: int
    first_text: str
    has_table: bool

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def split_slides(lines: Sequence[str], *, delimiter: str, title: str) -> list[Slide]:
    delim, title_re = re.compile(delimiter), re.compile(title)
    starts = [(n, m) for n, line in enumerate(lines, 1) if (m := delim.match(line))]
    slides: list[Slide] = []
    for index, (start, match) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        number = int(match.group(1)) if match.groups() else index + 1
        heading, text, first, table, inside = "", 0, "", False, False
        for line in lines[start:end]:
            stripped = line.strip()
            if stripped.startswith(IMAGE_UNIT_OPEN):
                inside = True
            if inside:
                inside = not stripped.startswith(IMAGE_UNIT_CLOSE)
                continue
            if not stripped:
                continue
            if (match_title := title_re.match(stripped)) and not heading:
                heading = match_title.group(1).strip()
                continue
            if stripped.startswith(("|", "<table")):
                table = True
            text += 1
            first = first or stripped[:80]
        slides.append(Slide(number, start, end, heading, text, first, table))
    return slides


def divider_candidates(slides: Sequence[Slide]) -> list[int]:
    return [s.number for s in slides if (s.title and s.text_lines <= 2) or s.text_lines <= 2]


class Section(BaseModel):
    start_slide: int = 0
    title: str = ""


class DeckSections(BaseModel):
    sections: list[Section] = Field(default_factory=list)


async def judge_sections(
    slides: Sequence[Slide], *, model: Any, language: str,
    attempts: int = 1, retry_temperature: float | None = None,
    stop_check=None,
) -> list[Section]:
    from langchain_core.messages import HumanMessage

    candidates = divider_candidates(slides)
    rows = "\n".join(f"{s.number}\t{'★' if s.number in candidates else ' '}\t{s.text_lines}行\t{s.title or s.first_text}" for s in slides)
    prompt = (
        "これはプレゼンテーションの各スライドの一覧です。内容のまとまりごとに、"
        "新しいセクションが始まるスライドと見出しを決めてください。スライド1は必ず最初です。\n\n"
        "スライド番号\t区切り候補\t本文行数\t見出し/冒頭\n" + rows + f"\n\n出力言語: {language}。JSON のみ。"
    )
    numbers = {s.number for s in slides}
    feedback = ""
    starts: list[tuple[int, str]] = []
    for _attempt in range(max(1, attempts)):
        if stop_check and stop_check():
            raise RuntimeError("slide section planning cancelled")
        correction = (
            "\n\n前回の構造化結果は検証に失敗した。スライド番号を修正して返すこと。\n"
            f"検証エラー: {feedback}\n"
            if feedback else ""
        )
        try:
            result = await model.structured(
                DeckSections,
                [HumanMessage(content=prompt + correction)],
                temperature=retry_temperature,
            )
            raw_starts = [int(section.start_slide) for section in result.sections]
            invalid = sorted(set(raw_starts) - numbers)
            starts = sorted(
                {s.start_slide: s.title for s in result.sections if s.start_slide in numbers}.items()
            )
            if invalid:
                feedback = f"存在しないスライド番号を指定した: {invalid}"
            elif not starts:
                feedback = "有効なセクション開始スライドが1件もない"
            elif starts[0][0] != min(numbers):
                feedback = f"最初のセクションはスライド{min(numbers)}から開始する必要がある"
            else:
                break
        except Exception as exc:
            starts = []
            feedback = f"{type(exc).__name__}: {exc}"
    if not starts or starts[0][0] != min(numbers):
        starts = [(n, next(s.title or s.first_text for s in slides if s.number == n)) for n in (candidates or [min(numbers)])]
        if starts[0][0] != min(numbers):
            starts.insert(0, (min(numbers), slides[0].title or slides[0].first_text or "はじめに"))
    return [Section(start_slide=n, title=t or f"スライド {n} から") for n, t in starts]


async def plan(lines: Sequence[str], *, config: Any, model: Any, on_progress=None, stop_check=None) -> CompiledSeedPlan | None:
    slides = split_slides(lines, delimiter=config.slide_delimiter, title=config.slide_title)
    if len(slides) < 2:
        return None
    sections = await judge_sections(
        slides,
        model=model,
        language=config.output_language,
        attempts=config.planner_attempts,
        retry_temperature=config.retry_temperature,
        stop_check=stop_check,
    )
    starts = [s.start_slide for s in sections]
    groups = []
    for index, section in enumerate(sections):
        upper = starts[index + 1] if index + 1 < len(starts) else 10**9
        groups.append((section, [s for s in slides if section.start_slide <= s.number < upper]))
    merged = []
    for section, group in groups:
        if merged and sum(s.text_lines for s in group) < 10:
            previous, previous_group = merged.pop()
            merged.append((previous, previous_group + group))
        else:
            merged.append((section, group))
    target = int(config.structure_target_lines)
    deck_title = next((line.lstrip("# ").strip() for line in lines[:3] if line.startswith("# ")), "プレゼンテーション")
    pages: list[SeedRange] = []
    preamble_end = slides[0].start - 1
    pack_preamble = 0 < preamble_end < 20
    if preamble_end >= 20:
        pages.append(SeedRange(title=deck_title, summary=lead(lines, 1, preamble_end), chapter=deck_title, source_start=1, source_end=preamble_end, path=[deck_title]))
    for section, group in merged:
        start, end = group[0].start, group[-1].end
        if not pages and pack_preamble:
            start = 1
        chain = [deck_title, section.title]
        if end - start + 1 <= target:
            pages.append(SeedRange(title=section.title, summary=lead(lines, start, end), chapter=" › ".join(chain), source_start=start, source_end=end, path=chain))
            continue
        pieces = _slide_divisor(group, target=target)
        pages.extend(SeedRange(title=f"{section.title}（{i}/{len(pieces)}）", summary=lead(lines, s, e), chapter=" › ".join(chain), source_start=s, source_end=e, path=chain) for i, (s, e) in enumerate(pieces, 1))
    return CompiledSeedPlan(summary="slide-section plan", pages=pages)


def _slide_divisor(group: Sequence[Slide], *, target: int) -> list[tuple[int, int]]:
    import math

    total = group[-1].end - group[0].start + 1
    d = 2
    while math.ceil(total / d) > target:
        d += 1
    per = math.ceil(len(group) / d)
    return [(chunk[0].start, chunk[-1].end) for i in range(0, len(group), per) if (chunk := group[i : i + per])]
