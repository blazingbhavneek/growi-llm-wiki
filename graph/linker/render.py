"""Pure rendering and parsing of the managed Markdown links footer."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph.common.markdown import LINKS_FOOTER_END as FOOTER_END, LINKS_FOOTER_START as FOOTER_START
from common.policy import policy_of
from graph.wiki.page import link_entity_mentions, link_titles, strip_reader_references
from graph.wiki.storage import write_text_atomic

FOOTER_TITLE = "## 関連資料"
MAX_FOOTER_ENTRIES = 20
MAX_INLINE_ENTRIES = 8
MAX_BIG_INLINE_ENTRIES = 12
MAX_NEO_BEHAVIOUR_INLINE_ENTRIES = 3
BIG_DOCUMENT_LINES = 150
MAX_SIMILAR_ENTRIES = 5
USEFUL_LABELS = {
    "defines", "defined-by", "uses", "used-by", "requires", "required-by",
    "prerequisite", "prerequisite-for", "implements", "implemented-by",
    "configures", "configured-by", "triggers", "triggered-by", "consequence",
    "consequence-of", "constraint", "constrains", "alternative", "alternative-to",
    "contradicts", "example-of", "has-example",
}
INTERNAL_SUMMARY_TERMS = ("新ノード", "対象ノード", "候補ノード", "チャンク", "lchunk-")
LABEL_PRIORITY = {
    "defines": 0, "defined-by": 0, "implements": 1, "implemented-by": 1,
    "configures": 2, "configured-by": 2, "requires": 3, "required-by": 3,
    "prerequisite": 3, "prerequisite-for": 3, "constraint": 3, "constrains": 3,
    "triggers": 4, "triggered-by": 4, "consequence": 4, "consequence-of": 4,
    "uses": 5, "used-by": 5, "example-of": 6, "has-example": 6,
    "alternative": 7, "alternative-to": 7, "contradicts": 7,
}


@dataclass
class RenderEdge:
    edge_id: str
    peer_page_rel: str
    peer_title: str
    peer_heading: str
    label: str
    summary: str
    forward: bool = True
    source: str = ""
    via: list[str] = field(default_factory=list)
    peer_summary: str = ""


@dataclass(frozen=True)
class FooterLink:
    peer_path: str
    label: str
    summary: str
    reverse: bool = False


def relative_link(from_page_rel: str, to_page_rel: str) -> str:
    return posixpath.relpath(to_page_rel, posixpath.dirname(from_page_rel) or ".")


def ordered(edges: list[RenderEdge], page_rel: str = "") -> list[RenderEdge]:
    document = posixpath.dirname(page_rel)
    return sorted(edges, key=lambda edge: (
        0 if edge.source in {"define", "use"} else 1,
        LABEL_PRIORITY.get(edge.label.lower(), 99),
        0 if page_rel and posixpath.dirname(edge.peer_page_rel) != document else 1,
        edge.peer_title,
        edge.edge_id,
    ))


def footer_edges(edges: list[RenderEdge], *, page_rel: str = "", limit: int | None = MAX_FOOTER_ENTRIES) -> list[RenderEdge]:
    """Edges are per chunk; a page footer shows one line per peer page and reason."""
    seen: set[tuple[str, ...]] = set()
    kept: list[RenderEdge] = []
    similar = 0
    for edge in ordered(edges, page_rel):
        if edge.source == "similar" or (edge.source not in {"use", "define", "jev"} and edge.label.strip().lower() not in USEFUL_LABELS):
            continue
        if any(term in edge.summary for term in INTERNAL_SUMMARY_TERMS):
            continue
        key = (edge.peer_page_rel,)
        if key in seen:
            continue
        if edge.source == "similar":
            if similar >= MAX_SIMILAR_ENTRIES:
                continue
            similar += 1
        seen.add(key)
        kept.append(edge)
    return kept if limit is None else kept[:limit]


def peer_defines(edge: RenderEdge) -> bool:
    """True when, seen from this page, the peer chunk defines the entity in ``via``."""
    return bool(edge.via) and ((edge.source == "use" and edge.forward) or (edge.source == "define" and not edge.forward))


def display(edge: RenderEdge) -> tuple[str, str, str]:
    """(arrow, label, summary) as this page's reader should see the edge."""
    if edge.source in ("use", "define") and edge.via:
        entity = edge.via[0]
        if peer_defines(edge):
            return "", "defines", f"「{entity}」の定義"
        return "", "uses", f"「{entity}」を使用"
    return ("" if edge.forward else "← "), edge.label, edge.summary


def render_limits(mode: str, big_document: bool = False, settings: Any = None) -> tuple[int, int]:
    """(inline, footer) visible-link caps; ``settings`` override the module defaults."""
    inline_field = ("wiki_linker_inline_max_neo" if mode == "neo"
                    else "wiki_linker_inline_max_big" if big_document else "wiki_linker_inline_max")
    inline_default = (MAX_NEO_BEHAVIOUR_INLINE_ENTRIES if mode == "neo"
                      else MAX_BIG_INLINE_ENTRIES if big_document else MAX_INLINE_ENTRIES)
    return (int(getattr(settings, inline_field, inline_default)),
            int(getattr(settings, "wiki_linker_footer_max", MAX_FOOTER_ENTRIES)))


def render_page(
    original: str, *, page_rel: str, edges: list[RenderEdge], mode: str,
    big_document: bool = False, choices: list[dict[str, Any]] | None = None,
    settings: Any = None,
) -> str:
    body = strip_reader_references(original).rstrip("\n") + "\n"
    body, unmask = policy_of(settings).mask_for_linking(body)
    entity_paths: set[str] = set()
    if mode == "neo":
        seen_entities: set[str] = set()
        entity_targets: list[tuple[str, str]] = []
        for edge in ordered(edges, page_rel):
            if not peer_defines(edge):
                continue
            entity = edge.via[0].strip()
            key = entity.casefold()
            if not entity or key in seen_entities:
                continue
            entity_targets.append((entity, relative_link(page_rel, edge.peer_page_rel)))
            seen_entities.add(key)
        for entity, path in sorted(entity_targets, key=lambda target: -len(target[0])):
            linked = link_entity_mentions(body, entity, path)
            if linked != body or f"[{entity}]({path})" in body:
                body = linked
                entity_paths.add(path)
    inline_budget, footer_max = render_limits(mode, big_document, settings)
    curated_edges = edges if mode == "legacy" else [edge for edge in edges if edge.source not in {"use", "define"}]
    by_id = {edge.edge_id: edge for edge in footer_edges(curated_edges, page_rel=page_rel, limit=None)}
    if choices is None:
        selected = [(edge, "footer" if mode == "neo" else "inline", "", edge.summary) for edge in by_id.values()]
    else:
        selected = []
        for choice in choices:
            edge = by_id.get(str(choice.get("edge_id", "")))
            if edge is not None:
                selected.append((edge, str(choice.get("placement", "footer")), str(choice.get("anchor", "")).strip(), str(choice.get("summary", "")).strip() or edge.summary))
    inline_paths: set[str] = set()
    inline_titles: set[str] = set()
    failed_inline: list[tuple[RenderEdge, str]] = []
    for edge, placement, anchor, summary in selected:
        if placement != "inline":
            continue
        if len(inline_paths) >= inline_budget:
            failed_inline.append((edge, summary))
            continue
        title = anchor or (edge.via[0] if mode == "neo" and peer_defines(edge) else edge.peer_title if mode == "legacy" else "")
        path = relative_link(page_rel, edge.peer_page_rel)
        if not title or title in inline_titles:
            failed_inline.append((edge, summary))
            continue
        linked = link_titles(body, [(title, path)])
        if linked != body or f"]({path})" in body:
            body = linked
            inline_paths.add(path)
            inline_titles.add(title)
        else:
            failed_inline.append((edge, summary))
    footer: list[tuple[RenderEdge, str]] = failed_inline
    footer.extend((edge, summary) for edge, placement, _anchor, summary in selected if placement != "inline")
    seen_paths: set[str] = set(inline_paths) | entity_paths
    unique_footer: list[tuple[RenderEdge, str]] = []
    for edge, summary in footer:
        path = relative_link(page_rel, edge.peer_page_rel)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        unique_footer.append((edge, summary))
    footer = unique_footer[:footer_max]
    if not footer:
        return unmask(body)
    lines = [FOOTER_START, FOOTER_TITLE, ""]
    for edge, summary in footer:
        heading = edge.peer_heading if edge.peer_heading and edge.peer_heading != edge.peer_title else ""
        peer = f"{edge.peer_title} › {heading}" if heading else edge.peer_title
        suffix = f" — {summary}" if summary else ""
        lines.append(f"- [{peer}]({relative_link(page_rel, edge.peer_page_rel)}){suffix}")
    lines.append(FOOTER_END)
    return unmask(body + "\n" + "\n".join(lines) + "\n")


_FOOTER_LINE_RE = re.compile(r"^- \[(?P<title>.*?)\]\((?P<path>[^)]+)\) — (?P<reverse>← )?(?P<label>[a-z][a-z0-9_-]*): (?P<summary>.*)$")
_REFERENCE_LINE_RE = re.compile(r"^- \[(?P<title>.*?)\]\((?P<path>[^)]+)\)(?: — (?P<summary>.*))?$")


def parse_footer(text: str) -> list[FooterLink]:
    start = text.find(FOOTER_START)
    end = text.find(FOOTER_END, start + len(FOOTER_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return []
    result: list[FooterLink] = []
    for line in text[start:end].splitlines():
        match = _FOOTER_LINE_RE.match(line.strip())
        if match:
            result.append(FooterLink(match["path"], match["label"].strip(), match["summary"].strip(), bool(match["reverse"])))
            continue
        match = _REFERENCE_LINE_RE.match(line.strip())
        if match:
            result.append(FooterLink(match["path"], "", (match["summary"] or "").strip()))
    return result


def write_if_changed(path: Path, text: str) -> bool:
    path = Path(path)
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return False
    write_text_atomic(path, text)
    return True


__all__ = ["BIG_DOCUMENT_LINES", "FOOTER_END", "FOOTER_START", "FOOTER_TITLE", "FooterLink", "INTERNAL_SUMMARY_TERMS", "LABEL_PRIORITY", "MAX_BIG_INLINE_ENTRIES", "MAX_FOOTER_ENTRIES", "MAX_INLINE_ENTRIES", "MAX_NEO_BEHAVIOUR_INLINE_ENTRIES", "MAX_SIMILAR_ENTRIES", "USEFUL_LABELS", "display", "footer_edges", "peer_defines", "RenderEdge", "ordered", "parse_footer", "relative_link", "render_limits", "render_page", "write_if_changed"]
