"""Legacy RRF candidate discovery and edge filtering."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .catalog import Catalog
from .prompts import EDGE_VERSION_LEGACY, legacy_edge_messages
from .wire import EdgeSuggestions

LEGACY_K = 50
MAX_FUSED_CANDIDATES = 16
BRIDGE_CANDIDATE_CAP = 5
RRF_K = 60
EDGE_GROUP_SIZE = 4
EDGE_MAX_OUTPUT_TOKENS = 2000


@dataclass
class Candidate:
    chunk_id: str
    source: str
    via: list[str] = field(default_factory=list)
    programmatic: bool = False
    label: str = ""
    summary: str = ""


def rrf_fuse(rankings: Iterable[Iterable[str]], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


def candidates(catalog: Catalog, chunk: Any, *, team: str | None = None) -> list[Candidate]:
    team = team or chunk.team
    channels: list[list[str]] = []
    for channel in ("body", "summary", "bridge"):
        vector = catalog.vector(channel, chunk.chunk_id)
        if vector:
            ids = catalog.vec_search(channel, vector, team, LEGACY_K + 1, exclude_page=chunk.page_rel)
            if ids:
                channels.append(ids)
    lexical = " ".join(filter(None, [chunk.title, chunk.model_text[:1500]]))
    ids = catalog.fts_search(lexical, team, LEGACY_K + 1, exclude_page=chunk.page_rel)
    if ids:
        channels.append(ids)
    if chunk.keywords:
        ids = catalog.fts_search(" ".join(chunk.keywords), team, LEGACY_K + 1, exclude_page=chunk.page_rel)
        if ids:
            channels.append(ids)
    ranked = rrf_fuse(channels)[:MAX_FUSED_CANDIDATES]
    bridge: list[str] = []
    if chunk.entity.strip():
        for cid in catalog.chunks_with_entity(chunk.entity, team, exclude_page=chunk.page_rel):
            if cid not in ranked and cid not in bridge:
                bridge.append(cid)
            if len(bridge) >= BRIDGE_CANDIDATE_CAP:
                break
    for neighbour in ranked[:5]:
        if len(bridge) >= BRIDGE_CANDIDATE_CAP:
            break
        for other in catalog.edge_peers(neighbour):
            if other != chunk.chunk_id and other not in ranked and other not in bridge and catalog.page_of(other) != chunk.page_rel:
                bridge.append(other)
                if len(bridge) >= BRIDGE_CANDIDATE_CAP:
                    break
    return [Candidate(cid, "legacy_rrf") for cid in ranked] + [Candidate(cid, "legacy_bridge") for cid in bridge]


def _view(row: Any) -> dict[str, Any]:
    title = row["title"] if "title" in row.keys() else row["page_rel"].rsplit("/", 1)[-1]
    return {"id": row["chunk_id"], "title": f"{title} › {row['heading']}".rstrip(" ›"), "summary": row["summary"], "keywords": json.loads(row["keywords_json"] or "[]"), "header": row["document"], "body": row["body"][:1200]}


async def filter_candidates(catalog: Catalog, model: Any, chunk: Any, selected: list[Candidate], *, mode: str = "legacy", artifact_dir: Any = None, stop_check: Any = None) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for offset in range(0, len(selected), EDGE_GROUP_SIZE):
        if stop_check and stop_check():
            raise RuntimeError("linker cancelled")
        group = selected[offset : offset + EDGE_GROUP_SIZE]
        payload_rows = [catalog.chunk(c.chunk_id) for c in group]
        payload_rows = [row for row in payload_rows if row is not None]
        target = _view(catalog.chunk(chunk.chunk_id))
        messages = legacy_edge_messages(target, [_view(row) for row in payload_rows])
        try:
            try:
                result = await model.structured(EdgeSuggestions, messages, max_output_tokens=EDGE_MAX_OUTPUT_TOKENS)
            except TypeError:
                result = await model.structured(EdgeSuggestions, messages)
            result = result if isinstance(result, EdgeSuggestions) else EdgeSuggestions.model_validate(result)
        except Exception:
            continue
        allowed = {c.chunk_id for c in group}
        for suggestion in result.edges:
            target_id = suggestion.target_node_id.strip()
            if target_id not in allowed or target_id == chunk.chunk_id:
                continue
            accepted.append({"chunk_a": chunk.chunk_id, "chunk_b": target_id, "label": suggestion.label.strip() or "related", "summary": suggestion.summary.strip(), "source": next(c.source for c in group if c.chunk_id == target_id), "via": next(c.via for c in group if c.chunk_id == target_id)})
    return accepted


__all__ = ["BRIDGE_CANDIDATE_CAP", "Candidate", "EDGE_GROUP_SIZE", "LEGACY_K", "MAX_FUSED_CANDIDATES", "RRF_K", "candidates", "filter_candidates", "rrf_fuse"]
