"""Mount-to-raw conversion without Git."""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Any, Callable

from common.paths import SUPPORTED_SOURCE_SUFFIXES

from .parser_client import ParserSkip, UnsupportedDocument, parse_document, read_text_source
from .project import VERBATIM, Project, assert_unique_generated_paths, raw_name_for

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
log = logging.getLogger(__name__)


def convert_mount(project: Project, *, parser_base_url: str, settings: Any, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    log_path = project.convert_log_path
    seen = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    present: set[str] = set(); converted: list[str] = []; removed: list[str] = []; unsupported: list[str] = []; failed: list[str] = []
    failures: list[str] = []
    paths = [
        path for path in sorted(project.mount.rglob("*"))
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
        and path.name not in SKIP_NAMES and not path.name.startswith("~$")
    ]
    assert_unique_generated_paths(path.relative_to(project.mount).as_posix() for path in paths)
    for path in paths:
        rel = path.relative_to(project.mount).as_posix(); present.add(rel); stat = path.stat()
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
                markdown = parse_document(
                    path,
                    base_url=parser_base_url,
                    settings=settings,
                    previous_markdown=target.read_text(encoding="utf-8") if target.exists() else None,
                )
        except ParserSkip as exc:
            # TEMPORARY: report the skip, but do not persist an exclusion in convert.json.
            unsupported.append(rel)
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            log.warning("ドキュメントをスキップ: %s: %s", rel, exc, exc_info=True)
            continue
        except UnsupportedDocument as exc:
            seen[rel] = {**key, "unsupported": str(exc)}; unsupported.append(rel); continue
        except Exception:
            failures.append(f"{rel}: {traceback.format_exc()}")
            failed.append(rel); continue
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text(markdown, encoding="utf-8")
        seen[rel] = key; converted.append(rel)
        if on_progress:
            on_progress({"stage": "convert", "file": rel})
    for rel in list(seen):
        if Path(rel).suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
            continue
        if rel not in present:
            target = project.raw / Path(rel).parent / raw_name_for(Path(rel).name)
            target.unlink(missing_ok=True); seen.pop(rel); removed.append(rel)
    project.metadata.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"converted": converted, "removed": removed, "unsupported": unsupported, "failed": failed, "failures": failures}


__all__ = ["UnsupportedDocument", "convert_mount", "parse_document"]
