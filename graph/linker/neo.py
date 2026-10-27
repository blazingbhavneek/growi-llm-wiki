"""Deterministic entity/behaviour candidate discovery for neo mode."""

from __future__ import annotations

import json
from typing import Any

from common.policy import policy_of

from .catalog import Catalog
from .legacy import EDGE_GROUP_SIZE, rrf_fuse
from .prompts import EDGE_VERSION_NEO, neo_edge_messages
from .wire import NeoEdgeSuggestions
from .chunks import normalize_name

NEO_SIMILAR_K = 5
HOP1_MAX, HOP2_MAX, HOP3_MAX = 8, 8, 4


def _view(row: Any) -> dict[str, Any]:
    return {
        "chunk_id": row["chunk_id"], "title": row["page_rel"].rsplit("/", 1)[-1], "heading": row["heading"],
        "summary": row["summary"], "entities": json.loads(row["entities_json"] or "[]"),
        "behaviours": json.loads(row["behaviours_json"] or "[]"), "body": row["body"][:1000],
    }


def _similar_ids(catalog: Catalog, chunk: Any, team: str) -> list[str]:
    channels: list[list[str]] = []
    for channel in ("body", "summary", "bridge"):
        vector = catalog.vector(channel, chunk.chunk_id)
        if vector:
            found = catalog.vec_search(channel, vector, team, 51, exclude_page=chunk.page_rel)
            if found:
                channels.append(found)
    lexical = catalog.fts_search(f"{chunk.title} {chunk.model_text[:1500]}", team, 51, exclude_page=chunk.page_rel)
    if lexical:
        channels.append(lexical)
    return rrf_fuse(channels)[:16]


def _entity_names(catalog: Catalog, chunk_id: str) -> set[str]:
    row = catalog.chunk(chunk_id)
    if not row:
        return set()
    return {normalize_name(item.get("name", "")) for item in json.loads(row["entities_json"] or "[]") if item.get("name")}


def candidates(catalog: Catalog, chunk: Any, *, team: str | None = None, settings: Any = None, judge: bool = False) -> list[Any]:
    from .legacy import Candidate

    team = team or chunk.team
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    def local(ids: list[str]) -> list[str]:
        # A shared name inside one document is a link. Across documents it is only a lead:
        # generic names (table headers, "overview") collide there, so those go to the judge.
        foreign = [cid for cid in ids if (row := catalog.chunk(cid)) is None or row["document"] != chunk.document]
        for cid in foreign:
            if cid not in selected_ids:
                selected.append(Candidate(cid, "name_match", [entity.name])); selected_ids.add(cid)
        return [cid for cid in ids if cid not in foreign]

    for entity in chunk.entities:
        name = normalize_name(entity.name)
        canon = catalog.canonical(team, name)
        if entity.role == "uses":
            definers = local(catalog.entity_chunks(team, canon, role="defines", exclude_page=chunk.page_rel))
            if len(definers) == 1:
                selected.append(Candidate(definers[0], "use", [entity.name], True, "defines", f"「{entity.name}」の定義")); selected_ids.add(definers[0])
            elif len(definers) >= 2:
                for definer in definers:
                    selected.append(Candidate(definer, "use", [entity.name])); selected_ids.add(definer)
        else:
            users = local(catalog.entity_chunks(team, canon, role="uses", exclude_page=chunk.page_rel))
            for user in users:
                selected.append(Candidate(user, "define", [entity.name], True, "uses", f"「{entity.name}」を使用")); selected_ids.add(user)
            if judge:
                for definer in catalog.entity_chunks(team, canon, role="defines", exclude_page=chunk.page_rel):
                    if definer not in selected_ids:
                        selected.append(Candidate(definer, "define_define", [entity.name])); selected_ids.add(definer)
    entity_names = {normalize_name(item.name) for item in chunk.entities}
    caps = [HOP1_MAX, HOP2_MAX, HOP3_MAX]
    caps, judge = policy_of(settings).neo_limits(caps, judge)
    if judge and settings is not None:
        # Fewer values than hops means fewer hops: "40,30" disables the 3-hop expansion.
        try: caps = ([int(value) for value in settings.wiki_linker_hop_caps.split(",")] + [0, 0, 0])[:3]
        except (AttributeError, TypeError, ValueError): pass
    obvious = set() if judge else set(_similar_ids(catalog, chunk, team))
    for cid, _score in catalog.behaviour_chunks(team, entity_names, exclude_page=chunk.page_rel)[:caps[0]]:
        if cid not in obvious and cid not in selected_ids and (not judge or not (_entity_names(catalog, cid) & entity_names)):
            selected.append(Candidate(cid, "hop1", [next(iter(entity_names))] if entity_names else [])); selected_ids.add(cid)

    hop2: list[Candidate] = []
    for e1 in entity_names:
        for behaviour in catalog.behaviour_links(team, e1):
            e2 = normalize_name(behaviour["object_norm"]) if behaviour["subject_norm"] == e1 else normalize_name(behaviour["subject_norm"])
            if not e2 or e2 in entity_names:
                continue
            for row in catalog.behaviour_links(team, e2):
                cid = str(row["chunk_id"])
                if catalog.page_of(cid) != chunk.page_rel and cid not in obvious and cid not in selected_ids and not (_entity_names(catalog, cid) & entity_names):
                    hop2.append(Candidate(cid, "hop2", [e1, e2])); selected_ids.add(cid)
                    if len(hop2) >= caps[1]:
                        break
            if len(hop2) >= caps[1]:
                break
        if len(hop2) >= caps[1]:
            break
    selected.extend(hop2[:caps[1]])
    hop3: list[Candidate] = []
    for e1 in entity_names:
        for first in catalog.behaviour_links(team, e1):
            e2 = normalize_name(first["object_norm"]) if first["subject_norm"] == e1 else normalize_name(first["subject_norm"])
            if not e2 or e2 in entity_names:
                continue
            for second in catalog.behaviour_links(team, e2):
                e3 = normalize_name(second["object_norm"]) if second["subject_norm"] == e2 else normalize_name(second["subject_norm"])
                if not e3 or e3 in entity_names or e3 == e2:
                    continue
                for row in catalog.behaviour_links(team, e3):
                    cid = str(row["chunk_id"])
                    if catalog.page_of(cid) != chunk.page_rel and cid not in obvious and cid not in selected_ids and not (_entity_names(catalog, cid) & entity_names):
                        hop3.append(Candidate(cid, "hop3", [e1, e2, e3])); selected_ids.add(cid)
                        if len(hop3) >= caps[2]:
                            break
                if len(hop3) >= caps[2]:
                    break
            if len(hop3) >= caps[2]:
                break
        if len(hop3) >= caps[2]:
            break
    selected.extend(hop3[:caps[2]])
    if judge:
        text = f"{chunk.title} {chunk.heading} {chunk.summary} {' '.join(chunk.keywords)}"
        limit = getattr(settings, "wiki_linker_screen_candidates", 50)
        # A large document fills a team-wide search with its own sections, so other
        # documents are searched separately and always get candidates of their own.
        found = (catalog.fts_search(text, team, limit, exclude_page=chunk.page_rel)
                 + catalog.fts_search(text, team, limit, exclude_page=chunk.page_rel, exclude_document=chunk.document))
        for cid in found:
            if cid not in selected_ids:
                selected.append(Candidate(cid, "topical")); selected_ids.add(cid)
    return selected


async def filter_candidates(catalog: Catalog, model: Any, chunk: Any, selected: list[Any], *, artifact_dir: Any = None, stop_check: Any = None) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for offset in range(0, len(selected), EDGE_GROUP_SIZE):
        if stop_check and stop_check():
            raise RuntimeError("linker cancelled")
        group = selected[offset : offset + EDGE_GROUP_SIZE]
        pairs = [(item, catalog.chunk(item.chunk_id)) for item in group]
        pairs = [(item, row) for item, row in pairs if row is not None]
        target_row = catalog.chunk(chunk.chunk_id)
        if target_row is None or not pairs:
            continue
        messages = neo_edge_messages(_view(target_row), [{**_view(row), "via": item.via} for item, row in pairs])
        try:
            try:
                result = await model.structured(NeoEdgeSuggestions, messages, max_output_tokens=2000)
            except TypeError:
                result = await model.structured(NeoEdgeSuggestions, messages)
            result = result if isinstance(result, NeoEdgeSuggestions) else NeoEdgeSuggestions.model_validate(result)
        except Exception:
            continue
        allowed = {item.chunk_id for item, _row in pairs}
        used_entities: set[str] = set()
        for suggestion in result.edges:
            if suggestion.target_chunk_id not in allowed or suggestion.target_chunk_id == chunk.chunk_id or not suggestion.summary.strip():
                continue
            item = next(item for item, _row in pairs if item.chunk_id == suggestion.target_chunk_id)
            if item.source == "use" and len(item.via) == 1:
                key = normalize_name(item.via[0])
                if key in used_entities:
                    continue
                used_entities.add(key)
            accepted.append({"chunk_a": chunk.chunk_id, "chunk_b": suggestion.target_chunk_id, "label": suggestion.label, "summary": suggestion.summary.strip(), "source": item.source, "via": item.via})
    return accepted


def inline_targets(page_rel: str, edges: list[Any]) -> list[tuple[str, str]]:
    from .render import peer_defines, relative_link
    return [(edge.via[0], relative_link(page_rel, edge.peer_page_rel)) for edge in edges if peer_defines(edge) and edge.peer_page_rel != page_rel]


__all__ = ["EDGE_VERSION_NEO", "HOP1_MAX", "HOP2_MAX", "HOP3_MAX", "NEO_SIMILAR_K", "candidates", "filter_candidates", "inline_targets"]
