"""H2 chunking and one-call metadata extraction."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from graph.clients.chat import extract_json_from_text
from graph.common.hashing import short_hash
from graph.common.markdown import strip_big_tables, strip_image_media
from graph.wiki.page import fence_flags, strip_reader_references
from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic

from .prompts import CHUNK_META_VERSION, chunk_meta_prompt
from .wire import ChunkBehaviour, ChunkEntity, ChunkMeta

# Bounds for one chunk-metadata call: temperature 0 loops on long lists, and without a
# token cap one call could generate until the request timeout. The cap includes thinking.
META_MAX_TOKENS = 16384  # output only: the search fields (search_terms, claims) have no count cap
META_TEMPERATURE = 0.7


def _is_lead(item: "Chunk", sectioned: set[str]) -> bool:
    """The paragraph above a page's first section: the wiki's own summary of that page."""
    return not item.heading and item.page_rel in sectioned


def _lead_meta(item: "Chunk") -> ChunkMeta:
    # Its text already is a summary, and it never defines a name (see jev_judge.check_roles),
    # so an LLM call would only re-extract names the sections below carry anyway.
    body = " ".join(line for line in item.model_text.splitlines() if line.strip() and not line.startswith("#"))
    return ChunkMeta(summary=body or item.title)


async def _describe(model: Any, messages: list[Any]) -> ChunkMeta:
    """One plain JSON call; the schema is already in the system prompt.

    model.structured first sends a strict-schema request with thinking off, and on real
    chunks that request ran to the token cap every time before its plain-JSON fallback
    answered. Going straight to the plain call halves the calls per chunk.
    """
    error: Exception | None = None
    for _attempt in range(2):
        try:
            raw = await model.text(messages, max_output_tokens=META_MAX_TOKENS, temperature=META_TEMPERATURE)
            return ChunkMeta.model_validate(extract_json_from_text(raw))
        except Exception as exc:  # noqa: BLE001 - one retry, then the caller's fallback
            error = exc
    raise error

def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


@dataclass
class RawChunk:
    ordinal: int
    heading: str
    line_start: int
    line_end: int
    text: str


@dataclass
class Chunk:
    chunk_id: str
    document: str
    team: str
    page_rel: str
    filename: str
    title: str
    ordinal: int
    heading: str
    line_start: int
    line_end: int
    text: str
    text_sha256: str
    meta: ChunkMeta = field(default_factory=ChunkMeta)

    @property
    def summary(self) -> str:
        return self.meta.summary

    @property
    def keywords(self) -> list[str]:
        return self.meta.keywords

    @property
    def entity(self) -> str:
        return self.meta.entity

    @property
    def claims(self) -> list[str]:
        return self.meta.claims

    @property
    def bridge_probe(self) -> str:
        return self.meta.bridge_probe

    @property
    def entities(self) -> list[ChunkEntity]:
        return self.meta.entities

    @property
    def behaviours(self) -> list[ChunkBehaviour]:
        return self.meta.behaviours

    @property
    def model_text(self) -> str:
        return model_text(self)


def split_page(text: str) -> list[RawChunk]:
    lines = text.splitlines()
    flags = fence_flags(lines)
    starts = [i for i, line in enumerate(lines) if not flags[i] and line.startswith("## ")]
    bounds = [0] + starts + [len(lines)]
    chunks: list[RawChunk] = []
    for s, e in zip(bounds, bounds[1:]):
        body = "\n".join(lines[s:e]).strip("\n")
        if not body.strip():
            continue
        heading = lines[s][3:].strip() if s in starts else ""
        chunks.append(RawChunk(len(chunks), heading, s + 1, e, body))
    return chunks


def _without_nav(text: str) -> str:
    marker = text.rfind("\n---\n")
    if marker >= 0 and ("前のページ" in text[marker:] or "次のページ" in text[marker:]):
        text = text[:marker]
    return text


def model_text(chunk: RawChunk | Chunk | str) -> str:
    text = chunk if isinstance(chunk, str) else chunk.text
    return strip_big_tables(strip_image_media(_without_nav(text)))[:12000]


def meta_text(chunk: RawChunk | Chunk | str) -> str:
    """The whole section for chunk_meta: search terms often live in big parameter tables."""
    text = chunk if isinstance(chunk, str) else chunk.text
    return strip_image_media(_without_nav(text))


def chunk_id(id_seed: str, filename: str, ordinal: int) -> str:
    return "lchunk-" + short_hash(f"{id_seed}\0{filename}\0{ordinal}", 20)


def make_chunks(document: str, team: str, filename: str, text: str, *, id_seed: str | None = None) -> list[Chunk]:
    text = strip_reader_references(text)
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ") and line[2:].strip()), Path(filename).stem)
    page_rel = f"{document}/{filename}"
    return [
        Chunk(
            chunk_id=chunk_id(id_seed or document, filename, raw.ordinal), document=document, team=team,
            page_rel=page_rel, filename=filename, title=title, ordinal=raw.ordinal,
            heading=raw.heading, line_start=raw.line_start, line_end=raw.line_end,
            text=raw.text, text_sha256=short_hash(raw.text, 64),
        )
        for raw in split_page(text)
    ]


def validate_meta(meta: ChunkMeta, text: str) -> ChunkMeta:
    collapse = lambda value, limit: " ".join((value or "").split())[:limit]
    keywords: list[str] = []
    seen: set[str] = set()
    for value in meta.keywords:
        value = " ".join((value or "").strip().split())
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); keywords.append(value)
        if len(keywords) == 12:
            break
    claims = _unique(collapse(value, 1000) for value in meta.claims)
    entities: list[ChunkEntity] = []
    seen_names: set[str] = set()
    for item in meta.entities:
        name = (item.name or "").strip()
        if len(name) < 2 or not item.kind.strip() or name not in text:
            continue
        key = normalize_name(name)
        if key in seen_names:
            continue
        seen_names.add(key)
        replaces: list[str] = []
        seen_replacements: set[str] = set()
        for old_name in item.replaces:
            old_name = (old_name or "").strip()
            key = normalize_name(old_name)
            if old_name and key != normalize_name(name) and key not in seen_replacements:
                seen_replacements.add(key)
                replaces.append(old_name)
        entities.append(item.model_copy(update={"name": name, "kind": item.kind.strip(), "replaces": replaces}))
    entity_names = {normalize_name(item.name): item.name for item in entities}
    behaviours: list[ChunkBehaviour] = []
    seen_behaviours: set[tuple[str, str, str]] = set()
    for item in meta.behaviours:
        subject = entity_names.get(normalize_name(item.subject))
        action = " ".join((item.action or "").split())[:60]
        if not subject or not action:
            continue
        obj = entity_names.get(normalize_name(item.object), "")
        key = (normalize_name(subject), normalize_name(action), normalize_name(obj))
        if key in seen_behaviours:
            continue
        seen_behaviours.add(key)
        behaviours.append(ChunkBehaviour(subject=subject, action=action, object=obj))
    return ChunkMeta(
        summary=collapse(meta.summary, 1000), keywords=keywords, entity=collapse(meta.entity, 1000),
        claims=claims, bridge_probe=collapse(meta.bridge_probe, 1000), entities=entities,
        behaviours=behaviours, role_judge=meta.role_judge, kind=collapse(meta.kind, 1000),
        points=_unique(collapse(value, 1000) for value in meta.points),
        search_terms=_unique(collapse(value, 200) for value in meta.search_terms),
    )


def _unique(values: Any) -> list[str]:
    """Non-empty values, first spelling kept, no count cap (the search fields are uncapped)."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); out.append(value)
    return out


def _meta_from_json(value: dict[str, Any]) -> ChunkMeta:
    try:
        return ChunkMeta.model_validate(value)
    except Exception:
        return ChunkMeta()


def cache_by_hash(path: Path, *, meta_version: str = CHUNK_META_VERSION) -> dict[str, ChunkMeta]:
    data = read_json(path, default={})
    if data.get("meta_version") != meta_version:
        return {}
    result: dict[str, ChunkMeta] = {}
    for page in data.get("pages", []):
        for item in page.get("chunks", []):
            if item.get("text_sha256"):
                result[item["text_sha256"]] = _meta_from_json(item)
    return result


def to_json(document: str, team: str, chunks: list[Chunk], *, id_seed: str | None = None, meta_version: str = CHUNK_META_VERSION) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    for item in chunks:
        page = pages.setdefault(item.filename, {"filename": item.filename, "title": item.title, "original_sha256": "", "chunks": []})
        page["chunks"].append({
            "chunk_id": item.chunk_id, "ordinal": item.ordinal, "heading": item.heading,
            "line_start": item.line_start, "line_end": item.line_end, "text_sha256": item.text_sha256,
            **item.meta.model_dump(mode="json"),
        })
    return {"schema_version": 1, "meta_version": meta_version, "document": document, "id_seed": id_seed or document, "team": team, "pages": list(pages.values())}


def snapshot_originals(doc_dir: Path) -> dict[str, str]:
    planning = Path(doc_dir) / "_planning"
    originals = planning / "pages"
    if originals.exists():
        existing = sorted(originals.glob("*.md"))
        if existing:
            return {page.name: short_hash(page.read_text(encoding="utf-8"), 64) for page in existing}
    if originals.exists():
        shutil.rmtree(originals)
    originals.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for page in sorted(Path(doc_dir).glob("*.md")):
        text = page.read_text(encoding="utf-8")
        write_text_atomic(originals / page.name, text)
        result[page.name] = short_hash(text, 64)
    return result


def _apply_entity_replacements(processed: list[Chunk], entities: list[ChunkEntity]) -> set[str]:
    replacements: dict[str, list[ChunkEntity]] = {}
    for entity in entities:
        for old_name in entity.replaces:
            replacements.setdefault(normalize_name(old_name), []).append(entity)
    if not replacements:
        return set()
    changed: set[str] = set()
    for chunk in processed:
        revised: list[ChunkEntity] = []
        touched = False
        for entity in chunk.entities:
            choices = replacements.get(normalize_name(entity.name))
            if not choices:
                revised.append(entity)
                continue
            touched = True
            revised.extend(
                choice.model_copy(update={"role": entity.role, "replaces": []})
                for choice in choices if choice.name in chunk.text
            )
        if touched:
            chunk.meta = validate_meta(chunk.meta.model_copy(update={"entities": revised}), chunk.text)
            changed.add(chunk.chunk_id)
    return changed


async def describe_all(chunks: list[Chunk], *, model: Any, output_language: str, concurrency: int, cache: dict[str, ChunkMeta] | None = None, artifact_dir: Path | None = None, stop_check: Callable[[], bool] | None = None, parallel: bool = False, policy: Any = None) -> tuple[int, int]:
    cache = cache or {}
    calls = fallbacks = 0
    if policy is not None:  # a policy may describe some chunks first (common/policy.py: describe_pages)
        found, calls = await policy.describe_pages(chunks, model=model, output_language=output_language, concurrency=concurrency,
                                                   cache=cache, artifact_dir=artifact_dir, stop_check=stop_check)
        cache = {**cache, **found}  # chunks it did not describe fall through to their own call below
    if parallel:
        semaphore = asyncio.Semaphore(max(1, concurrency))
        pending = []
        sectioned = {item.page_rel for item in chunks if item.heading}
        for item in chunks:
            if item.text_sha256 in cache:
                item.meta = validate_meta(cache[item.text_sha256], item.text)
                continue
            if _is_lead(item, sectioned):
                item.meta = validate_meta(_lead_meta(item), item.text)
                continue
            if stop_check and stop_check():
                raise RuntimeError("linker cancelled")
            prompt = chunk_meta_prompt(page_title=item.title, heading=item.heading, document=item.document,
                                       text=meta_text(item), output_language=output_language, known_entities=[])
            if artifact_dir:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.prompt.md", prompt.render())
            async def describe(target=item, request=prompt):
                async with semaphore:
                    try:
                        result = await _describe(model, request.messages())
                        return validate_meta(result if isinstance(result, ChunkMeta) else ChunkMeta.model_validate(result), target.text), None
                    except Exception as exc:
                        return ChunkMeta(summary=target.model_text[:300]), exc
            pending.append((item, asyncio.create_task(describe())))
        for item, task in pending:
            item.meta, error = await task
            calls += 1
            if error:
                fallbacks += 1
                if artifact_dir:
                    write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}-error.txt", f"{type(error).__name__}: {error}")
            if artifact_dir:
                write_json_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.json", item.meta)
        processed: list[Chunk] = []
        for item in chunks:
            _apply_entity_replacements(processed, item.entities)
            processed.append(item)
        return calls, fallbacks

    registry: dict[str, ChunkEntity] = {}
    processed: list[Chunk] = []

    sectioned = {item.page_rel for item in chunks if item.heading}
    for item in chunks:
        if item.text_sha256 in cache:
            item.meta = validate_meta(cache[item.text_sha256], item.text)
        elif _is_lead(item, sectioned):
            item.meta = validate_meta(_lead_meta(item), item.text)
        else:
            if stop_check and stop_check():
                raise RuntimeError("linker cancelled")
            prompt = chunk_meta_prompt(
                page_title=item.title, heading=item.heading, document=item.document,
                text=meta_text(item), output_language=output_language,
                known_entities=[{"name": entity.name, "kind": entity.kind} for entity in registry.values()],
            )
            if artifact_dir:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.prompt.md", prompt.render())
            calls += 1
            try:
                result = await _describe(model, prompt.messages())
                item.meta = validate_meta(result if isinstance(result, ChunkMeta) else ChunkMeta.model_validate(result), item.text)
            except Exception as exc:
                item.meta = ChunkMeta(summary=item.model_text[:300])
                fallbacks += 1
                if artifact_dir:
                    write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}-error.txt", f"{type(exc).__name__}: {exc}")
            if artifact_dir:
                write_json_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.json", item.meta)
        replaced = {normalize_name(name) for entity in item.entities for name in entity.replaces}
        _apply_entity_replacements(processed, item.entities)
        for name in replaced:
            registry.pop(name, None)
        for entity in item.entities:
            registry[normalize_name(entity.name)] = entity
        processed.append(item)
    return calls, fallbacks


__all__ = ["Chunk", "RawChunk", "cache_by_hash", "chunk_id", "describe_all", "make_chunks", "meta_text", "model_text", "normalize_name", "snapshot_originals", "split_page", "to_json", "validate_meta"]
