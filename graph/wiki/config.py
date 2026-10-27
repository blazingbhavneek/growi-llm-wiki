"""Configuration for overlapping observation, seed planning, and rewriting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from graph.config import app_concurrency

PROMPT_VERSION = "wiki-overlap-plan-ja-6"
SEED_PLAN_VERSION = "wiki-seed-plan-ja-11"
SEED_PLAN_COMPILE_VERSION = "wiki-seed-compile-ja-12"
REWRITE_PROMPT_VERSION = "wiki-sections-ja-5"

class WikiConfig(BaseModel):
    # Policy name (common/policy.py); its hooks key caches apart, so an existing
    # standard run is never resumed as another policy. Missing means standard.
    policy: Literal["standard", "fast"] = "standard"
    skip_excel_and_small: bool = False
    # Overlapping source observation
    window_target_lines: int = 250
    window_overlap_lines: int = 50
    planner_concurrency: int = Field(default_factory=app_concurrency)
    planner_attempts: int = 5
    regional_window_count: int = 10
    # Short in-stage repair budget. A failed document is checkpointed and retried later.
    map_attempts: int = 5
    planner_max_output_tokens: int = 4000
    # Plans are small JSON; the cap only bounds reasoning loops (one ran 10 min on a 15-line file).
    map_max_output_tokens: int = 16000
    # Planner target per page; pages over twice this are split at headings by Python.
    page_target_lines: int = 100

    # Section-wise page writing
    rewrite_concurrency: int = Field(default_factory=app_concurrency)
    section_target_lines: int = 80
    section_min_lines: int = 8
    write_attempts: int = 3
    # Reasoning plus the whole section (a long table section is ~7K tokens). Higher lets a
    # reasoning loop run ~15 min per attempt; lower cuts table rows off.
    write_max_output_tokens: int = 16000
    intro_max_output_tokens: int = 1500
    reference_candidates: int = 6
    reference_attempts: int = 3
    reference_max_output_tokens: int = 8000
    judge_attempts: int = 2
    judge_max_output_tokens: int = 4000

    # Model and output
    chat_base_url: str = "http://10.160.144.101:51029/v1"
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    # Fast policy roles. Empty values intentionally fall back to the normal
    # chat client, so existing standard and fast configurations keep working.
    writer_base_url: str = ""
    writer_api_key: str = ""
    writer_model: str = ""
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_model: str = ""
    temperature: float = 0.7
    # Keep repairs at the same non-zero sampling temperature.
    retry_temperature: float = 0.7
    request_timeout: int = 300
    # Reasoning on plain-text rewrite/intro calls (structured calls keep the
    # server default). Python's lossless checks gate the output either way.
    text_thinking: bool = True
    output_language: str = "Japanese (日本語)"
    # page file name -> what human editors want that page to look like (publisher/human_changes.py)
    human_guidance: dict[str, list[str]] = Field(default_factory=dict)
    prompt_version: str = PROMPT_VERSION
    resume: bool = True
    # Tier-2 updates must resume the stored plan; never silently re-plan.
    require_resume: bool = False
    output_root: str = ".wiki/pages"
    document_slug: str = ""
    # Keep project runs stable so changed sources can resume incrementally.
    run_dir: str = ""
    # Shared content-addressed seed-plan cache. Empty keeps the historical path.
    plan_cache_dir: str = ""

    # Format-aware structural planning.
    source_kind: str = "md"
    structure_target_lines: int = 250
    structure_min_lines: int = 40
    slide_delimiter: str = r"^## Slide (\d+)\s*$"
    slide_title: str = r"^### (.+?)\s*$"
    pdf_use_headings: bool = False
    tabular_slice_records: int = 40
    tabular_preview_rows: int = 12
    tabular_preview_cols: int = 12
