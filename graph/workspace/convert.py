"""Mount-to-raw conversion without Git."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from common.paths import SUPPORTED_SOURCE_SUFFIXES

from .parser_client import ParserSkip, UnsupportedDocument, parse_document, read_text_source
from .project import VERBATIM, Project, assert_unique_generated_paths, raw_name_for, walk_files

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
log = logging.getLogger(__name__)


def convert_mount(project: Project, *, parser_base_url: str, settings: Any, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    log_path = project.convert_log_path
    seen = {}
    if log_path.exists():
        try:
            seen = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("convert state is corrupt, the run stops before converting anything path=%s error=%s: %s", log_path, type(exc).__name__, exc, exc_info=True)
            raise
    present: set[str] = set(); converted: list[str] = []; removed: list[str] = []; unsupported: list[str] = []; failed: list[str] = []
    failures: list[str] = []
    paths = [
        path for path in sorted(walk_files(project.mount))
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
        and path.name not in SKIP_NAMES and not path.name.startswith("~$")
    ]
    assert_unique_generated_paths(path.relative_to(project.mount).as_posix() for path in paths)
    if not parser_base_url:
        needs_parser = sum(1 for path in paths if path.suffix.lower() not in VERBATIM)
        if needs_parser:
            # shortcut: counts every supported non-Markdown file, not only the changed ones.
            log.error("WIKI_PARSER_BASE_URL is not set, non-Markdown documents will not convert files=%d setting=WIKI_PARSER_BASE_URL mount=%s", needs_parser, project.mount)
    for path in paths:
        rel = path.relative_to(project.mount).as_posix(); present.add(rel)
        try:
            stat = path.stat()
        except OSError as exc:
            log.error("stat failed, the convert run aborted file=%s path=%s error=%s: %s", rel, path, type(exc).__name__, exc, exc_info=True)
            raise
        key = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        if seen.get(rel, {}).get("mtime_ns") == key["mtime_ns"] and seen.get(rel, {}).get("size") == key["size"]:
            continue
        target = project.raw / Path(rel).parent / raw_name_for(Path(rel).name)
        try:
            if path.suffix.lower() in VERBATIM:
                markdown = read_text_source(path)
            else:
                if not parser_base_url:
                    raise RuntimeError("WIKI_PARSER_BASE_URL is required for non-Markdown files")
                previous = None
                if target.exists():
                    try:
                        previous = target.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        # The previous raw output, not the mount source, is what cannot be read here.
                        log.error("previous raw output is unreadable path=%s encoding=utf-8 error=%s: %s", target, type(exc).__name__, exc, exc_info=True)
                        raise
                markdown = parse_document(
                    path,
                    base_url=parser_base_url,
                    settings=settings,
                    previous_markdown=previous,
                )
        except ParserSkip as exc:
            # TEMPORARY: report the skip, but do not persist an exclusion in convert.json.
            unsupported.append(rel)
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            log.warning("ドキュメントをスキップ: %s: %s", rel, exc, exc_info=True)
            continue
        except UnsupportedDocument as exc:
            log.error("parser rejected the document, it will never publish file=%s error=%s: %s", rel, type(exc).__name__, exc, exc_info=True)
            seen[rel] = {**key, "unsupported": str(exc)}; unsupported.append(rel); continue
        except Exception as exc:
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            failed.append(rel)
            # A missing WIKI_PARSER_BASE_URL is reported once, above, with the affected file count.
            if parser_base_url or path.suffix.lower() in VERBATIM:
                log.error("convert failed file=%s error=%s: %s", rel, type(exc).__name__, exc, exc_info=True)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            log.error("raw output write failed, the run aborts and convert.json is never saved so every file re-parses next run file=%s path=%s error=%s: %s", rel, target, type(exc).__name__, exc, exc_info=True)
            raise
        seen[rel] = key; converted.append(rel)
        if on_progress:
            on_progress({"stage": "convert", "file": rel})
    for rel in list(seen):
        if Path(rel).suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
            continue
        if rel not in present:
            target = project.raw / Path(rel).parent / raw_name_for(Path(rel).name)
            target.unlink(missing_ok=True); seen.pop(rel); removed.append(rel)
    try:
        project.metadata.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("convert state write failed, every file re-parses next run path=%s error=%s: %s", log_path, type(exc).__name__, exc, exc_info=True)
        raise
    return {"converted": converted, "removed": removed, "unsupported": unsupported, "failed": failed, "failures": failures}


__all__ = ["UnsupportedDocument", "convert_mount", "parse_document"]
