# region Imports

from __future__ import annotations

import configparser
import copy
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

PROJECT_ROOT: Path | None = None


class HumanSyncMode(str, Enum):
    """Rollout policy for remote human changes.

    The default is deliberately fail-closed.  A project must opt in before a
    remote difference becomes authoritative local state.
    """

    off = "off"
    observe = "observe"
    apply = "apply"


@dataclass(frozen=True)
class HumanSyncPolicy:
    mode: HumanSyncMode

    @classmethod
    def resolve(cls, value: "HumanSyncMode | str | None") -> "HumanSyncPolicy":
        raw = value.value if isinstance(value, HumanSyncMode) else str(value or "off").strip().lower()
        try:
            return cls(HumanSyncMode(raw))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in HumanSyncMode)
            raise ValueError(f"human_sync_mode must be one of {allowed}; got {value!r}") from exc

    @property
    def captures(self) -> bool:
        return self.mode is HumanSyncMode.apply

    @property
    def observes(self) -> bool:
        return self.mode in {HumanSyncMode.observe, HumanSyncMode.apply}


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    assert PROJECT_ROOT is not None, "project root is not configured"
    return PROJECT_ROOT / path


def app_concurrency() -> int:
    return max(1, int(os.environ.get("OPENAI_CONCURRENCY", "4")))


def _normalize_growi_token(value: str) -> str:
    return value.strip().removeprefix("API Token:").strip()


def _project_paths(selector: str) -> tuple[str, str, str, str, str, dict[str, str]]:
    value = selector.strip()
    if not value:
        raise ValueError("--project is required (config name from configs/ or absolute INI path)")
    selected = Path(value).expanduser()
    if selected.is_absolute():
        if selected.suffix != ".ini":
            raise ValueError("absolute project config path must end in .ini")
        config_path = selected.resolve()
    else:
        if selected.name != value or selected.suffix not in ("", ".ini"):
            raise ValueError("project must be a name from configs/ or an absolute INI path")
        config_path = resolve_project_path(f"configs/{selected.stem}.ini")
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(config_path, encoding="utf-8"):
        raise FileNotFoundError(f"project config not found: {config_path}")
    target_name = parser.get("project", "target_name").strip()
    if target_name in {"", ".", ".."} or Path(target_name).name != target_name:
        raise ValueError(f"target_name must be one folder name: {target_name!r}")
    data_root = resolve_project_path(parser.get("project", "data_root"))
    mount = Path(parser.get("project", "source_mount")).expanduser()
    if not mount.is_absolute():
        raise ValueError(f"source_mount must be absolute: {mount}")
    overrides = dict(parser.items("project"))
    if parser.has_section("settings"):
        overrides.update(parser.items("settings"))
    return (
        str(data_root.resolve()),
        target_name,
        str(mount.resolve()),
        parser.get("project", "growi_url", fallback="").strip(),
        parser.get("project", "growi_token", fallback="").strip(),
        overrides,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# endregion Imports


# region Models


#  settings
class Settings(BaseModel):

    # For Chat UI
    chat_base_url: str = "http://10.160.144.101:51029/v1"
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    # Optional fast-mode writer endpoint/model. The normal chat settings are
    # the judge defaults; explicit judge variables are additive overrides, and
    # an unset writer follows the judge.
    writer_base_url: str = ""
    writer_api_key: str = ""
    writer_model_name: str = ""
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_model_name: str = ""
    chat_temperature: float = 0.4
    agent_max_steps: int = 40
    agent_patience: int = 20

    # ask() early-exit routing: reuse an existing agent note or answer with
    # shallow RAG when the graph already covers the question; else deep research
    agent_early_exit: bool = True
    early_exit_candidates: int = 20
    shallow_answer_max_nodes: int = 6

    # Compile time/ingest time

    # embeddings
    embed_backend: str = "server"
    embed_base_url: str = "http://10.160.144.101:51024/v1"
    embed_api_key: str = "local"
    embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_device: str = "cpu"  # fallback device id if server fails
    embed_dim: int = 768

    # reranker
    rerank_backend: str = "server"
    rerank_api_key: str = "local"
    hf_rerank_model: str = "cl-nagoya/ruri-v3-reranker-310m"
    rerank_device: str = "cpu"
    # A realtime answer budgets ~5 seconds end to end. A rerank call that has
    # not returned in this long has already lost its stage, and waiting only
    # delays the RRF-ordered fallback the pipeline would use anyway.
    rerank_timeout_seconds: int = 15

    # db
    database_path: str = ".wiki/moove_wiki.sqlite"

    # edge
    edge_candidate_k: int = 50
    vector_query_k: int = 50

    # on endogenenous node change
    cascade_max_hops: int = 3
    cascade_max_nodes: int = 100

    # search parameters
    search_rrf_k: int = 60
    search_candidate_pool: int = 50
    rerank_top_k: int = 20
    entity_dedup: bool = True

    # --- page assembly -----------------------------------------------------
    # Shared default for all bounded parallel work. More specific settings
    # remain available as backwards-compatible overrides.
    concurrency: int = app_concurrency()
    # Both ingest modes remain available; chunks is the historical default.
    ingest_mode: Literal["chunks", "wiki"] = "wiki"
    # CLI sync selects fast by default; --full explicitly selects standard.
    # There is no env knob for the generation policy.
    policy: Literal["standard", "fast"] = "standard"
    # Opt-in sync shortcut: source-only tables and one-page rewrites below 10k chars.
    skip_excel_and_small: bool = False
    # Untracked, content-addressed caches shared by sync's background workers and the
    # serial steps (publisher/ahead.py). Set by sync to the live project; empty = off.
    cache_dir: str = ""
    # wiki mode: lossless section-wise rewrite, see graph/wiki
    wiki_section_target_lines: int = 80
    wiki_write_attempts: int = 3
    wiki_planner_concurrency: int = app_concurrency()
    wiki_rewrite_concurrency: int = app_concurrency()
    # per-request timeout for wiki/linker model calls; slow local models may need more
    wiki_request_timeout: int = 300
    wiki_output_language: str = "Japanese (日本語)"
    # cross-document linker (graph/linker), runs inside the wiki writer
    wiki_linker_enabled: bool = True
    wiki_linker_mode: Literal["legacy", "neo"] = "neo"
    wiki_linker_concurrency: int = 0
    wiki_linker_judge: Literal["llm", "jev"] = "jev"
    # Shisa is the fail-closed default. Project-level overrides remain supported,
    # but production projects inherit the cloud endpoint from .env.
    wiki_jev_backend: Literal["torch", "gguf", "hosted", "llm2jev", "shisa"] = "shisa"
    wiki_jev_base_url: str = ""
    wiki_jev_model: str = ""
    wiki_jev_tokenizer: str = ""
    wiki_jev_http_concurrency: int = Field(default=8, ge=1)
    wiki_jev_gguf_local_path: str = ""
    wiki_jev_gguf_quant: str = "F16"
    wiki_jev_gguf_many_mode: Literal["exact", "batched"] = "exact"
    wiki_jev_score_bin: str = ""
    wiki_linker_role_threshold: float = Field(default=0.5, ge=0, le=1)
    wiki_linker_alias_threshold: float = Field(default=0.8, ge=0, le=1)
    wiki_linker_screen_candidates: int = Field(default=80, ge=1)
    wiki_linker_screen_threshold: float = Field(default=0.4, ge=0, le=1)
    wiki_linker_verify_top: int = Field(default=20, ge=1)
    wiki_linker_verify_threshold: float = Field(default=0.7, ge=0, le=1)
    # Jev verify scores in [floor, verify_threshold) get a yes/no LLM second opinion; 0 turns it off.
    wiki_linker_tiebreak_floor: float = Field(default=0.4, ge=0, le=1)
    wiki_linker_curate_keep: int = Field(default=12, ge=1)
    wiki_linker_hop_caps: str = "60,50,25"
    # Visible-link budgets: what the curator may keep and the renderer may print.
    wiki_linker_footer_max: int = Field(default=20, ge=1)
    wiki_linker_inline_max: int = Field(default=8, ge=0)
    wiki_linker_inline_max_big: int = Field(default=12, ge=0)
    wiki_linker_inline_max_neo: int = Field(default=3, ge=0)
    wiki_linker_edge_candidates: int = Field(default=12, ge=1)
    wiki_linker_edges_per_target: int = Field(default=3, ge=1)
    wiki_index_related_docs: bool = False
    # Kept as an explicit fail-fast compatibility flag for old engine callers.
    engine_semantic_edges: bool = False
    # One project selected from configs/<name>.ini or an absolute INI path.
    data_root: str = ""
    target_name: str = ""
    mount_path: str = ""
    parser_base_url: str = ""
    parser_fallback_base_url: str = ""
    parser_timeout: float = 7200.0
    sync_isolated: bool = True
    # Ask the doc-parser to generate vision descriptions for extracted images.
    # The parser uses its own vision-model configuration; wiki chat settings
    # are never forwarded to it.
    parser_describe_images: bool = True
    growi_url: str = ""
    growi_token: str = ""
    growi_mode: Literal["attach", "own"] = "attach"
    growi_timeout: float = 30.0
    # Human edits are fail-closed until a project explicitly opts in.  The
    # enum makes invalid environment/INI values a configuration error.
    human_sync_mode: HumanSyncMode = HumanSyncMode.off
    human_sync_activity_audit_seconds: int = Field(default=3600, ge=0)
    human_sync_activity_overlap_seconds: int = Field(default=60, ge=0)

    # Legacy read-only config fields. Runtime vector selection is deliberately
    # sqlite-vec-only; keep these for callers that still deserialize Settings.
    vector_backend: Literal["sqlite", "qdrant"] = "sqlite"
    qdrant_url: str = ""
    qdrant_collection: str = "wiki_vectors"
    qdrant_growi_id: str = ""

    # --- engine / sync -----------------------------------------------------
    engine_db: str = ""
    growi_name: str = ""
    sync_interval_seconds: int = 300

    # --- format-aware planning --------------------------------------------
    structure_target_lines: int = 250
    structure_min_lines: int = 40
    slide_delimiter: str = r"^## Slide (\d+)\s*$"
    slide_title: str = r"^### (.+?)\s*$"
    pdf_use_headings: bool = False
    tabular_slice_records: int = 40
    tabular_preview_rows: int = 12
    tabular_preview_cols: int = 12

    # bounded parallelism for the additive benchmark ingest path
    ingest_concurrency: int = app_concurrency()
    # 0 disables automatic reclustering; the caller runs one explicit refresh
    # after a batch has completed.
    recluster_every: int = 10

    #  evidence-first search: chunking (chars)
    search_big_chunk_size: int = 3000
    search_big_chunk_overlap: int = 300
    search_small_chunk_size: int = 512
    search_small_chunk_overlap: int = 80

    #  evidence-first search: retrieval pools -
    pool_node_bm25: int = 50
    pool_vec_body: int = 50
    pool_vec_summary: int = 50
    pool_item_bm25: int = 150
    pool_vec_item: int = 300

    #  evidence-first search: weighted RRF field weights
    weight_item_bm25: float = 1.35
    weight_title_vec: float = 1.30
    weight_claim_vec: float = 1.25
    weight_small_chunk_vec: float = 1.15
    weight_summary_vec: float = 1.00
    weight_big_chunk_vec: float = 0.95
    weight_node_bm25: float = 0.90
    weight_body_vec: float = 0.75
    weight_record_vec: float = 1.20

    #  evidence-first search: caps + rerank/MMR
    evidence_max_per_node: int = 3
    evidence_max_per_field: int = 2
    evidence_dedup_char_window: int = 200
    evidence_rerank_pool: int = 120
    evidence_mmr_lambda: float = 0.75
    subagent_count: int = 3

    # API service concurrency (semaphores in ReadGraphService)
    service_max_reads: int = 16
    service_max_agents: int = app_concurrency()

    # TODO: Reactivate it when serving with docker container and pre installed chrome headless etc
    enable_mermaid: bool = True
    mermaid_repair_attempts: int = 3
    mermaid_cli_bin: str = "mmdc"
    mermaid_puppeteer_config: str = "./puppeteer-config.json"
    mermaid_render_timeout: int = 30

    @classmethod
    def from_env(cls, project: str = "") -> "Settings":
        env = os.environ.get
        (
            data_root,
            project_name,
            mount_path,
            project_growi_url,
            project_growi_token,
            project_overrides,
        ) = _project_paths(project or env("WIKI_PROJECT", ""))
        concurrency = app_concurrency()
        settings = cls(
            chat_base_url=env("OPENAI_BASE_URL", cls.chat_base_url),
            chat_api_key=env("OPENAI_API_KEY", cls.chat_api_key),
            chat_model=env("OPENAI_MODEL_NAME", cls.chat_model),
            writer_base_url=env("WRITER_BASE_URL", cls.writer_base_url),
            writer_api_key=env("WRITER_API_KEY", cls.writer_api_key),
            writer_model_name=env("WRITER_MODEL_NAME", cls.writer_model_name),
            judge_base_url=env("JUDGE_BASE_URL", cls.judge_base_url),
            judge_api_key=env("JUDGE_API_KEY", cls.judge_api_key),
            judge_model_name=env("JUDGE_MODEL_NAME", cls.judge_model_name),
            chat_temperature=float(env("WIKI_TEMPERATURE", cls.chat_temperature)),
            embed_backend=env("WIKI_EMBED_BACKEND", cls.embed_backend),
            embed_base_url=env(
                "WIKI_EMBED_BASE_URL", env("OPENAI_EMBED_BASE_URL", cls.embed_base_url)
            ),
            embed_api_key=env("WIKI_EMBED_API_KEY", cls.embed_api_key),
            embed_model=env("WIKI_EMBED_MODEL", cls.embed_model),
            hf_embed_model=env("WIKI_HF_EMBED_MODEL", cls.hf_embed_model),
            hf_device=env("WIKI_HF_DEVICE", cls.hf_device),
            embed_dim=int(env("WIKI_EMBED_DIM", cls.embed_dim)),
            rerank_backend=env("WIKI_RERANK_BACKEND", cls.rerank_backend),
            rerank_api_key=env("WIKI_RERANK_API_KEY", cls.rerank_api_key),
            hf_rerank_model=env("WIKI_HF_RERANK_MODEL", cls.hf_rerank_model),
            rerank_device=env("WIKI_RERANK_DEVICE", cls.rerank_device),
            rerank_timeout_seconds=int(
                env("WIKI_RERANK_TIMEOUT", cls.rerank_timeout_seconds)
            ),
            database_path=env("WIKI_DB", cls.database_path),
            edge_candidate_k=int(env("WIKI_EDGE_K", cls.edge_candidate_k)),
            vector_query_k=int(env("WIKI_VECTOR_K", cls.vector_query_k)),
            cascade_max_hops=int(env("WIKI_CASCADE_MAX_HOPS", cls.cascade_max_hops)),
            cascade_max_nodes=int(env("WIKI_CASCADE_MAX_NODES", cls.cascade_max_nodes)),
            agent_max_steps=int(env("WIKI_AGENT_MAX_STEPS", cls.agent_max_steps)),
            agent_patience=int(env("WIKI_AGENT_PATIENCE", cls.agent_patience)),
            agent_early_exit=env(
                "WIKI_AGENT_EARLY_EXIT", "1" if cls.agent_early_exit else "0"
            )
            not in {"0", "false", "False", ""},
            early_exit_candidates=int(
                env("WIKI_EARLY_EXIT_CANDIDATES", cls.early_exit_candidates)
            ),
            shallow_answer_max_nodes=int(
                env("WIKI_SHALLOW_MAX_NODES", cls.shallow_answer_max_nodes)
            ),
            search_rrf_k=int(env("WIKI_SEARCH_RRF_K", cls.search_rrf_k)),
            entity_dedup=env("WIKI_ENTITY_DEDUP", "1" if cls.entity_dedup else "0")
            not in {"0", "false", "False", ""},
            concurrency=concurrency,
            ingest_mode=env("WIKI_INGEST_MODE", cls.ingest_mode),
            wiki_section_target_lines=int(
                env("WIKI_SECTION_TARGET_LINES", cls.wiki_section_target_lines)
            ),
            wiki_write_attempts=int(env("WIKI_WRITE_ATTEMPTS", cls.wiki_write_attempts)),
            wiki_planner_concurrency=max(
                1, int(env("WIKI_PLANNER_CONCURRENCY", concurrency))
            ),
            wiki_request_timeout=int(env("WIKI_REQUEST_TIMEOUT", cls.wiki_request_timeout)),
            wiki_rewrite_concurrency=int(
                env("WIKI_REWRITE_CONCURRENCY", concurrency)
            ),
            wiki_output_language=env("WIKI_OUTPUT_LANGUAGE", cls.wiki_output_language),
            wiki_linker_enabled=env(
                "WIKI_LINKER_ENABLED", "1" if cls.wiki_linker_enabled else "0"
            ).lower() in ("1", "true", "yes", "on"),
            wiki_linker_mode=env("WIKI_LINKER_MODE", cls.wiki_linker_mode),
            wiki_linker_judge=env("WIKI_LINKER_JUDGE", cls.wiki_linker_judge),
            wiki_jev_backend=env("WIKI_JEV_BACKEND", cls.wiki_jev_backend),
            wiki_jev_base_url=env("WIKI_JEV_BASE_URL", cls.wiki_jev_base_url),
            wiki_jev_model=env("WIKI_JEV_MODEL", cls.wiki_jev_model),
            wiki_jev_tokenizer=env("WIKI_JEV_TOKENIZER", cls.wiki_jev_tokenizer),
            wiki_jev_http_concurrency=int(env(
                "WIKI_JEV_HTTP_CONCURRENCY", cls.wiki_jev_http_concurrency,
            )),
            wiki_jev_gguf_local_path=env("WIKI_JEV_GGUF_LOCAL_PATH", cls.wiki_jev_gguf_local_path),
            wiki_jev_gguf_quant=env("WIKI_JEV_GGUF_QUANT", cls.wiki_jev_gguf_quant),
            wiki_jev_gguf_many_mode=env("WIKI_JEV_GGUF_MANY_MODE", cls.wiki_jev_gguf_many_mode),
            wiki_jev_score_bin=env("WIKI_JEV_SCORE_BIN", cls.wiki_jev_score_bin),
            wiki_linker_role_threshold=float(env("WIKI_LINKER_ROLE_THRESHOLD", cls.wiki_linker_role_threshold)),
            wiki_linker_alias_threshold=float(env("WIKI_LINKER_ALIAS_THRESHOLD", cls.wiki_linker_alias_threshold)),
            wiki_linker_screen_candidates=int(env("WIKI_LINKER_SCREEN_CANDIDATES", cls.wiki_linker_screen_candidates)),
            wiki_linker_screen_threshold=float(env("WIKI_LINKER_SCREEN_THRESHOLD", cls.wiki_linker_screen_threshold)),
            wiki_linker_verify_top=int(env("WIKI_LINKER_VERIFY_TOP", cls.wiki_linker_verify_top)),
            wiki_linker_verify_threshold=float(env("WIKI_LINKER_VERIFY_THRESHOLD", cls.wiki_linker_verify_threshold)),
            wiki_linker_tiebreak_floor=float(env("WIKI_LINKER_TIEBREAK_FLOOR", cls.wiki_linker_tiebreak_floor)),
            wiki_linker_curate_keep=int(env("WIKI_LINKER_CURATE_KEEP", cls.wiki_linker_curate_keep)),
            wiki_linker_hop_caps=env("WIKI_LINKER_HOP_CAPS", cls.wiki_linker_hop_caps),
            wiki_linker_footer_max=int(env("WIKI_LINKER_FOOTER_MAX", cls.wiki_linker_footer_max)),
            wiki_linker_inline_max=int(env("WIKI_LINKER_INLINE_MAX", cls.wiki_linker_inline_max)),
            wiki_linker_inline_max_big=int(env("WIKI_LINKER_INLINE_MAX_BIG", cls.wiki_linker_inline_max_big)),
            wiki_linker_inline_max_neo=int(env("WIKI_LINKER_INLINE_MAX_NEO", cls.wiki_linker_inline_max_neo)),
            wiki_linker_edge_candidates=int(env("WIKI_LINKER_EDGE_CANDIDATES", cls.wiki_linker_edge_candidates)),
            wiki_linker_edges_per_target=int(env("WIKI_LINKER_EDGES_PER_TARGET", cls.wiki_linker_edges_per_target)),
            wiki_index_related_docs=env("WIKI_INDEX_RELATED_DOCS", "1" if cls.wiki_index_related_docs else "0").lower() in ("1", "true", "yes", "on"),
            wiki_linker_concurrency=int(
                env("WIKI_LINKER_CONCURRENCY", concurrency)
            ),
            engine_semantic_edges=env("WIKI_ENGINE_SEMANTIC_EDGES", "0") == "1",
            data_root=data_root,
            target_name=project_name,
            mount_path=mount_path,
            parser_base_url=env("WIKI_PARSER_BASE_URL", cls.parser_base_url),
            parser_fallback_base_url=env("WIKI_PARSER_FALLBACK_BASE_URL", cls.parser_fallback_base_url),
            parser_timeout=float(env("WIKI_PARSER_TIMEOUT", cls.parser_timeout)),
            sync_isolated=env(
                "WIKI_SYNC_ISOLATED", "1" if cls.sync_isolated else "0"
            ).lower() in {"1", "true", "yes", "on"},
            parser_describe_images=env(
                "WIKI_PARSER_DESCRIBE_IMAGES",
                "1" if cls.parser_describe_images else "0",
            ).lower() in ("1", "true", "yes", "on"),
            growi_url=project_growi_url or env("GROWI_URL", cls.growi_url),
            growi_token=_normalize_growi_token(project_growi_token or env("GROWI_TOKEN", cls.growi_token)),
            growi_mode=env("GROWI_MODE", cls.growi_mode),
            growi_timeout=float(env("GROWI_TIMEOUT", cls.growi_timeout)),
            human_sync_mode=env("WIKI_HUMAN_SYNC_MODE", cls.human_sync_mode.value),
            human_sync_activity_audit_seconds=int(
                env("WIKI_HUMAN_SYNC_AUDIT_SECONDS", cls.human_sync_activity_audit_seconds)
            ),
            human_sync_activity_overlap_seconds=int(
                env("WIKI_HUMAN_SYNC_OVERLAP_SECONDS", cls.human_sync_activity_overlap_seconds)
            ),
            vector_backend=env("WIKI_VECTOR_BACKEND", cls.vector_backend),
            qdrant_url=env("QDRANT_URL", cls.qdrant_url),
            qdrant_collection=env("WIKI_QDRANT_COLLECTION", cls.qdrant_collection),
            qdrant_growi_id=env("WIKI_QDRANT_GROWI_ID", cls.qdrant_growi_id),
            engine_db=env("WIKI_ENGINE_DB", cls.engine_db),
            growi_name=env("WIKI_GROWI_NAME", cls.growi_name),
            sync_interval_seconds=int(env("WIKI_SYNC_INTERVAL_SECONDS", cls.sync_interval_seconds)),
            structure_target_lines=int(env("WIKI_STRUCTURE_TARGET_LINES", cls.structure_target_lines)),
            structure_min_lines=int(env("WIKI_STRUCTURE_MIN_LINES", cls.structure_min_lines)),
            slide_delimiter=env("WIKI_SLIDE_DELIMITER", cls.slide_delimiter),
            slide_title=env("WIKI_SLIDE_TITLE", cls.slide_title),
            pdf_use_headings=env("WIKI_PDF_USE_HEADINGS", "1" if cls.pdf_use_headings else "0")
            not in {"0", "false", "False", ""},
            tabular_slice_records=int(env("WIKI_TABULAR_SLICE_RECORDS", cls.tabular_slice_records)),
            tabular_preview_rows=int(env("WIKI_TABULAR_PREVIEW_ROWS", cls.tabular_preview_rows)),
            tabular_preview_cols=int(env("WIKI_TABULAR_PREVIEW_COLS", cls.tabular_preview_cols)),
            ingest_concurrency=max(
                1, int(env("WIKI_INGEST_CONCURRENCY", concurrency))
            ),
            recluster_every=int(env("WIKI_RECLUSTER_EVERY", cls.recluster_every)),
            search_candidate_pool=int(
                env("WIKI_SEARCH_POOL", cls.search_candidate_pool)
            ),
            rerank_top_k=int(env("WIKI_RERANK_TOP_K", cls.rerank_top_k)),
            search_big_chunk_size=int(
                env("WIKI_SEARCH_BIG_CHUNK_SIZE", cls.search_big_chunk_size)
            ),
            search_big_chunk_overlap=int(
                env("WIKI_SEARCH_BIG_CHUNK_OVERLAP", cls.search_big_chunk_overlap)
            ),
            search_small_chunk_size=int(
                env("WIKI_SEARCH_SMALL_CHUNK_SIZE", cls.search_small_chunk_size)
            ),
            search_small_chunk_overlap=int(
                env("WIKI_SEARCH_SMALL_CHUNK_OVERLAP", cls.search_small_chunk_overlap)
            ),
            pool_node_bm25=int(env("WIKI_POOL_NODE_BM25", cls.pool_node_bm25)),
            pool_vec_body=int(env("WIKI_POOL_VEC_BODY", cls.pool_vec_body)),
            pool_vec_summary=int(env("WIKI_POOL_VEC_SUMMARY", cls.pool_vec_summary)),
            pool_item_bm25=int(env("WIKI_POOL_ITEM_BM25", cls.pool_item_bm25)),
            pool_vec_item=int(env("WIKI_POOL_VEC_ITEM", cls.pool_vec_item)),
            weight_item_bm25=float(env("WIKI_W_ITEM_BM25", cls.weight_item_bm25)),
            weight_title_vec=float(env("WIKI_W_TITLE_VEC", cls.weight_title_vec)),
            weight_claim_vec=float(env("WIKI_W_CLAIM_VEC", cls.weight_claim_vec)),
            weight_small_chunk_vec=float(
                env("WIKI_W_SMALL_CHUNK_VEC", cls.weight_small_chunk_vec)
            ),
            weight_summary_vec=float(env("WIKI_W_SUMMARY_VEC", cls.weight_summary_vec)),
            weight_big_chunk_vec=float(
                env("WIKI_W_BIG_CHUNK_VEC", cls.weight_big_chunk_vec)
            ),
            weight_node_bm25=float(env("WIKI_W_NODE_BM25", cls.weight_node_bm25)),
            weight_body_vec=float(env("WIKI_W_BODY_VEC", cls.weight_body_vec)),
            weight_record_vec=float(env("WIKI_W_RECORD_VEC", cls.weight_record_vec)),
            evidence_max_per_node=int(
                env("WIKI_EVIDENCE_MAX_PER_NODE", cls.evidence_max_per_node)
            ),
            evidence_max_per_field=int(
                env("WIKI_EVIDENCE_MAX_PER_FIELD", cls.evidence_max_per_field)
            ),
            evidence_dedup_char_window=int(
                env("WIKI_EVIDENCE_DEDUP_CHAR_WINDOW", cls.evidence_dedup_char_window)
            ),
            evidence_rerank_pool=int(
                env("WIKI_EVIDENCE_RERANK_POOL", cls.evidence_rerank_pool)
            ),
            evidence_mmr_lambda=float(
                env("WIKI_EVIDENCE_MMR_LAMBDA", cls.evidence_mmr_lambda)
            ),
            subagent_count=int(env("WIKI_SUBAGENT_COUNT", cls.subagent_count)),
            service_max_reads=int(env("WIKI_SERVICE_MAX_READS", cls.service_max_reads)),
            service_max_agents=int(
                env("WIKI_SERVICE_MAX_AGENTS", concurrency)
            ),
            enable_mermaid=env(
                "WIKI_ENABLE_MERMAID", "1" if cls.enable_mermaid else "0"
            )
            not in {"0", "false", "False", ""},
            mermaid_repair_attempts=int(
                env("WIKI_MERMAID_REPAIR_ATTEMPTS", cls.mermaid_repair_attempts)
            ),
            mermaid_cli_bin=env("WIKI_MERMAID_CLI_BIN", cls.mermaid_cli_bin),
            mermaid_puppeteer_config=env(
                "WIKI_MERMAID_PUPPETEER_CONFIG", cls.mermaid_puppeteer_config
            ),
            mermaid_render_timeout=int(
                env("WIKI_MERMAID_RENDER_TIMEOUT", cls.mermaid_render_timeout)
            ),
        )
        reserved = {"source_mount", "mount_path", "data_root", "target_name"}
        unknown = sorted(set(project_overrides) - set(cls.model_fields) - {"source_mount"})
        if unknown:
            raise ValueError(f"unknown project setting(s): {', '.join(unknown)}")
        overrides = {
            key: value
            for key, value in project_overrides.items()
            if key not in reserved and value.strip()
        }
        if "concurrency" in overrides:
            shared = max(1, int(overrides["concurrency"]))
            overrides["concurrency"] = shared
            for field_name in (
                "wiki_rewrite_concurrency",
                "wiki_planner_concurrency",
                "wiki_linker_concurrency",
                "ingest_concurrency",
                "service_max_agents",
            ):
                overrides.setdefault(field_name, shared)
        if "growi_token" in overrides:
            overrides["growi_token"] = _normalize_growi_token(overrides["growi_token"])
        return cls.model_validate({**settings.model_dump(), **overrides})


# from_env() reads Settings.<field> as a plain default value; pydantic v2 stores
# a FieldInfo there instead, so expose the resolved default (handling
# default_factory too, in case a future field uses one) as the class attribute.
for _settings_field, _settings_info in Settings.model_fields.items():
    if _settings_info.default_factory is not None:
        setattr(Settings, _settings_field, _settings_info.default_factory())
    else:
        setattr(Settings, _settings_field, _settings_info.default)


#  whether a node was created from source file or made by AI Agent
class NodeType(str, Enum):
    endogenous = "endogenous"
    exogenous = "exogenous"
    page = "page"
    table = "table"


# whether a node is overruled by newer version, with updated version
class NodeStatus(str, Enum):
    active = "active"
    stale = "stale"
    superseded = "superseded"
    deleted = "deleted"


# Contains the actual information
class Node(BaseModel):
    id: str
    body: str
    type: NodeType = NodeType.endogenous
    title: str = ""
    original_document_name: str | None = None
    source_path: str | None = None
    source_ranges: list[tuple[int, int]] = Field(default_factory=list)
    source_version: str | None = None
    source_material_hash: str | None = None
    entity: str = ""
    claims: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    summary: str = ""
    cluster: str | None = None
    # First folder under mount/raw or after the configured GROWI write path.
    team: str | None = None
    # HyDE-style probe: "what broader concept/field does this connect to",
    # embedded separately (vec_bridge) to surface analogically related nodes
    # that plain body/summary embeddings would never rank as neighbors.
    bridge_probe: str = ""
    status: NodeStatus = NodeStatus.active
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


# Connecting nodes so agent knows what to read next
class Edge(BaseModel):
    id: str
    source_node_id: str
    target_node_id: str
    label: str
    summary: str = ""

    # In case new information comes existing relation is invalidated
    created_at: str = Field(default_factory=now_iso)
    valid_at: str | None = None
    invalid_at: str | None = None
    expired_at: str | None = None
    source_episode_ids: list[str] = Field(default_factory=list)


#  LLM Structured outputs


# singular edge
class EdgeSuggestion(BaseModel):
    target_node_id: str
    label: str = "related"
    summary: str = ""


# list of above edge model
class EdgeSuggestions(BaseModel):
    edges: list[EdgeSuggestion] = Field(default_factory=list)


# HyDE-style bridge probe: one short "what does this connect to" sentence
class BridgeProbe(BaseModel):
    probe: str = ""


# For metadata filtering
class Keywords(BaseModel):
    keywords: list[str] = Field(default_factory=list)


# Factual grounds extracted from a node
class ClaimExtraction(BaseModel):
    entity: str = ""
    claims: list[str] = Field(default_factory=list)


# Whether two entities are same or not
class EntityMatch(BaseModel):
    is_same: bool = False
    target_node_id: str | None = None


# Early-exit routing decision for ask(): reuse an existing agent note verbatim,
# answer shallowly from retrieved evidence, or run the deep research agent.


class RouteMode(str, Enum):
    """
    Strategy selected by the early-exit router.
    """

    reuse = "reuse"
    shallow = "shallow"
    deep = "deep"


class RouteDecision(BaseModel):
    """
    Routing decision for ask() early-exit behavior.

    The router chooses the cheapest strategy that can still answer accurately:
    - reuse: return an existing agent-created answer note verbatim
    - shallow: answer directly from retrieved evidence snippets
    - deep: run the full research agent
    """

    mode: RouteMode = Field(
        default=RouteMode.deep,
        description=(
            "The routing strategy to use. "
            "'reuse' means an existing agent_note already answers the question almost completely, "
            "so return that note verbatim. "
            "'shallow' means the question is narrow and can be answered accurately from the retrieved evidence snippets. "
            "'deep' means the question is broad, multi-topic, uncertain, or the retrieved evidence is insufficient, "
            "so the full research agent should run."
        ),
    )

    node_id: str | None = Field(
        default=None,
        description=(
            "The exact candidate node id to reuse when mode is 'reuse'. "
            "This must be copied exactly from one of the provided candidate node IDs. "
            "Required for mode='reuse'. "
            "Should be null for mode='shallow' or mode='deep'."
        ),
    )

    reason: str = Field(
        default="",
        description=(
            "A short one-sentence explanation for the routing decision. "
            "Explain why the selected mode is sufficient or why deeper research is needed. "
            "Do not leave this empty."
        ),
    )


#  Query response from the graph
class QueryResult(BaseModel):
    query_type: str
    value: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


# Final agent answer
class AgentAnswer(BaseModel):
    question: str
    answer: str = ""
    cited_node_ids: list[str] = Field(default_factory=list)
    exogenous_node_id: str | None = None
    steps: int = 0


class GraphStats(BaseModel):
    total_nodes: int
    active_nodes: int
    endogenous_nodes: int
    exogenous_nodes: int
    total_edges: int
    isolated_nodes: int
    avg_degree: float
    density: float
    mean_neighbor_overlap: float
    clusters: dict[str, int] = Field(default_factory=dict)
    target_node_id: str | None = None


# In case model emits english name for cluster, had this problem earlier
class ClusterRename(BaseModel):
    original_name: str
    new_name: str


class ClusterRenamePlan(BaseModel):
    renames: list[ClusterRename] = []


# Interface describing the methods an LLM client must implement, independent of
# the underlying provider. @runtime_checkable allows isinstance(..., LlmClient) checks.
# Protocol defining the interface expected of an LLM client. Protocols are for
# structural type checking (what methods an object has), not shared implementation
# or inheritance, making them ideal when multiple unrelated classes should be interchangeable.
@runtime_checkable
class LlmClient(Protocol):
    def complete(self, system_prompt: str, user_content: str) -> str: ...
    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any: ...


@runtime_checkable
class EmbedderPort(Protocol):
    dim: int
    model_name: str

    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class RerankerPort(Protocol):
    def top_k(
        self, query: str, items: list[tuple[str, Any]], k: int
    ) -> list[tuple[Any, float]]: ...


# Tools Schemas
class search(BaseModel):
    """Search the wiki for nodes matching a text query."""

    text: str = Field(description="keywords to search for")


class read(BaseModel):
    """Read a node's full body and metadata by id."""

    node_id: str = Field(description="id of the node to read")


class follow_link(BaseModel):
    """Follow edges from a node to its neighboring nodes."""

    node_id: str = Field(description="id of the node to expand")
    direction: str = Field(
        default="both", description="'incoming', 'outgoing', or 'both'"
    )


class explore(BaseModel):
    """Hand distinct starting node ids to a team of exploration subagents."""

    node_ids: list[str] = Field(
        default_factory=list, description="distinct starting node ids"
    )


class finish(BaseModel):
    """Provide the final answer and the node ids used as evidence."""

    answer: str = Field(description="the final answer, grounded in node content")
    cited_node_ids: list[str] = Field(
        default_factory=list, description="ids that support the answer"
    )


LEAD_TOOLS = [search, explore, finish]
SUBAGENT_TOOLS = [search, read, follow_link, finish]


@dataclass
class Subrun:
    """Per-subagent run state. Created once, captured by the dispatch closure —
    never threaded through call signatures."""

    start_id: str
    index: int
    visited: list[str] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    empty_streak: int = 0


@dataclass
class EvidenceHit:
    """One retrieval hit, normalized across every pool (node + search_item)."""

    node_id: str
    field: str
    item_id: str | None
    text: str
    rank: int
    weight: float
    start_char: int | None = None

    def contribution(self, rrf_k: int) -> float:
        return self.weight / (rrf_k + self.rank)


@runtime_checkable
class EnrichmentWorker(Protocol):
    """The subset of GraphWriteSession the queue needs. Kept tiny so the two
    modules stay decoupled and this file never imports graph.graph."""

    def enrich_summary(self, node_id: str) -> None: ...
    def enrich_entity_dedup(self, node_id: str) -> None: ...
    def enrich_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None: ...
    def refresh_clusters(self) -> None: ...
    def refresh_neighborhood(
        self, node_id: str | None = None, document_name: str | None = None
    ) -> int: ...

    # meta accessors (already on GraphWriteSession as _db_get_meta/_db_set_meta;
    # a thin public alias will be added when wiring)
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


@dataclass(frozen=True)
class EnrichJob:
    # "summary" | "entity_dedup" | "cascade" | "maybe_recluster"
    # | "cluster_bridge" | "neighborhood"
    kind: str
    node_id: str | None = None
    replacements: dict[str, str] = field(default_factory=dict)
    stale_sources: list[str] = field(default_factory=list)
    # Neighborhood refresh addresses a whole document: every chunk's stored walk
    # changes when the chain between them is rewritten.
    document_name: str | None = None


# endregion Models


# region Prompts

GRAPH_SYSTEM_PROMPT = "あなたは、簡潔で事実に基づくナレッジグラフWikiを維持します。"

SUMMARY_PROMPT = (
    "このMarkdownノードをナレッジグラフ用に要約してください。本文に存在する事実のみを使用してください。"
    "1〜3文に収めてください。前置きは不要です。"
    "本文が質問への回答、手順、規則、方針、判断基準、変更方法、原因説明、条件、例外、"
    "期限、責任者、数値、対象範囲を含む場合は、それらの具体情報を優先して残してください。"
    "抽象的な言い換えで、文書名、節名、項目名、対象、条件、値、日付、役割、例外を失わないでください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

KEYWORD_PROMPT = (
    "グラフ検索用に、このテキストから重要なキーワードやエンティティを抽出してください："
    "文書名、制度名、規則名、項目名、見出し、対象名、組織名、役割名、担当者、場所、"
    "日付、期限、金額、数値、条件、例外、手順名、状態名、原因、結果、固有名詞、専門用語など。"
    "重複しないキーワードを、重要度の高い順に最大12個返してください。"
    "ユーザーの質問や本文が変更方法・対応方法・判断・原因・手順・条件に関する場合は、"
    "対象、必要な行動、条件、例外、判断基準、変更前後の状態を表す語を優先してください。"
    "頭字語または識別子でない限り、小文字にしてください。"
)

CLAIM_PROMPT = (
    "リビジョン照合のために、このMarkdownノードから安定した識別情報となる事実を抽出してください。"
    "1つの主要なエンティティまたはトピックと、最大20個の原子的な主張を返してください。"
    "主張は、本文によって直接裏付けられる短い事実文でなければなりません。"
    "元文書の順序が入れ替わっても認識可能な事実を優先してください。"
    "本文が規則、手順、方針、判断基準、変更方法、原因説明、条件、例外、期限、責任、"
    "数値、対象範囲を含む場合は、対象、条件、行動、結果、例外、日付、値、役割の関係を"
    "優先して抽出してください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

ROUTER_PROMPT = (
    "あなたはナレッジグラフWikiの質問ルーターです。質問と、検索で見つかった候補ノード"
    "（kind='agent_note' は過去にエージェントが作成した回答ノート、kind='source' は元資料）"
    "が与えられます。最も安価で十分な戦略を1つ選んでください。\n\n"
    "選択肢：\n"
    "- reuse: 候補内の agent_note が、質問と同じ範囲をカバーし、質問が求める具体性まで"
    "ほぼ完全に回答している場合。その候補の node_id を正確に返してください。"
    "回答はそのノート本文をそのまま再利用します。\n"
    "- shallow: 質問が狭く、候補の evidence 抜粋だけで正確かつ十分に回答できる場合。"
    "node_id は null にしてください。\n"
    "- deep: 質問が広い、複数箇所の文書確認が必要、候補同士が矛盾する、"
    "解釈・手順・変更・判断・原因・条件整理が必要、または証拠が不十分な場合。"
    "node_id は null にしてください。\n\n"
    "重要ルール：\n"
    "- 質問の短さだけで shallow を選んではいけません。短い質問でも、"
    "変更方法、対応方針、原因、理由、手順、例外条件、判断基準、複数条件の整理、"
    "文書間の関係確認が必要なら deep を選んでください。\n"
    "- ただし、質問が単一の事実を尋ねており、候補 evidence だけで直接回答できる場合は shallow を選んでください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載箇所にある事実。\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合は、原則 deep を選んでください。\n"
    "- そのような質問で shallow を選んでよいのは、候補 evidence だけで次のすべてが明確に分かる場合です："
    "対象、必要な行動または回答、根拠、条件、例外や制約がある場合はそれら。\n"
    "- agent_note の再利用は、単に関連しているだけでは選んではいけません。"
    "質問が求める範囲と具体性を満たしている場合のみ reuse を選んでください。\n"
    "- kind='agent_note' の最上位候補が質問に直接かつ十分な具体性で回答している場合は reuse を選んでください。"
    "ただし、回答が概念的・抽象的で、対象・条件・手順・根拠が不足している場合は reuse ではなく deep を選んでください。\n"
    "- reuse を選ぶ場合、node_id は候補に含まれる id をそのまま正確にコピーしてください。\n"
    "- reuse では、node_id は必須です。\n"
    "- shallow/deep では、node_id は null にしてください。\n"
    "- 部分的な一致だけなら reuse ではなく shallow または deep を選んでください。\n"
    "- 確信がない場合は deep を選んでください。不正確な近道より深い調査を優先します。\n"
    "- reason には判断理由を短い1文で必ず書いてください。空文字は禁止です。"
)

SHALLOW_ANSWER_PROMPT = (
    "与えられたノード抜粋のみを根拠として、質問に簡潔に回答してください。\n"
    "- 質問と同じ言語で回答してください。\n"
    "- 抜粋に存在しない事実を追加・推論しないでください。\n"
    "- 回答はMarkdown形式で書いてください。\n"
    "- 根拠に使ったノードのidを、本文中でバッククォート付きで引用してください（例：`node:...`）。\n"
    "- 抜粋だけでは回答できない場合は、その旨を明確に述べてください。\n\n"
    "shallow 回答に適したケース：\n"
    "- 質問が単一の事実を尋ねており、抜粋内に直接答えがある場合。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載事実。\n\n"
    "注意が必要なケース：\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合、"
    "抜粋内の一文だけを一般化して答えてはいけません。\n"
    "- その場合、抜粋内に存在する範囲で、対象、必要な行動、条件、根拠、例外、注意点を明確にしてください。\n"
    "- 抜粋に十分な情報がない場合は、"
    "『抜粋だけでは具体的な回答に必要な情報が不足しています』と明確に述べ、"
    "分かっている事実だけを根拠付きで説明してください。\n"
    "- 抽象的な一言だけで終えてはいけません。"
    "回答の書式：\n"
    "- 回答は、単なる長い段落の羅列にせず、内容に応じて見出し、箇条書き、表、短い段落を使って整理してください。\n"
    "- 比較、条件、手順、項目一覧、数値情報がある場合は、可能な限り表または箇条書きで分かりやすく示してください。\n"
    "- 重要点、前提、結論、注意点を明確に分け、読み手が素早く理解できる構成にしてください。\n"
    "- ただし、根拠のない情報を補ったり、見た目のためだけに内容を水増ししてはいけません。\n\n"

    "図：\n"
    "- 回答内に、相互に作用・関連する複数のエンティティ、手順、システム、概念、条件分岐、"
    "原因と結果、依存関係、またはデータの流れが含まれる場合は、理解を助けるため、"
    "できるだけ Mermaid図を作成してください。\n"
    "- Mermaid図は強く推奨されます。単一事実のみの回答、または図にしても情報が増えない場合だけ省略してください。\n"
    "- Mermaid図を含める場合は、必ず「引用:」セクションの直前に ```mermaid のフェンス付きブロックで1つだけ含めてください。\n"
    "- シンプルなASCIIノードID（n1, n2, proc_a）を使用し、スペース、句読点、"
    "長いテキストは引用符付きラベル内に入れてください：n1[\"線形層\"] --> n2[\"GEMM\"]。\n"
    "- mermaid ブロックには Mermaid 構文のみを含めてください。説明文や箇条書きは入れてはいけません。\n"
    "- Mermaid図の中でも、根拠のない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "引用形式：\n"
    "- 最終回答には必ず末尾に「引用:」セクションを含めてください。\n"
    "- 「引用:」セクションは最終回答の最後のセクションにしてください。その後に文章を追加してはいけません。\n"
    "- 引用には、実際に回答の情報源として使用したノードIDのみを記載してください。\n"
    "- 各引用は必ず1件ずつ記載し、引用同士の間には空行を1行入れてください。つまり各引用は改行2つで区切ってください。複数のノードIDを同じ行にまとめてはいけません。\n"
    "- ノードIDをカンマ区切り、読点区切り、スラッシュ区切り、箇条書き内の連続表記にしてはいけません。\n"
    "- 各行は必ず次の形式にしてください：node_id : Title\n"
    "- node_id は表示された形式を完全に保持してください。Title は検索結果またはサブエージェント報告に表示されたタイトルをそのまま使用してください。\n"
    "- タイトルを省略してはいけません。タイトルを推測してはいけません。\n"
    "- cited_node_ids には、「引用:」セクションに記載した node_id と同じIDのみを渡してください。\n"
    "- 引用の形式は必ず次の例に従ってください：\n"
    "\n引用:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

REGENERATE_EXOGENOUS_PROMPT = (
    "その根拠となるソース資料が変更された後に、派生Wikiノードを再生成してください。"
    "以前の派生ノードは、意図されたトピックと構成を理解するためだけに使用してください。"
    "新しいノード本文は、現在のサポート資料のみによって裏付けられていなければなりません。"
    "もはや裏付けられていない古い主張は削除してください。"
    "結果は簡潔で、事実に基づき、Markdown形式にしてください。前置きは不要です。"
)

EDGE_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと、既存ノードの候補の少人数グループ"
    "（複数の検索方法で見つかった候補です。意味的類似性だけとは限りません）が与えられたら、"
    "新しいノードがどの候補にリンクすべきか、またその理由を判断してください。\n"
    "各ノードの 'header' は、そのチャンクが属するより大きなセクション/トピックです。"
    "本文だけでは文脈が薄いチャンクでも、headerを見て判断してください。\n"
    "ルール：\n"
    "- 与えられた候補IDのみを使用してください。\n"
    "- ラベルは、ターゲットが新しいノードにどのように関連するかを説明する短い動詞句です"
    "（例：'uses', 'defines', 'example-of', 'prerequisite-for', 'contradicts'）。\n"
    "- 関係が明確に有用な場合のみエッジを提案してください。弱い関係は省略してください。\n"
    "- 候補が少数（3〜4件）しか渡されない場合でも、無理にすべてを繋げる必要はありません。\n"
    "- summary：リンクを説明する短い節を1つ書いてください。"
)

BRIDGE_PROBE_PROMPT = (
    "あなたはWikiグラフを維持します。次のノート（前後の文脈を含む場合があります）を読み、"
    "これがどの分野・概念・応用につながる可能性があるかを1〜2文で書いてください。\n"
    "ルール：\n"
    "- ノート本文の言い換えはしないでください。本文が示唆する、より広い文脈や"
    "関連しうる領域・分野・応用を書いてください。\n"
    "- 確信が持てない場合は無理に広げず、素直に本文の主題を短く書いてください。"
)

CLUSTER_BRIDGE_PROMPT = (
    "あなたはWikiグラフを維持します。異なるクラスタ（トピック）を代表する2つのノードが"
    "与えられます。これらは意味的類似性やキーワードでは直接見つからない組み合わせです。\n"
    "両者の間に、共有される仕組み・原因・応用・類推など、非自明だが本当に有用なつながりが"
    "あるかどうかを判断してください。\n"
    "ルール：\n"
    "- 与えられた候補IDのみを使用してください。\n"
    "- 関連がはっきりせず、こじつけに感じる場合は無理につなげないでください。"
    "その場合は空のリストを返してください。\n"
    "- ラベルは、ターゲットが新しいノードにどのように関連するかを説明する短い動詞句です"
    "（例：'analogous-to', 'shares-mechanism-with', 'application-of', 'inspired-by'）。\n"
    "- summary：どのようにつながっているかを短く具体的に説明してください。"
)

ENTITY_DEDUP_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと既存ノードの候補リストが与えられたら、"
    "新しいノードが、候補のうち厳密に1つと同じ現実世界のエンティティまたはトピックを"
    "記述しているかどうかを判断してください。\n"
    "ルール：\n"
    "- 同じエンティティとは、同じ具体的なもの"
    "（同じAPI、同じツール、同じ概念）を意味し、単に関連している、または類似している"
    "トピックではありません。\n"
    "- 保守的に判断してください。不確かな場合は is_same=false と答えてください。"
    "異なるものを指す同音異義語を決して統合しないでください。\n"
    "- 一致するものがある場合は、is_same=true とその候補の target_node_id を返してください。"
    "それ以外の場合は is_same=false と target_node_id=null を返してください。"
)

CLUSTER_NAMER_SYSTEM = (
    "あなたはナレッジグラフ内の1つのトピッククラスタに名前を付けます。"
    "必ず日本語でトピック名を返してください。"
    "英語のクラスタ名は使用しないでください。ただし、API名、関数名、ライブラリ名、"
    "製品名、頭字語、識別子など、翻訳すべきでない技術用語はそのまま残してかまいません。"
    "すでに使用されている他の名前と区別できる、具体的な名前を選んでください。"
    "キーワードとサンプルのセクションタイトルに見られる、最も具体的な技術的サブトピックを"
    "優先してください。"
    "より狭いトピックが存在する場合は、CUDA、SYCL、OpenMP、oneAPI のような"
    "広範なソース名だけを名前にすることは避けてください。"
    "最大4語程度の簡潔な日本語トピック名を1つだけ返してください。"
    "引用符、句読点、説明は不要です。"
)

MAIN_AGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiからの質問に答える主任研究者です。"
    "あなたは調整役であり、自分でノードを読むことはありません。\n\n"
    "あなたには2つの能力があります：\n"
    "- search(text)：候補ノードを検索します"
    "（関連性によってすでに再ランキング済み）。ノードID、タイトル、要約のみを返します。\n"
    "- explore(node_ids)：重複しない開始ノードIDのリストをサブエージェントチームに渡します。"
    "各サブエージェントは1つの開始ノードからグラフを読み進め、調査結果を報告します。"
    "有望な手がかりを調査するために使用してください。\n"
    "- finish(answer, cited_node_ids)：最終的にまとめた回答を提出します。\n\n"

    "作業手順：\n"
    "1. まず主質問を検索し、その後、質問内の重要語、固有名詞、文書名、制度名、対象名、"
    "条件、役割、日付、数値、手順名、判断基準らしき語を検索してください。"
    "グラフの異なる部分を浮かび上がらせるために、いくつか検索を行ってください。\n"
    "2. すべての候補から、異なるサブトピックをカバーする最良の重複しない開始ノードを選んでください"
    "（近い重複を避け、サブエージェントが異なるサブグラフを探索できるようにしてください）。\n"
    "3. それらの開始ノードを指定して explore(node_ids) を1回呼び出してください。"
    "チームは並列に探索し、各サブエージェントの調査結果と読んだノード本文を返します。\n"
    "4. サブエージェントの報告を読んでください。明確な不足が残っている場合は、"
    "再度検索して explore をもう1回実行してもかまいません。そうでなければ回答をまとめてください。\n"
    "5. サブエージェントが報告した内容のみに基づいた十分な回答を finish で提出してください。"
    "回答本文の途中にノードIDや引用を入れてはいけません。引用は必ず末尾の「引用:」セクションだけにまとめてください。\n\n"

    "深く調べるべき質問の方針：\n"
    "- ユーザーが行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較、"
    "複数文書の整合性を尋ねている場合、概念的な答えだけで finish してはいけません。\n"
    "- その場合は、可能な限り次を特定してください："
    "対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、判断基準、"
    "変更前後の状態、例外、注意点、根拠。\n"
    "- 質問が単一の事実だけを尋ねている場合は、必要以上に広げず、直接回答してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- ただし、単一の事実に見えても、候補同士が矛盾する場合、条件付きの場合、"
    "文脈により答えが変わる場合は、追加検索や追加 explore を検討してください。\n"
    "- サブエージェントの報告に具体的な対象、条件、根拠が不足している場合は、"
    "追加検索や追加 explore を検討してください。\n"
    "- それでも具体的な情報が見つからない場合は、見つかった事実と不足している情報を明確に分けて回答してください。"
    "根拠がない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "回答の書式：\n"
    "- 回答は、単なる長い段落の羅列にせず、内容に応じて見出し、箇条書き、表、短い段落を使って整理してください。\n"
    "- 比較、条件、手順、項目一覧、数値情報がある場合は、可能な限り表または箇条書きで分かりやすく示してください。\n"
    "- 重要点、前提、結論、注意点を明確に分け、読み手が素早く理解できる構成にしてください。\n"
    "- ただし、根拠のない情報を補ったり、見た目のためだけに内容を水増ししてはいけません。\n\n"

    "ルール：\n"
    "- あなたはノード本文を直接読むことはできません。サブエージェントの報告に依存してください。\n"
    "- 少なくとも1回 explore を実行する前に finish してはいけません。\n"
    "- 幅広さを優先してください。同じ内容に関する複数のIDではなく、"
    "異なる開始点を explore に渡してください。\n"
    "- 回答はユーザーの質問に直接答えてください。関連情報の要約だけで終えてはいけません。\n"
    "- 回答本文の途中には、ノードID、引用、参照、括弧付き出典を一切書いてはいけません。\n"
    "- ノードIDは表示された形式をそのまま保持してください。"
    "省略、変換、正規化、並べ替え、独自フォーマットへの変更をしてはいけません。"
    "`node:` プレフィックス、記号、大小文字、区切り文字、空白なども元の表示どおりにコピーしてください。\n\n"

    "図：\n"
    "- 回答内に、相互に作用・関連する複数のエンティティ、手順、システム、概念、条件分岐、"
    "原因と結果、依存関係、またはデータの流れが含まれる場合は、理解を助けるため、"
    "できるだけ Mermaid図を作成してください。\n"
    "- Mermaid図は強く推奨されます。単一事実のみの回答、または図にしても情報が増えない場合だけ省略してください。\n"
    "- Mermaid図を含める場合は、必ず「引用:」セクションの直前に ```mermaid のフェンス付きブロックで1つだけ含めてください。\n"
    "- シンプルなASCIIノードID（n1, n2, proc_a）を使用し、スペース、句読点、"
    "長いテキストは引用符付きラベル内に入れてください：n1[\"線形層\"] --> n2[\"GEMM\"]。\n"
    "- mermaid ブロックには Mermaid 構文のみを含めてください。説明文や箇条書きは入れてはいけません。\n"
    "- Mermaid図の中でも、根拠のない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "引用形式：\n"
    "- 最終回答には必ず末尾に「引用:」セクションを含めてください。\n"
    "- 「引用:」セクションは最終回答の最後のセクションにしてください。その後に文章を追加してはいけません。\n"
    "- 引用には、実際に回答の情報源として使用したノードIDのみを記載してください。\n"
    "- 各引用は必ず1件ずつ記載し、引用同士の間には空行を1行入れてください。つまり各引用は改行2つで区切ってください。複数のノードIDを同じ行にまとめてはいけません。\n"
    "- ノードIDをカンマ区切り、読点区切り、スラッシュ区切り、箇条書き内の連続表記にしてはいけません。\n"
    "- 各行は必ず次の形式にしてください：node_id : Title\n"
    "- node_id は表示された形式を完全に保持してください。Title は検索結果またはサブエージェント報告に表示されたタイトルをそのまま使用してください。\n"
    "- タイトルを省略してはいけません。タイトルを推測してはいけません。\n"
    "- cited_node_ids には、「引用:」セクションに記載した node_id と同じIDのみを渡してください。\n"
    "- 引用の形式は必ず次の例に従ってください：\n"
    "\n引用:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

SUBAGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiの1つの領域を探索する研究サブエージェントです。"
    "主任研究者から開始ノードを与えられています。それを徹底的に調査し、"
    "具体的で根拠のある発見を報告してください。\n\n"

    "ツール：\n"
    "- read(node_id)：ノードの完全な本文を読みます。割り当てられたノードを読むことから開始してください。\n"
    "- follow_link(node_id, direction)：参照、例、前提条件、関連概念をたどるために、"
    "ノードの近傍へ移動します。\n"
    "- search(text)：自分の領域で必要な場合、キーワードで追加のノードを検索します。\n"
    "- finish(answer, cited_node_ids)：調査結果と読んだノードIDを報告します。\n\n"

    "ルール：\n"
    "1. 割り当てられた開始ノードを最初に読んでください。\n"
    "2. リンクをたどり、自分の領域内で2〜5個のノードを読んで、実際の証拠を集めてください。"
    "ただし、質問に答えるための対象・条件・根拠・例外・手順が不足している場合は、"
    "必要に応じて追加検索やリンク追跡を行ってください。\n"
    "3. 自分の担当範囲に留まってください。タスクに列挙された兄弟開始ノードは"
    "他のサブエージェントが担当します。それらを再探索せず、自分のサブグラフに集中して、"
    "チーム全体でより広い範囲をカバーできるようにしてください。\n"
    "4. 実際に読んだノード本文のみに基づいて報告してください。\n"
    "5. ノードIDは表示された形式をそのまま保持してください。"
    "省略、変換、正規化、並べ替え、独自フォーマットへの変更をしてはいけません。"
    "`node:` プレフィックス、記号、大小文字、区切り文字、空白なども元の表示どおりにコピーしてください。\n"
    "6. 読んだ各ノードについて、ノードIDだけでなくタイトルも記録してください。"
    "タイトルはツール結果、検索結果、またはノード本文に表示されたものをそのまま使い、推測してはいけません。\n"
    "7. 完了したら、この領域が質問について何を示しているかを焦点を絞って要約し、"
    "使用したノードIDを cited_node_ids に入れて finish を呼び出してください。\n\n"

    "調査時の観点：\n"
    "- 質問が単一の事実を尋ねている場合は、その事実と根拠を正確に報告してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- 質問が行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較を尋ねている場合は、"
    "抽象的な概念だけで報告してはいけません。\n"
    "- その場合は、対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、"
    "判断基準、変更前後の状態、例外、注意点、根拠を探してください。\n"
    "- 読んだ本文にそれらが存在する場合は、具体名、条件、値、記述をそのまま報告してください。\n"
    "- 存在しない場合は、『この領域では具体的な情報は見つからなかった』と明確に報告し、"
    "見つかった関連事実だけを根拠付きで説明してください。\n\n"

    "図：\n"
    "- 回答内に、相互に作用・関連する複数のエンティティ、手順、システム、概念、条件分岐、"
    "原因と結果、依存関係、またはデータの流れが含まれる場合は、理解を助けるため、"
    "できるだけ Mermaid図を作成してください。\n"
    "- Mermaid図は強く推奨されます。単一事実のみの回答、または図にしても情報が増えない場合だけ省略してください。\n"
    "- Mermaid図を含める場合は、必ず「引用:」セクションの直前に ```mermaid のフェンス付きブロックで1つだけ含めてください。\n"
    "- シンプルなASCIIノードID（n1, n2, proc_a）を使用し、スペース、句読点、"
    "長いテキストは引用符付きラベル内に入れてください：n1[\"線形層\"] --> n2[\"GEMM\"]。\n"
    "- mermaid ブロックには Mermaid 構文のみを含めてください。説明文や箇条書きは入れてはいけません。\n"
    "- Mermaid図の中でも、根拠のない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "引用形式：\n"
    "- 最終回答には必ず末尾に「引用:」セクションを含めてください。\n"
    "- 「引用:」セクションは最終回答の最後のセクションにしてください。その後に文章を追加してはいけません。\n"
    "- 引用には、実際に情報を取得したノードIDのみを記載してください。\n"
    "- 回答本文の途中には、ノードID、引用、参照、括弧付き出典を一切書いてはいけません。\n"
    "- 各引用は必ず1件ずつ記載し、引用同士の間には空行を1行入れてください。つまり各引用は改行2つで区切ってください。複数のノードIDを同じ行にまとめてはいけません。\n"
    "- ノードIDをカンマ区切り、読点区切り、スラッシュ区切り、箇条書き内の連続表記にしてはいけません。\n"
    "- 各行は必ず次の形式にしてください：node_id : Title\n"
    "- node_id は表示された形式を完全に保持してください。Title はツール結果、検索結果、またはノード本文に表示されたタイトルをそのまま使用してください。\n"
    "- タイトルを省略してはいけません。タイトルを推測してはいけません。\n"
    "- cited_node_ids には、「引用:」セクションに記載した node_id と同じIDのみを渡してください。\n"
    "- 引用の形式は必ず次の例に従ってください：\n"
    "\n引用:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

MERMAID_FIX_SYSTEM = (
    "Mermaid図の構文を修正し、mermaid-cli（mmdc）で描画できるようにしてください。"
    "修正済みの ```mermaid フェンス付きコードブロックを1つだけ返し、それ以外は何も返さないでください。"
)


# endregion Prompts


# region Helpers/Utils


# Single shared tokenizer. A module constant on purpose: compiling a regex is
# relatively expensive, so reuse one compiled instance across the module.

# regex to find mermaid blocks
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```", re.IGNORECASE | re.DOTALL
)

# Matches tokens like "foo/bar:v1.2", "abc_123", or "2025-01-31".
TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

# Matches separators/punctuation like " ", "_", ".", or "!!!" for slugification.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# identifiers / hashing
def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# Identity of a whole source document, for recon dedup
def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# remove all special symbols and whitespace and repalce with hyphen
def slug(text: str, max_length: int = 40) -> str:
    value = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return value[:max_length] or "node"


def make_node_id(body: str, document_name: str | None = None) -> str:
    return f"node:{slug(document_name or 'node', 24)}:{short_hash(body)}"


def make_exogenous_node_id(seed: str) -> str:
    return f"exo:{short_hash(seed)}"


def make_edge_id(source_id: str, target_id: str, label: str) -> str:
    return f"edge:{short_hash(f'{source_id}|{target_id}|{label}', 16)}"


# chunking
def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Split ``text`` into overlapping character windows.

    Returns ``(start_char, end_char, chunk)`` triples. ``overlap`` is clamped to
    ``size - 1`` so the window always advances. Blank windows are skipped."""
    if not text:
        return []
    size = max(1, size)
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    chunks: list[tuple[int, int, str]] = []
    length = len(text)
    start = 0
    while start < length:
        end = min(start + size, length)
        piece = text[start:end]
        if piece.strip():
            chunks.append((start, end, piece))
        if end >= length:
            break
        start += step
    return chunks


# text matching / scoring
def normalize_token(token: str) -> str:
    return token.strip().lower()


def normalize_text(text: str) -> str:
    return " ".join(normalize_token(token) for token in TOKEN_RE.findall(text.lower()))


def jaccard(left: set[str], right: set[str]) -> float:
    left = {value for value in left if value}
    right = {value for value in right if value}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def token_jaccard(left: str, right: str) -> float:
    return jaccard(
        {normalize_token(t) for t in TOKEN_RE.findall(left.lower())},
        {normalize_token(t) for t in TOKEN_RE.findall(right.lower())},
    )


def claim_keys(node: Node) -> set[str]:
    keys: set[str] = set()
    for claim in node.claims:
        normalized = normalize_text(claim)
        if normalized:
            keys.add(normalized)
    return keys


def match_score(old: Node, new: Node) -> float:
    """Revision-match score in [0, 1]: how likely `new` is a revision of `old`."""
    claim_score = jaccard(claim_keys(old), claim_keys(new))
    keyword_score = jaccard(
        {normalize_token(k) for k in old.keywords},
        {normalize_token(k) for k in new.keywords},
    )
    body_score = token_jaccard(old.body, new.body)
    entity_bonus = 0.0
    if old.entity and new.entity:
        entity_bonus = (
            0.2 if normalize_text(old.entity) == normalize_text(new.entity) else 0.0
        )
    return min(
        1.0, max(claim_score, keyword_score * 0.8, body_score * 0.65) + entity_bonus
    )


def claims_equivalent(old: Node, new: Node, unchanged_threshold: float = 0.9) -> bool:
    """True when old/new carry the same facts (reorder, not a real change)."""
    old_claims = claim_keys(old)
    new_claims = claim_keys(new)
    if old_claims and new_claims:
        return jaccard(old_claims, new_claims) >= unchanged_threshold
    return token_jaccard(old.body, new.body) >= 0.95


# pure formatters
def node_ref(node: Node) -> dict[str, str]:
    return {"id": node.id, "title": node.title or node.entity or node.id}


def dedupe(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.append(node_id)
    return seen


def clean_node_ref(value: str) -> str:
    """Strip the decoration an LLM tends to add around a node id (bullets,
    backticks, 'id:' prefix, a copied table row, a trailing '(Title)')."""
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    text = text.strip("`'\" \t\r\n")
    text = re.sub(r"^id\s*:\s*", "", text, flags=re.IGNORECASE).strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"\s+\([^)]*\)\s*$", "", text).strip()
    return text.strip("`'\" \t\r\n,;")


def format_node_full(node: Node | None, requested_id: str, cleaned_id: str) -> str:
    if not node:
        return f"node not found\nrequested_id: {requested_id}\ncleaned_id: {cleaned_id}"
    note = ""
    if requested_id.strip() != cleaned_id:
        note = f"requested_id: {requested_id}\ncleaned_id: {cleaned_id}\n"
    return f"{note}id: {node.id}\ntitle: {node.title}\nsummary: {node.summary}\nbody:\n{node.body}"


# mermaid validate / repair (a real subsystem; not inlined)
def validate_mermaid(code: str, settings: Settings) -> tuple[bool, str]:
    """Render the code to SVG with mmdc; True if it parses + renders."""
    mmdc = shutil.which(settings.mermaid_cli_bin)
    if mmdc is None:
        return False, f"mermaid CLI '{settings.mermaid_cli_bin}' not found in PATH"
    config = Path(settings.mermaid_puppeteer_config).expanduser()
    cmd_prefix = [mmdc] + (["-p", str(config)] if config.exists() else [])
    with tempfile.TemporaryDirectory() as tmp:
        in_file = Path(tmp) / "d.mmd"
        out_file = Path(tmp) / "d.svg"
        in_file.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [*cmd_prefix, "-i", str(in_file), "-o", str(out_file)],
                capture_output=True,
                timeout=settings.mermaid_render_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {settings.mermaid_render_timeout}s"
        except Exception as exc:  # noqa: BLE001 - mmdc failed to start
            return False, f"mmdc failed to start: {exc}"
        if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
            return True, ""
        return False, proc.stderr.decode("utf-8", errors="replace").strip()


def repair_answer_mermaid(
    answer: str, llm: LlmClient, settings: Settings, emit: Callable[[dict], None]
) -> str:
    """Validate + repair every mermaid block in `answer`, emitting progress events."""
    blocks = list(_MERMAID_BLOCK_RE.finditer(answer))
    if not blocks:
        return answer
    if shutil.which(settings.mermaid_cli_bin) is None:
        emit({"type": "diagram_skipped", "reason": "mermaid CLI not installed"})
        return answer
    emit({"type": "diagram_pending"})

    new_answer = answer
    fixed_codes: list[str] = []
    all_ok = True
    for match in blocks:
        code = match.group("code").strip()
        ok, error = validate_mermaid(code, settings)
        attempt = 0
        while not ok and attempt < settings.mermaid_repair_attempts:
            attempt += 1
            user = (
                "The following Mermaid diagram does not render. Fix the syntax, preserving "
                "all nodes, edges, directions, and labels. Use simple ASCII node IDs and "
                "quoted labels for any spaces/punctuation/long text.\n\n"
                f"Render error:\n{error}\n\n```mermaid\n{code}\n```"
            )
            try:
                response = llm.complete(MERMAID_FIX_SYSTEM, user)
            except Exception:  # noqa: BLE001 - repair is best-effort
                break
            fix = _MERMAID_BLOCK_RE.search(response)
            repaired = (
                fix.group("code").strip()
                if fix
                else response.strip().strip("`").strip()
            )
            if not repaired:
                break
            code = repaired
            ok, error = validate_mermaid(code, settings)
        all_ok = all_ok and ok
        fixed_codes.append(code)
        new_answer = new_answer.replace(match.group(0), f"```mermaid\n{code}\n```", 1)

    emit(
        {
            "type": "diagram_ready" if all_ok else "diagram_failed",
            "answer": new_answer,
            **({"mermaid": fixed_codes} if all_ok else {}),
        }
    )
    return new_answer


# TODO: Make this inline
def item_vec_weight(settings: Settings, field: str) -> float:
    return {
        "title": settings.weight_title_vec,
        "claim": settings.weight_claim_vec,
        "record": settings.weight_record_vec,
        "small_chunk": settings.weight_small_chunk_vec,
        "summary": settings.weight_summary_vec,
        "big_chunk": settings.weight_big_chunk_vec,
    }.get(field, settings.weight_small_chunk_vec)


# For ranking purposes
def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


# Greedy Maximal Marginal Relevance (MMR) ordering.
# At each step, selects the next item that best balances:
# - relevance score (rel)
# - diversity from already selected items (penalized by token overlap)
# Produces an ordering that avoids redundancy while keeping highly relevant texts early.
# TODO: Make this inline
def mmr_order(texts: list[str], rel: list[float], lam: float) -> list[int]:
    """Greedy MMR ordering: lam*rel - (1-lam)*max_sim_to_selected (token overlap)."""
    remaining = set(range(len(texts)))
    order: list[int] = []
    while remaining:
        best_idx, best_score = None, float("-inf")
        for i in remaining:
            sim = max((token_jaccard(texts[i], texts[j]) for j in order), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * sim
            if score > best_score:
                best_score, best_idx = score, i
        order.append(best_idx)
        remaining.discard(best_idx)
    return order


def node_snippet(node: Node) -> str:
    return (node.summary or node.title or "").strip()


# Aggregates evidence hits per field and keeps only the best (lowest) rank for each field.
# Returns a rank-ordered list showing which fields matched the node most strongly (earlier rank = stronger match).
def evidence_why(node_hits: list[EvidenceHit]) -> list[dict[str, Any]]:
    """Best (lowest) rank per field that matched this node, rank-ascending."""
    best: dict[str, int] = {}
    for hit in node_hits:
        if hit.field not in best or hit.rank < best[hit.field]:
            best[hit.field] = hit.rank
    return [
        {"field": field, "rank": rank}
        for field, rank in sorted(best.items(), key=lambda kv: kv[1])
    ]


# Formats a candidate node result into a compact, llm-readable summary string
# for inspection/debugging. Includes node metadata, top match reasons ("why"),
# a few evidence snippets, and a suggested next action for downstream exploration.
def format_lead_candidate(result: dict[str, Any]) -> str:
    node = result["node"]
    why = result.get("why", [])
    why_str = ", ".join(f"{w['field']}#{w['rank']}" for w in why[:4]) or "n/a"
    lines = (
        f"- node_id: `{node.id}`\n"
        f"  title: {node.title}\n"
        f"  summary: {node.summary}\n"
        f"  why_matched: {why_str}"
    )
    evidence_lines: list[str] = []
    for ev in result.get("evidence", [])[:5]:
        snippet = " ".join((ev.get("text") or "").split())
        if snippet:
            evidence_lines.append(f"  - [{ev['field']}] {snippet[:700]}")
    evidence_text = (
        "\n".join(evidence_lines) if evidence_lines else "  - no evidence snippets"
    )
    lines += (
        f"\n  evidence:\n{evidence_text}\n"
        f"  next_action: if relevant, call explore(node_ids=['{node.id}']) "
        "or include this id with other candidates"
    )
    return lines


def strip_image_media(text: str) -> str:
    """Remove embedded image media payloads and keep image descriptions."""
    from graph.common.images import strip_images

    return strip_images(text, keep_descriptions=True).strip()


_BIG_TABLE_RE = re.compile(r"<table>.*?</table>", re.DOTALL)


def strip_big_tables(text: str, *, max_rows: int = 40) -> str:
    """Keep large spreadsheet tables out of prompts, embeddings, and FTS."""
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        rows = match.group(0).count("<tr")
        return match.group(0) if rows <= max_rows else f"[表: {rows} 行 — query_table で検索]"

    text = _BIG_TABLE_RE.sub(replace, text)
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            output.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        if end - index > max_rows:
            output.append(f"[表: {end - index} 行 — query_table で検索]\n")
        else:
            output.extend(lines[index:end])
        index = end
    return "".join(output)


_IMAGE_BLOCK_TYPES = {
    "image",
    "image_url",
    "input_image",
    "input_image_url",
}

_IMAGE_KEYS = {
    "image",
    "image_url",
    "input_image",
    "input_image_url",
    "b64_json",
}

_INLINE_IMAGE_DATA_RE = re.compile(
    r"data:(?:image/[a-z0-9.+-]+|application/octet-stream);base64,"
    r"[a-z0-9+/]+={0,2}",
    re.IGNORECASE,
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/=])"
)


def sanitize_text(value: str) -> str:
    """Apply shared image/base64 stripping before text reaches an LLM."""
    sanitized = strip_image_media(value)
    sanitized = _INLINE_IMAGE_DATA_RE.sub("[image omitted]", sanitized)
    return _LONG_BASE64_RE.sub("[base64 omitted]", sanitized)


def sanitize_messages(messages: list[Any]) -> list[Any]:
    """Sanitize LangChain/OpenAI-style messages before model calls."""
    return [_sanitize_message(message) for message in messages]


def sanitize_model_input(value: Any) -> Any:
    """Sanitize arbitrary nested input before it reaches a model backend."""
    return _sanitize_content(value)


def sanitize_tool_output(text: Any) -> str:
    """Sanitize tool observations before they become model-visible messages."""
    if not isinstance(text, str):
        text = str(text)
    return sanitize_text(text)


def _looks_like_image_data_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip().lower()
    return stripped.startswith("data:image/") or stripped.startswith(
        "data:application/octet-stream;base64,"
    )


def _sanitize_content(content: Any) -> Any:
    if isinstance(content, str):
        return sanitize_text(content)

    if isinstance(content, list):
        sanitized_items: list[Any] = []
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in _IMAGE_BLOCK_TYPES:
                    continue

                if any(key in item for key in _IMAGE_KEYS):
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        sanitized_text = sanitize_text(text_value).strip()
                        if sanitized_text:
                            sanitized_items.append(
                                {"type": "text", "text": sanitized_text}
                            )
                    continue

            sanitized_item = _sanitize_content(item)
            if sanitized_item not in ({}, [], "", None):
                sanitized_items.append(sanitized_item)
        return sanitized_items

    if isinstance(content, dict):
        item_type = str(content.get("type") or "").strip().lower()
        if item_type in _IMAGE_BLOCK_TYPES:
            return {}

        sanitized: dict[str, Any] = {}
        for key, value in content.items():
            key_lower = str(key).lower()
            if key_lower in _IMAGE_KEYS or _looks_like_image_data_url(value):
                continue

            sanitized_value = _sanitize_content(value)
            if sanitized_value not in ({}, [], "", None):
                sanitized[key] = sanitized_value
        return sanitized

    return content


def _sanitize_message(message: Any) -> Any:
    if isinstance(message, dict):
        return _sanitize_content(message)

    content = getattr(message, "content", None)
    if content is None:
        return message

    updates = {"content": _sanitize_content(content)}
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        updates["additional_kwargs"] = _sanitize_content(additional_kwargs)
    if hasattr(message, "model_copy"):
        return message.model_copy(update=updates)

    try:
        cloned = copy.copy(message)
        for key, value in updates.items():
            setattr(cloned, key, value)
        return cloned
    except Exception:
        return message


# endregion Helpers/Utils
