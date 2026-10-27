from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChunkEntity(BaseModel):
    name: str = ""
    kind: str = ""
    role: Literal["defines", "uses"] = "uses"
    replaces: list[str] = Field(default_factory=list)


class ChunkBehaviour(BaseModel):
    subject: str = ""
    action: str = ""
    object: str = ""


class ChunkMeta(BaseModel):
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    entity: str = ""
    claims: list[str] = Field(default_factory=list)
    bridge_probe: str = ""
    entities: list[ChunkEntity] = Field(default_factory=list)
    behaviours: list[ChunkBehaviour] = Field(default_factory=list)
    role_judge: str = ""
    # Search-only fields: they ride in the 目次 data block and the linker never reads them.
    kind: str = ""
    points: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)


class EdgeSuggestion(BaseModel):
    target_node_id: str
    label: str = "related"
    summary: str = ""


class EdgeSuggestions(BaseModel):
    edges: list[EdgeSuggestion] = Field(default_factory=list)


NeoLabel = Literal[
    "defines", "uses", "prerequisite", "consequence", "constraint",
    "alternative", "contradicts", "interacts",
]


class NeoEdgeSuggestion(BaseModel):
    target_chunk_id: str
    label: NeoLabel = "interacts"
    summary: str = ""


class NeoEdgeSuggestions(BaseModel):
    edges: list[NeoEdgeSuggestion] = Field(default_factory=list)


class PageReferenceChoice(BaseModel):
    edge_id: str
    placement: Literal["inline", "footer"] = "footer"
    anchor: str = ""
    summary: str = ""


class PageReferencePlan(BaseModel):
    references: list[PageReferenceChoice] = Field(default_factory=list)


__all__ = [
    "ChunkBehaviour", "ChunkEntity", "ChunkMeta", "EdgeSuggestion", "EdgeSuggestions",
    "NeoEdgeSuggestion", "NeoEdgeSuggestions", "NeoLabel", "PageReferenceChoice",
    "PageReferencePlan",
]
