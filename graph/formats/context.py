"""Hierarchy context: one structured model call per parent."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from common.policy import policy_of
from graph.wiki.storage import read_json, write_json_atomic

from .tree import lead

log = logging.getLogger(__name__)


class PageLine(BaseModel):
    number: int = 0
    summary: str = ""


class ParentSummary(BaseModel):
    parent_summary: str = ""
    pages: list[PageLine] = Field(default_factory=list)


def _prompt(chain: Sequence[str], pages: Sequence[Any], lines: Sequence[str], language: str) -> str:
    heading = " › ".join(chain) or "（文書全体）"
    rows = "\n".join(
        f"- #{page.number} 「{page.title}」（原文 {page.owner_ranges[0][0]}-{page.owner_ranges[-1][1]}行）: "
        f"{lead(lines, page.owner_ranges[0][0], page.owner_ranges[-1][1], limit=400)}"
        for page in pages
    )
    return (
        f"次のセクション「{heading}」に属するページ一覧です。各ページの冒頭を示します。\n\n{rows}\n\n"
        "1) セクション全体が何について述べているかを2〜4文でparent_summaryに書く。\n"
        "2) 各ページを1行（60字以内）で要約し、番号を一致させる。\n"
        f"出力言語: {language}。JSON のみを返す。"
    )


async def summarize_hierarchy(
    pages: Sequence[Any], lines: Sequence[str], *, model: Any, config: Any,
    checkpoint: Path, stop_check=None,
) -> dict[str, str]:
    from langchain_core.messages import HumanMessage

    deterministic = await policy_of(config).hierarchy(pages, lines, checkpoint=checkpoint)
    if deterministic is not None:
        return deterministic
    document = getattr(config, "document_slug", "")
    try:
        cached = read_json(checkpoint, default={}) if checkpoint.exists() else {}
    except (OSError, ValueError):
        # graph/wiki/storage logs the file error (path + errno, or corrupt-vs-missing) on the line above.
        log.error("階層の保存情報を読み込めません: 文書=%s、ファイル=%s", document, checkpoint, exc_info=True)
        raise
    if cached.get("policy"):
        cached = {}  # written without the model by another policy; never reuse it as model output
    groups: dict[tuple[str, ...], list[Any]] = {}
    for page in pages:
        groups.setdefault(tuple(page.path[:-1]), []).append(page)
    parents: dict[str, str] = dict(cached.get("parents", {}))
    summaries: dict[str, str] = dict(cached.get("pages", {}))
    for chain, group in groups.items():
        key = " › ".join(chain)
        if key in parents and all(str(page.number) in summaries for page in group):
            continue
        if stop_check and stop_check():
            raise RuntimeError("context cancelled")
        try:
            result = await model.structured(ParentSummary, [HumanMessage(content=_prompt(chain, group, lines, config.output_language))])
            by_number = {int(item.number): item.summary.strip() for item in result.pages}
        except Exception as exc:
            log.error("節の要約生成に失敗したため、元のテキストの冒頭を使います: 文書=%s、節=%s、ページ=%s、エラー=%s: %s", document, key, group[0].title, type(exc).__name__, exc, exc_info=True)
            result, by_number = ParentSummary(), {}
        parents[key] = result.parent_summary.strip() or lead(lines, group[0].owner_ranges[0][0], group[-1].owner_ranges[-1][1], limit=400)
        for page in group:
            summaries[str(page.number)] = by_number.get(page.number) or page.summary or lead(lines, page.owner_ranges[0][0], page.owner_ranges[-1][1])
        try:
            write_json_atomic(checkpoint, {"parents": parents, "pages": summaries})
        except OSError:
            log.error("階層の要約を保存できません。次回もう一度計算します: 文書=%s、ファイル=%s", document, checkpoint, exc_info=True)
            raise
    for page in pages:
        page.summary = summaries.get(str(page.number), page.summary)
    log.info("階層ごとの要約を作成しました: 文書=%s、親階層数=%d、ページ数=%d、ファイル=%s", document, len(parents), len(summaries), checkpoint)
    return parents


def context_block(page: Any, pages: Sequence[Any], parents: dict[str, str], *, limit: int = 12) -> str:
    if not page.path:
        return ""
    key = " › ".join(page.path[:-1])
    siblings = [p for p in pages if tuple(p.path[:-1]) == tuple(page.path[:-1]) and p.number != page.number]
    previous = next((p for p in pages if p.number == page.number - 1), None)
    following = next((p for p in pages if p.number == page.number + 1), None)
    out = ["## 文脈", f"階層: {' › '.join(page.path)}"]
    if parents.get(key):
        out.append(f"親セクションの要約: {parents[key]}")
    if siblings:
        out.append("同じ親の他のページ:")
        out.extend(f"- {p.title} — {p.summary[:120]}" for p in siblings[:limit])
    if previous:
        out.append(f"前のページ: {previous.title} — {previous.summary[:120]}")
    if following:
        out.append(f"次のページ: {following.title} — {following.summary[:120]}")
    return "\n".join(out) + "\n"
