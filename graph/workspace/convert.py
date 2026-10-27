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
            log.error("変換状態が破損しているため、変換を始める前に中断します: パス=%s、エラー=%s: %s", log_path, type(exc).__name__, exc, exc_info=True)
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
            log.error("WIKI_PARSER_BASE_URL が未設定のため、Markdown以外の文書を変換できません: ファイル数=%d、入力フォルダー=%s", needs_parser, project.mount)
    for path in paths:
        rel = path.relative_to(project.mount).as_posix(); present.add(rel)
        try:
            stat = path.stat()
        except OSError as exc:
            log.error("ファイル情報を取得できないため、変換を中断しました: ファイル=%s、パス=%s、エラー=%s: %s", rel, path, type(exc).__name__, exc, exc_info=True)
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
                        log.error("以前の変換テキストをUTF-8で読み込めません: パス=%s、エラー=%s: %s", target, type(exc).__name__, exc, exc_info=True)
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
            log.warning("文書の処理を省略します: %s: %s", rel, exc, exc_info=True)
            continue
        except UnsupportedDocument as exc:
            log.error("解析サービスが文書を拒否したため、公開できません: ファイル=%s、エラー=%s: %s", rel, type(exc).__name__, exc, exc_info=True)
            seen[rel] = {**key, "unsupported": str(exc)}; unsupported.append(rel); continue
        except Exception as exc:
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            failed.append(rel)
            # A missing WIKI_PARSER_BASE_URL is reported once, above, with the affected file count.
            if parser_base_url or path.suffix.lower() in VERBATIM:
                log.error("変換に失敗しました: ファイル=%s、エラー=%s: %s", rel, type(exc).__name__, exc, exc_info=True)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            log.error("変換テキストを書き込めないため中断します。convert.json は保存されず、次回は全ファイルを再解析します: ファイル=%s、パス=%s、エラー=%s: %s", rel, target, type(exc).__name__, exc, exc_info=True)
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
        log.error("変換状態を保存できません。次回はすべてのファイルを再解析します: パス=%s、エラー=%s: %s", log_path, type(exc).__name__, exc, exc_info=True)
        raise
    return {"converted": converted, "removed": removed, "unsupported": unsupported, "failed": failed, "failures": failures}


__all__ = ["UnsupportedDocument", "convert_mount", "parse_document"]
