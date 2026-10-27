"""Narrow compatibility boundary for the migration.

Only this module knows where the pre-refactor implementations live.  Phase
facades call these adapters while their internals are moved folder by folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .async_tools import run_async_blocking


def project(root: Path, mount: Path | None = None) -> Any:
    from graph.workspace.project import Project

    return Project(Path(root), Path(mount) if mount is not None else None).ensure()


def open_project(settings: Any) -> Any:
    from graph.workspace.project import open_project as open_legacy_project

    return open_legacy_project(settings)


def convert_mount(project: Any, *, settings: Any, on_progress: Any = None) -> dict[str, Any]:
    from graph.workspace.convert import convert_mount as convert_legacy

    return convert_legacy(
        project,
        parser_base_url=str(getattr(settings, "parser_base_url", "")),
        settings=settings,
        on_progress=on_progress,
    )


def parse_document(source: Path, *, base_url: str, settings: Any, timeout_s: float, previous_markdown: str | None = None, describe_images: bool = True) -> str:
    from graph.workspace.parser_client import parse_document as parse_legacy

    return parse_legacy(
        source,
        base_url=base_url,
        settings=settings,
        timeout_s=timeout_s,
        previous_markdown=previous_markdown,
        describe_images=describe_images,
    )


def read_text_source(source: Path) -> str:
    from graph.workspace.parser_client import read_text_source as read_legacy

    return read_legacy(source)


def build_wiki_output(*, source_path: Path, document_name: str, out_dir: Path, mode: str, settings: Any, llm: Any = None, embedder: Any = None, state_dir: Path | None = None, on_progress: Any = None, stop_check: Any = None, resume: bool = True) -> Any:
    from graph.workspace.writer import build_wiki_output as build_legacy

    return build_legacy(
        source_path=source_path,
        document_name=document_name,
        out_dir=out_dir,
        mode=mode,
        settings=settings,
        llm=llm,
        embedder=embedder,
        state_dir=state_dir,
        on_progress=on_progress,
        stop_check=stop_check,
        resume=resume,
    )


def publish_output(staged: Path, target: Path) -> int:
    from graph.workspace.writer import publish_output as publish_legacy

    return publish_legacy(staged, target)


def link_document(project: Any, raw_rel: str, *, settings: Any, model: Any = None, embedder: Any = None, on_progress: Any = None, stop_check: Any = None) -> Any:
    from graph.linker import link_document as link_legacy

    return run_async_blocking(
        link_legacy(project, raw_rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check)
    )


def link_documents(project: Any, rels: list[str], *, settings: Any, model: Any = None, embedder: Any = None, on_progress: Any = None, stop_check: Any = None, changed_pages: set[str] | None = None, regenerated_pages: set[str] | None = None) -> Any:
    from graph.linker import link_documents as link_many_legacy

    return run_async_blocking(
        link_many_legacy(project, rels, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages, regenerated_pages=regenerated_pages)
    )


def build_wiki(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import build_wiki_only

    return build_wiki_only(settings, only=only, force=force, on_progress=on_progress)


def build_link(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import link_raw

    return link_raw(settings, only=only, force=force, on_progress=on_progress)


def build_all(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import build_raw

    return build_raw(settings, only=only, force=force, on_progress=on_progress)


def sync(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.pipeline import sync_once

    return sync_once(settings, **kwargs)


def index(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.index import build_index

    return build_index(settings, **kwargs)


def publish(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.pipeline import publish_only

    return publish_only(settings, **kwargs)


__all__ = ["build_all", "build_link", "build_wiki", "build_wiki_output", "convert_mount", "index", "link_document", "link_documents", "open_project", "parse_document", "project", "publish", "publish_output", "sync"]
