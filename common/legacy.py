"""Narrow compatibility boundary for the migration.

Only this module knows where the pre-refactor implementations live.  Phase
facades call these adapters while their internals are moved folder by folder.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .async_tools import run_async_blocking

log = logging.getLogger(__name__)


def _failed(operation: str, target: str, exc: BaseException) -> None:
    """One ERROR per adapter: this layer is the one that knows the document, path or project."""
    log.error("stage=%s failed %s error=%s: %s", operation, target, type(exc).__name__, exc, exc_info=True)


def _project(settings: Any) -> str:
    return f"project={getattr(settings, 'target_name', '') or getattr(settings, 'data_root', '')}"


def project(root: Path, mount: Path | None = None) -> Any:
    from graph.workspace.project import Project

    try:
        return Project(Path(root), Path(mount) if mount is not None else None).ensure()
    except Exception as exc:
        _failed("project-ensure", f"path={root}", exc)
        raise


def open_project(settings: Any) -> Any:
    from graph.workspace.project import open_project as open_legacy_project

    try:
        return open_legacy_project(settings)
    except Exception as exc:
        _failed("project-config", _project(settings), exc)
        raise


def convert_mount(project: Any, *, settings: Any, on_progress: Any = None) -> dict[str, Any]:
    from graph.workspace.convert import convert_mount as convert_legacy

    try:
        return convert_legacy(
            project,
            parser_base_url=str(getattr(settings, "parser_base_url", "")),
            settings=settings,
            on_progress=on_progress,
        )
    except Exception as exc:
        _failed("convert", _project(settings), exc)
        raise


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

    try:
        return read_legacy(source)
    except Exception as exc:
        _failed("read-source", f"path={source}", exc)
        raise


def build_wiki_output(*, source_path: Path, document_name: str, out_dir: Path, mode: str, settings: Any, llm: Any = None, embedder: Any = None, state_dir: Path | None = None, on_progress: Any = None, stop_check: Any = None, resume: bool = True) -> Any:
    from graph.workspace.writer import build_wiki_output as build_legacy

    try:
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
    except Exception as exc:
        _failed("wiki-build", f"document={document_name}", exc)
        raise


def publish_output(staged: Path, target: Path) -> int:
    from graph.workspace.writer import publish_output as publish_legacy

    try:
        return publish_legacy(staged, target)
    except Exception as exc:
        _failed("publish", f"path={target}", exc)
        raise


def link_document(project: Any, raw_rel: str, *, settings: Any, model: Any = None, embedder: Any = None, on_progress: Any = None, stop_check: Any = None) -> Any:
    from graph.linker import link_document as link_legacy

    try:
        return run_async_blocking(
            link_legacy(project, raw_rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check)
        )
    except Exception as exc:
        _failed("link", f"document={raw_rel}", exc)
        raise


def link_documents(project: Any, rels: list[str], *, settings: Any, model: Any = None, embedder: Any = None, on_progress: Any = None, stop_check: Any = None, changed_pages: set[str] | None = None, regenerated_pages: set[str] | None = None) -> Any:
    from graph.linker import link_documents as link_many_legacy

    try:
        return run_async_blocking(
            link_many_legacy(project, rels, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages, regenerated_pages=regenerated_pages)
        )
    except Exception as exc:
        _failed("link-batch", f"documents={len(rels)} first={rels[0] if rels else ''}", exc)
        raise


def build_wiki(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import build_wiki_only

    try:
        return build_wiki_only(settings, only=only, force=force, on_progress=on_progress)
    except Exception as exc:
        _failed("wiki-phase", _project(settings), exc)
        raise


def build_link(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import link_raw

    try:
        return link_raw(settings, only=only, force=force, on_progress=on_progress)
    except Exception as exc:
        _failed("link-phase", _project(settings), exc)
        raise


def build_all(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from publisher.pipeline import build_raw

    try:
        return build_raw(settings, only=only, force=force, on_progress=on_progress)
    except Exception as exc:
        _failed("build-phase", _project(settings), exc)
        raise


def sync(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.pipeline import sync_once

    try:
        return sync_once(settings, **kwargs)
    except Exception as exc:
        _failed("sync", _project(settings), exc)
        raise


def index(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.index import build_index

    try:
        return build_index(settings, **kwargs)
    except Exception as exc:
        _failed("index", _project(settings), exc)
        raise


def publish(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from publisher.pipeline import publish_only

    try:
        return publish_only(settings, **kwargs)
    except Exception as exc:
        _failed("publish-phase", _project(settings), exc)
        raise


__all__ = ["build_all", "build_link", "build_wiki", "build_wiki_output", "convert_mount", "index", "link_document", "link_documents", "open_project", "parse_document", "project", "publish", "publish_output", "sync"]
