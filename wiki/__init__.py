"""Standalone raw-Markdown to wiki generation phase."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.context import Context
from common.legacy import build_wiki_output, project, publish_output
from common.paths import DataLayout
from common.policy import STANDARD, Policy, resolve_policy
from common.storage import read_json, write_json_atomic


@dataclass(frozen=True)
class Config:
    mode: str = "wiki"
    policy: Policy = Policy()
    settings: Any = None
    source_kind: str = "md"

    @classmethod
    def from_settings(cls, settings: Any, *, policy: str | Policy | None = None) -> "Config":
        chosen = policy if isinstance(policy, Policy) else resolve_policy(policy or getattr(settings, "policy", "standard"))
        return cls(mode=str(getattr(settings, "ingest_mode", "wiki")), policy=chosen, settings=settings)


@dataclass(frozen=True)
class Input:
    raw: Path
    relative: str = ""
    force_full: bool = False


@dataclass(frozen=True)
class Result:
    document: str
    raw: str
    tier: int
    reason: str
    changed_pages: tuple[str, ...]
    regenerated_pages: tuple[str, ...]
    policy: str


def _settings(cfg: Config) -> Any:
    if cfg.settings is not None:
        return cfg.settings
    return SimpleNamespace(
        ingest_mode=cfg.mode,
        policy=cfg.policy.name,
        chat_base_url="",
        chat_api_key="local",
        chat_model="",
        chat_temperature=0.7,
        concurrency=1,
        ingest_concurrency=1,
        wiki_planner_concurrency=1,
        wiki_rewrite_concurrency=1,
        wiki_linker_concurrency=1,
        wiki_section_target_lines=80,
        wiki_write_attempts=cfg.policy.repair_attempts,
        wiki_request_timeout=300,
        wiki_output_language="Japanese (日本語)",
    )


def run(cfg: Config, inp: Input, out: Path, ctx: Context | None = None) -> Result:
    source = Path(inp.raw)
    if not source.is_file():
        raise FileNotFoundError(source)
    layout = DataLayout(Path(out)).ensure()
    raw_rel = inp.relative or source.name
    raw_target = layout.raw_file(raw_rel)
    raw_target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != raw_target.resolve():
        shutil.copyfile(source, raw_target)
    document = layout.document(raw_rel)
    settings = _settings(cfg)
    settings.policy = cfg.policy.name
    state_dir = layout.state_dir(raw_rel)
    work_dir = layout.work_dir(raw_rel)
    output = work_dir / "generated"
    with (ctx.stage("wiki", document) if ctx is not None else _null_context()):
        result = build_wiki_output(
            source_path=raw_target,
            document_name=raw_rel,
            out_dir=output,
            mode=cfg.mode,
            settings=settings,
            llm=ctx.llm if ctx is not None else None,
            embedder=ctx.embedder if ctx is not None else None,
            state_dir=state_dir,
            on_progress=ctx.emit if ctx is not None else None,
            resume=not inp.force_full,
        )
        publish_output(result.out_dir, layout.wiki_dir(raw_rel))
        if cfg.policy is not STANDARD:
            stamp = read_json(state_dir / "run.json", default={})
            stamp["policy"] = cfg.policy.name
            stamp["policy_version"] = cfg.policy.version
            write_json_atomic(state_dir / "run.json", stamp)
    plan = read_json(state_dir / "state" / "plan.json", default={})
    pages = tuple(str(item.get("filename")) for item in plan.get("pages", []) if item.get("filename"))
    return Result(document, raw_rel, 3 if inp.force_full else 0, "forced" if inp.force_full else "built", pages, pages, cfg.policy.name)


def run_project(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from common.legacy import build_wiki

    return build_wiki(settings, only=only, force=force, on_progress=on_progress)


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


__all__ = ["Config", "Input", "Policy", "Result", "run", "run_project"]
