"""Jev yes/no checks used by the optional linker judge."""

from __future__ import annotations

import re
import unicodedata
import logging

from jev import JevQuestion, JevRequest
from .prompts import (JEV_ALIAS_QUESTION, JEV_CURATE_QUESTION,
                      JEV_DOCUMENT_RELATION_QUESTION, JEV_MAIN_DEFINITION_QUESTION,
                      JEV_ROLE_QUESTION, JEV_SCREEN_QUESTION, JEV_VERIFY_QUESTION,
                      edge_tiebreak_messages)

ALIAS_VERSION = "jev-alias-1"
# The LLM gives a yes/no second opinion (reasoning on; the cap includes the reasoning) on
# every link Jev was unsure about and on every cross-document link Jev accepted, since a
# wrong one joins unrelated material. Linking runs nightly, so all of them are asked.
TIEBREAK_MAX_TOKENS = 3000
log = logging.getLogger(__name__)


def _p(result) -> float:
    return float(result.p_yes)


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if c not in "ーｰ" and not c.isspace() and
                   not unicodedata.category(c).startswith(("P", "S")))


def _trigrams(value: str) -> set[str]:
    return {value[i:i + 3] for i in range(max(0, len(value) - 2))}


def _similar(a: str, b: str) -> float:
    aa, bb = _trigrams(a), _trigrams(b)
    return len(aa & bb) / len(aa | bb) if aa and bb else 0.0


async def check_roles(engine, chunks, settings):
    fallback = 0
    sectioned = {item.page_rel for item in chunks if item.heading}
    for item in chunks:
        if item.meta.role_judge == "jev-1":
            continue
        complete = True
        entities = [entity for entity in item.entities if entity.role in {"defines", "uses"}]
        if not item.heading and item.page_rel in sectioned:
            # The lead paragraph above a page's sections only previews them, so it never
            # defines a name; as a definer it would steal use->define links from the section.
            for entity in entities:
                entity.role = "uses"
            item.meta.role_judge = "jev-1"
            continue
        questions = [JevQuestion(JEV_ROLE_QUESTION.format(name=entity.name), key=str(index)) for index, entity in enumerate(entities)]
        try:
            results = await engine.adecide_many({"section": {"page": item.title, "heading": item.heading, "text": item.model_text}}, questions)
            for entity, result in zip(entities, results):
                entity.role = "defines" if _p(result) >= settings.wiki_linker_role_threshold else "uses"
        except Exception as exc:
            fallback += len(entities)
            complete = False
            log.warning("Jev role check failed for chunk %s: %s", item.chunk_id, exc)
        if complete:
            item.meta.role_judge = "jev-1"
    return fallback


async def resolve_aliases(catalog, engine, team, new_names, settings):
    existing = catalog.entity_names(team)
    names = {norm: label for norm, label in existing}
    names.update({str(norm): str(name) for norm, name in new_names})
    fresh = {str(norm) for norm, _ in new_names}
    normalized = {name: _norm(label) for name, label in names.items()}
    pairs = set()
    for name in fresh:
        ranked = []
        for other in names:
            if name == other:
                continue
            score = _similar(normalized[name], normalized[other])
            if normalized[name] == normalized[other] or score >= .5:
                ranked.append((score, other))
        pairs.update(tuple(sorted((name, other))) for _score, other in sorted(ranked, reverse=True)[:5])
    for a, b in sorted(pairs):
        cached = catalog.alias_decision_get(team, a, b, ALIAS_VERSION)
        if cached is None:
            state = {"A": {"name": names[a], "text": _defining_text(catalog, team, a)},
                     "B": {"name": names[b], "text": _defining_text(catalog, team, b)}}
            question = JevQuestion(JEV_ALIAS_QUESTION)
            result = (await engine.adecide_many(state, [question]))[0]
            cached = _p(result)
            catalog.alias_decision_put(team, a, b, ALIAS_VERSION, cached)
    parent = {name: name for name in names}
    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name
    decisions = catalog.conn.execute("SELECT a,b,p FROM alias_decisions WHERE team=? AND version=?", (team, ALIAS_VERSION)).fetchall()
    for decision in decisions:
        a, b = str(decision["a"]), str(decision["b"])
        if a in parent and b in parent and float(decision["p"]) >= settings.wiki_linker_alias_threshold:
            parent[find(b)] = find(a)
    groups = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)
    catalog.set_canonical(team, list(groups.values()))


def _defining_text(catalog, team, name_norm):
    ids = catalog.entity_chunks(team, name_norm, role="defines")
    if not ids:
        ids = catalog.entity_chunks(team, name_norm)
    row = catalog.chunk(ids[0]) if ids else None
    return str(row["body"] if row else "")[:1500]


async def primary_definer(catalog, engine, team, canon, definers, settings):
    if len(definers) == 1:
        return definers[0]
    requests = []
    for cid in definers:
        row = catalog.chunk(cid)
        if row:
            requests.append(JevRequest({"section": str(row["body"])[:6000]},
                                       JevQuestion(JEV_MAIN_DEFINITION_QUESTION.format(name=canon), key=cid)))
    results = await engine.adecide_batch(requests)
    return max(results, key=_p).key if results else ""


def _count_tokens(engine, text):
    """Tokens by the engine's own counter, else a coarse overestimate.

    The fallback counts half a token per UTF-8 byte, which overestimates real
    tokenizers, so parts come out smaller, never larger, than the budget.
    """
    count = getattr(engine, "count_tokens", None)
    if callable(count):
        try: return int(count(text))
        except Exception: pass
    return len(str(text or "").encode("utf-8")) // 2


PART_TOKENS = 1200  # candidate body text per verify question: body plus the block
# overhead (title/summary/entities/prompt, ~530 tokens at most in this corpus)
# and the readout framing stay under the 2048 head budget however dense the prose is.


def _body_parts(body, count):
    """Full body when it fits PART_TOKENS, else paragraph-scale parts.

    Every part is scored as its own question and the candidate keeps the best
    score, so a long body costs extra questions instead of a skipped target.
    """
    text = str(body or "")
    if count(text) <= PART_TOKENS:
        return [text]
    parts, current, used = [], [], 0
    for para in re.split(r"\n[ \t]*\n", text):
        cost = count(para)
        if cost > PART_TOKENS:
            if current:
                parts.append("\n\n".join(current)); current, used = [], 0
            per_char = cost / max(1, len(para))
            cut = max(1, int(PART_TOKENS / per_char))
            while para:
                parts.append(para[:cut]); para = para[cut:]
            continue
        if current and used + cost > PART_TOKENS:
            parts.append("\n\n".join(current)); current, used = [], 0
        current.append(para); used += cost
    if current:
        parts.append("\n\n".join(current))
    return parts or [text]


def _candidate_block(row, *, body=""):
    """The candidate section, as the question text that rides on the shared target state."""
    title = row["page_rel"].rsplit("/", 1)[-1]
    lines = [f"候補の節: {title} › {row['heading']}".rstrip(" ›"), f"要約: {row['summary']}"]
    if str(row["entities_json"] or "[]") != "[]":
        lines.append(f"エンティティ: {row['entities_json']}")
    if body:
        lines.append(f"本文: {body}")
    return "\n".join(lines)


async def judge_edges(catalog, engine, target, candidates, settings, model=None):
    # Sections of the target's own document share its vocabulary and would fill every slot
    # in a large document, so other documents get their own screening slots.
    limit = settings.wiki_linker_screen_candidates
    rows = {c.chunk_id: catalog.chunk(c.chunk_id) for c in candidates}
    found = [c for c in candidates if rows[c.chunk_id]]
    selected = ([c for c in found if rows[c.chunk_id]["document"] == target.document][:limit]
                + [c for c in found if rows[c.chunk_id]["document"] != target.document][:limit])
    # One state per target and per stage, like growi-search's sweep: the target prefix is encoded
    # once and every candidate rides on it as its question, so a chunk costs a couple of decodes
    # instead of one full decode per candidate.
    def target_state(chars):
        return {"target": {"title": target.title, "heading": target.heading,
                           "summary": target.summary, "text": target.model_text[:chars]}}
    screen_state = target_state(1500)
    screen = []
    top = []
    for candidate in selected:
        row = rows[candidate.chunk_id]
        q = JevQuestion(_candidate_block(row) + "\n" + JEV_SCREEN_QUESTION, key=candidate.chunk_id)
        if candidate.source == "define_define":
            top.append((candidate, q, 1.0, row))
        else:
            screen.append((candidate, q))
    screened = await engine.adecide_batch([JevRequest(screen_state, q) for _candidate, q in screen])
    top.extend((candidate, q, _p(result), rows[candidate.chunk_id]) for (candidate, q), result in zip(screen, screened)
               if _p(result) >= settings.wiki_linker_screen_threshold)
    top.sort(key=lambda item: item[2], reverse=True)
    # Sibling sections share a document's vocabulary and outscore other documents at
    # screening, so each side gets its own verify slots; otherwise inter-document links
    # never reach verification.
    keep = settings.wiki_linker_verify_top
    top = ([item for item in top if item[3]["document"] == target.document][:keep]
           + [item for item in top if item[3]["document"] != target.document][:keep])
    verify_state = target_state(6000)
    count = lambda text: _count_tokens(engine, text)
    verify_reqs, verify_owners = [], []
    for candidate, _q, _score, row in top:
        for part in _body_parts(row["body"], count):
            verify_reqs.append(JevRequest(verify_state, JevQuestion(
                _candidate_block(row, body=part) + "\n" + JEV_VERIFY_QUESTION, key=candidate.chunk_id)))
            verify_owners.append(candidate.chunk_id)
    verified = await engine.adecide_batch(verify_reqs)
    best = {}
    for cid, result in zip(verify_owners, verified):
        p = _p(result)
        if cid not in best or p > best[cid]:
            best[cid] = p
    kept, confirm, unsure = [], [], []
    floor = getattr(settings, "wiki_linker_tiebreak_floor", 0)
    for item in top:
        p = best[item[0].chunk_id]
        cross = item[3]["document"] != target.document
        if p >= settings.wiki_linker_verify_threshold:
            (confirm if cross and model is not None else kept).append((item, p))
        elif model is not None and floor and p >= floor:
            unsure.append((item, p))
    for item, p in confirm + unsure:
        if await _llm_says_yes(model, target, item[3]):
            kept.append((item, p))
    return [{"chunk_a": target.chunk_id, "chunk_b": item[0].chunk_id, "label": "related", "summary": "", "source": "jev", "via": [item[0].source, *item[0].via], "p": p}
            for item, p in kept]


async def _llm_says_yes(model, target, row) -> bool:
    messages = edge_tiebreak_messages(
        {"title": target.title, "heading": target.heading, "text": target.model_text},
        {"title": row["page_rel"].rsplit("/", 1)[-1], "heading": row["heading"], "text": row["body"]})
    try:
        reply = await model.text(messages, max_output_tokens=TIEBREAK_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 - no second opinion means Jev's "no" stands
        log.warning("LLM tie-break failed for %s: %s", target.chunk_id, exc)
        return False
    last = next((line.strip() for line in reversed(str(reply).splitlines()) if line.strip()), "")
    return "はい" in last and "いいえ" not in last


async def curate(engine, page_text, edges, settings):
    if not edges:
        return []
    results = await engine.adecide_many(page_text, [JevQuestion(
        JEV_CURATE_QUESTION.format(peer_title=edge.get('peer_title', ''), peer_heading=edge.get('peer_heading', ''),
                                   peer_summary=edge.get('summary', '')), key=edge["edge_id"])
        for edge in edges])
    ranked = [key for _probability, key in sorted(((_p(result), result.key) for result in results if _p(result) >= .5), reverse=True)]
    # Links to other documents get their own slots, so a page's many sibling links in a
    # large document cannot push them out.
    cross = {edge["edge_id"] for edge in edges if edge.get("cross")}
    keep = settings.wiki_linker_curate_keep
    return [key for key in ranked if key not in cross][:keep] + [key for key in ranked if key in cross][:keep]


async def related_documents(engine, doc_cards, settings):
    requests = [JevRequest({"A": a, "B": b}, JevQuestion(JEV_DOCUMENT_RELATION_QUESTION, key=key), key) for key, a, b in doc_cards]
    results = await engine.adecide_batch(requests)
    return {result.key for result in results if _p(result) >= .5}


__all__ = ["check_roles", "curate", "judge_edges", "primary_definer", "related_documents", "resolve_aliases"]
