"""Small data contracts used by the seed-page pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .wire import ObservedRange, RegionalPage, SeedRange


class ImageRecord(BaseModel):
    image_id: str
    source_start: int
    source_end: int
    unit_sha256: str
    media_sha256: str = ""
    mime: str = ""
    alt: str = ""
    description: str = ""
    prompt_marker: str = ""


class WindowReport(BaseModel):
    """Persisted result for one overlapping observation window."""

    id: str
    ordinal: int
    source_start: int
    source_end: int
    summary: str = ""
    observations: list[ObservedRange] = Field(default_factory=list)
    mechanical: bool = False
    note: str = ""


class ObservationSet(BaseModel):
    document_id: str
    source_sha256: str
    normalized_source_sha256: str
    source_line_count: int
    windows: list[WindowReport] = Field(default_factory=list)
    images: list[ImageRecord] = Field(default_factory=list)


class RegionalReport(BaseModel):
    ordinal: int
    source_start: int = 0
    source_end: int = 0
    summary: str = ""
    pages: list[RegionalPage] = Field(default_factory=list)


class CompiledSeedPlan(BaseModel):
    summary: str = ""
    pages: list[SeedRange] = Field(default_factory=list)
