"""Standalone wiki-to-links phase facade."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.context import Context
from common.legacy import build_link, link_documents, project
from common.paths import DataLayout
from common.policy import Policy, resolve_policy
from common.storage import read_json


@dataclass(frozen=True)
class Config:
    mode: str = "neo"
    policy: Policy = Policy()
    settings: Any = None

    @classmethod
    def from_settings(cls, settings: Any, *, policy: str | Policy | None = None) -> "Config":
        chosen = policy if isinstance(policy, Policy) else resolve_policy(policy or getattr(settings, "policy", "standard"))
        return cls(str(getattr(settings, "wiki_linker_mode", "neo")), chosen, settings)


@dataclass(frozen=True)
class Input:
    root: Path
    documents: tuple[str, ...] = ()
    changed_pages: tuple[str, ...] = ()
    regenerated_pages: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    touched_documents: tuple[str, ...]
    policy: str
    calls: int = 0


def _settings(cfg: Config) -> Any:
    if cfg.settings is not None:
        return cfg.settings
    return SimpleNamespace(wiki_linker_enabled=True, wiki_linker_mode=cfg.mode, policy=cfg.policy.name, concurrency=1)


def _raw_rel(layout: DataLayout, document: Path) -> str:
    source = read_json(document / "_planning" / "source.json", default={})
    if source.get("raw"):
        return str(source["raw"])
    return (document.relative_to(layout.wiki).parent / f"{document.name.replace('.', '_', 1)}.md").as_posix()


def _documents(layout: DataLayout, requested: tuple[str, ...]) -> list[str]:
    if requested:
        return list(requested)
    return sorted(path.parent.parent.relative_to(layout.wiki).as_posix() for path in layout.wiki.rglob("_planning/metadata.json"))


def run(cfg: Config, inp: Input, out: Path, ctx: Context | None = None) -> Result:
    layout = DataLayout(Path(out)).ensure()
    settings = _settings(cfg)
    settings.wiki_linker_mode = cfg.mode
    settings.policy = cfg.policy.name
    documents = _documents(layout, inp.documents)
    raw_rels = [_raw_rel(layout, layout.wiki / document) for document in documents]
    target = project(layout.root)
    with (ctx.stage("linker", "<batch>") if ctx is not None else _null_context()):
        linked = link_documents(
            target,
            raw_rels,
            settings=settings,
            model=ctx.llm if ctx is not None else None,
            embedder=ctx.embedder if ctx is not None else None,
            on_progress=ctx.emit if ctx is not None else None,
            changed_pages=set(inp.changed_pages) if inp.changed_pages else None,
            regenerated_pages=set(inp.regenerated_pages) if inp.regenerated_pages else None,
        )
    touched = tuple(getattr(linked, "touched_documents", ()) or ())
    return Result(touched, cfg.policy.name, sum(ctx.calls.values()) if ctx is not None else 0)


def run_project(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    return build_link(settings, only=only, force=force, on_progress=on_progress)


def status(root: Path) -> dict[str, Any]:
    database = DataLayout(Path(root)).linker_database
    if not database.exists():
        return {"mode": None, "documents": 0, "chunks": 0, "edges": 0}
    with sqlite3.connect(database) as connection:
        mode = connection.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        return {"mode": mode[0] if mode else None, **{table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("documents", "chunks", "edges")}}


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


__all__ = ["Config", "Input", "Policy", "Result", "run", "run_project", "status"]
