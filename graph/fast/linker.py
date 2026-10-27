"""Fast linker behaviour: batched chunk metadata, no curator call, Jev-only edge leads."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from graph.clients.chat import extract_json_from_text
from graph.linker.chunks import (
    META_MAX_TOKENS, META_TEMPERATURE, Chunk, _is_lead, meta_text, validate_meta,
)
from graph.linker.prompts import CHUNK_META_VERSION, Prompt, chunk_meta_prompt
from graph.linker.wire import ChunkMeta
from graph.wiki.storage import write_json_atomic, write_text_atomic

log = logging.getLogger(__name__)

# Sections per page call. ponytail: guesses until a GPU run; raise if the answers stay
# complete, lower if pages fall back to per-section calls.
META_BATCH_SECTIONS = 4
META_BATCH_CHARS = 12000


# -- batched chunk metadata ---------------------------------------------------------

def chunk_meta_batch_prompt(
    *, page_title: str, document: str, sections: list[tuple[str, str, str]], output_language: str,
) -> Prompt:
    """Several sections of one page in one call, same fields as chunk_meta_prompt."""
    text = "\n\n".join(f"### [{key}] {heading or '(導入)'}\n{body}" for key, heading, body in sections)
    single = chunk_meta_prompt(page_title=page_title, heading="（下記の各節）", document=document,
                               text=text, output_language=output_language)
    item = ChunkMeta.model_json_schema()
    defs = item.pop("$defs", {})
    item["properties"].pop("role_judge", None)
    item["properties"] = {"section": {"type": "string"}, **item["properties"]}
    schema = {"type": "object", "properties": {"sections": {"type": "array", "items": item}},
              "required": ["sections"], "$defs": defs}
    return Prompt(
        kind="chunk_meta_batch",
        version=f"{CHUNK_META_VERSION}:batch-v1",
        system=single.system.split("\nJSON形式:\n")[0]
        + "\n本文は「### [S番号] 見出し」で区切られた複数の節である。各節を独立に読み、下記の項目を節ごとに記述し、"
        "section にその S番号を入れて全ての節を返す。\nJSON形式:\n" + json.dumps(schema, ensure_ascii=False),
        body=single.body,
    )


def _page_batches(items: list[Chunk]) -> list[list[Chunk]]:
    batches: list[list[Chunk]] = []
    size = 0
    for item in items:
        text = len(meta_text(item))
        if not batches or batches[-1][0].page_rel != item.page_rel or len(batches[-1]) >= META_BATCH_SECTIONS or size + text > META_BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(item)
        size += text
    return batches


async def describe_pages(chunks: Sequence[Chunk], *, model: Any, output_language: str, concurrency: int,
                         cache: dict[str, ChunkMeta], artifact_dir: Path | None,
                         stop_check: Callable[[], bool] | None) -> tuple[dict[str, ChunkMeta], int]:
    """One metadata call per page, split when oversized.

    Returns metadata by text hash. A section missing, invalid or empty in the answer is
    left out, so describe_all retries it with its own call.
    """
    sectioned = {item.page_rel for item in chunks if item.heading}
    batches = _page_batches([item for item in chunks if item.text_sha256 not in cache and not _is_lead(item, sectioned)])
    semaphore = asyncio.Semaphore(max(1, concurrency))
    found: dict[str, ChunkMeta] = {}

    async def describe(number: int, group: list[Chunk]) -> None:
        if stop_check and stop_check():
            raise RuntimeError("linker cancelled")
        prompt = chunk_meta_batch_prompt(
            page_title=group[0].title, document=group[0].document, output_language=output_language,
            sections=[(f"S{index}", item.heading, meta_text(item)) for index, item in enumerate(group, 1)],
        )
        stem = f"meta-batch-{number}-{Path(group[0].filename).stem}"
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(artifact_dir / f"{stem}.prompt.md", prompt.render())
        async with semaphore:
            try:
                raw = await model.text(prompt.messages(), max_output_tokens=META_MAX_TOKENS * 2, temperature=META_TEMPERATURE)
                answer = extract_json_from_text(raw)
            except Exception as exc:  # noqa: BLE001 - every section falls back to its own call
                if artifact_dir:
                    write_text_atomic(artifact_dir / f"{stem}-error.txt", f"{type(exc).__name__}: {exc}")
                return
        if artifact_dir:
            write_json_atomic(artifact_dir / f"{stem}.json", answer)
        for value in answer.get("sections", []) if isinstance(answer, dict) else []:
            if not isinstance(value, dict):
                continue
            key = str(value.pop("section", ""))
            index = int(key[1:]) - 1 if key[:1] == "S" and key[1:].isdigit() else -1
            if not 0 <= index < len(group):
                continue
            try:
                meta = validate_meta(ChunkMeta.model_validate(value), group[index].text)
            except Exception:  # noqa: BLE001 - invalid section: its own call retries it
                continue
            if meta.summary and (meta.keywords or meta.search_terms):
                found[group[index].text_sha256] = meta

    await asyncio.gather(*(describe(number, group) for number, group in enumerate(batches, 1)))
    return found, len(batches)


# -- curation and edge filtering ------------------------------------------------------

async def curate_page(*, candidates: list[Any], current: list[dict[str, Any]], mode: str,
                      big_document: bool, settings: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep valid existing choices, then add the remaining verified edges in footer order.

    footer_edges already filtered the candidates; no curator call, no invented anchor.
    """
    from graph.linker.render import render_limits
    from graph.linker.service import _valid_choices

    inline_limit, footer_limit = render_limits(mode, big_document, settings)
    current = _valid_choices(current, candidates, inline_limit=inline_limit, footer_limit=footer_limit)
    current_ids = {choice["edge_id"] for choice in current}
    additions = [
        {"edge_id": edge.edge_id, "placement": "footer", "anchor": "", "summary": edge.summary or edge.peer_summary}
        for edge in candidates
        if edge.edge_id not in current_ids
    ]
    return (
        _valid_choices(current + additions, candidates, inline_limit=inline_limit, footer_limit=footer_limit),
        sorted(edge.edge_id for edge in candidates),
    )


async def filter_target(*, catalog: Any, target: Any, candidates_: Sequence[Any], jev_engine: Any,
                        settings: Any, use_jev: bool) -> tuple[list[dict[str, Any]], int, int]:
    """Same-document entity matches are exact; every other lead needs a Jev verdict.

    No LLM tie-break; with Jev unavailable the leads are omitted.
    """
    accepted = []
    leads = []
    for candidate in candidates_:
        if candidate.source not in {"use", "define"}:
            leads.append(candidate)
            continue
        if catalog.chunk(candidate.chunk_id) is None:
            continue
        via = candidate.via[0] if candidate.via else ""
        accepted.append({
            "chunk_a": target.chunk_id,
            "chunk_b": candidate.chunk_id,
            "label": candidate.label or "related",
            "summary": candidate.summary or (f"「{via}」との関係" if via else ""),
            "source": candidate.source,
            "via": candidate.via,
        })
    if leads and jev_engine is not None and use_jev:
        from graph.linker.jev_judge import judge_edges
        try:
            accepted += await judge_edges(catalog, jev_engine, target, leads, settings, model=None)
        except Exception as exc:
            log.warning("fast Jev edge judge failed for %s: %s", target.chunk_id, exc)
            return accepted, len(leads), 1
        return accepted, len(leads), 0
    return accepted, 0, 0
