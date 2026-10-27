"""Export one wiki run into the planning layout consumed by the librarian."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic

log = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"^\d+-(.+\.md)$")


def canonical_name(filename: str) -> str:
    match = _PREFIX_RE.match(filename)
    return match.group(1) if match else filename


def export_ingest_layout(run_root: Path, dest: Path, *, document_name: str) -> Path:
    """Copy published pages and write the ``ingest_md_output`` planning files."""

    run_root = Path(run_root)
    dest = Path(dest)
    plan_path = run_root / "state" / "plan.json"
    try:
        plan = read_json(plan_path)
    except FileNotFoundError as exc:
        log.error("基本構成がないため、Wikiの書き出しを中断します。構成が生成されていません: ファイル=%s、実行ID=%s、文書=%s、エラー=%s: %s",
                  plan_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    except (OSError, ValueError) as exc:
        # storage.read_json already names the file and the errno; this names the export it stopped.
        log.error("基本構成が破損しているか読めないため、Wikiの書き出しを中断します: ファイル=%s、実行ID=%s、文書=%s、エラー=%s: %s",
                  plan_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    try:
        pages = plan["pages"]
    except KeyError as exc:
        log.error("Wiki構成にページ一覧がありません: ファイル=%s、項目=%s、文書=%s、エラー=%s: %s",
                  plan_path, exc, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    manifest_path = run_root / "state" / "manifest.json"
    try:
        manifest = read_json(manifest_path, default={})
    except (OSError, ValueError) as exc:
        log.error("実行情報を読めないため、Wikiの書き出しを中断します。評価点と原文使用箇所を復元できません: ファイル=%s、実行ID=%s、文書=%s、エラー=%s: %s",
                  manifest_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    if not manifest:
        # shortcut: a run without a manifest is a normal older run, not a failure; only the
        # judge fields stay empty.  Upgrade to ERROR if the manifest ever becomes mandatory.
        log.debug("判定結果の管理ファイルがありません。出力する構成に評価点と原文使用箇所は含まれません: ファイル=%s、実行ID=%s",
                  manifest_path, run_root)
    try:
        by_number = {int(item["number"]): item for item in manifest.get("pages", [])}
    except (KeyError, TypeError, ValueError) as exc:
        log.error("実行情報のページ番号が不正です。ファイルが破損しています: ファイル=%s、実行ID=%s、文書=%s、エラー=%s: %s",
                  manifest_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise

    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            log.error("書き出し先を削除できません。一部だけ削除されている場合があります: 保存先=%s、文書=%s、エラー=%s: %s",
                      dest, document_name, type(exc).__name__, exc, exc_info=True)
            raise
    docs = dest / "docs"
    planning = dest / "_planning"
    try:
        docs.mkdir(parents=True)
        planning.mkdir(parents=True)
    except OSError as exc:
        log.error("書き出し先を作成できません。以前の出力は削除済みの場合があります: 保存先=%s、文書=%s、エラー=%s: %s",
                  dest, document_name, type(exc).__name__, exc, exc_info=True)
        raise

    coverage: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for page in pages:
        try:
            filename = page["filename"]
        except KeyError as exc:
            log.error("構成内のページにファイル名がありません: ファイル=%s、実行ID=%s、文書=%s、ページ番号=%s、エラー=%s: %s",
                      plan_path, run_root, document_name, page.get("number"), type(exc).__name__, exc, exc_info=True)
            raise
        source = run_root / "wiki" / filename
        if not source.exists():
            log.error("構成にあるページが生成されていないため、完全に書き出せません: 実行ID=%s、文書=%s、ページ=%s、ファイル=%s",
                      run_root, document_name, filename, source)
            raise FileNotFoundError(f"wiki run has no published page {filename}")
        try:
            shutil.copyfile(source, docs / filename)
        except OSError as exc:
            log.error("ページを書き出し先にコピーできません: ページ=%s、パス=%s、実行ID=%s、文書=%s、エラー=%s: %s（容量・権限・元ページの存在を確認してください）",
                      filename, docs / filename, run_root, document_name, type(exc).__name__, exc, exc_info=True)
            raise
        owner = page["owner_ranges"]
        header = page.get("chapter") or "一般"
        name = canonical_name(filename)
        coverage.append(
            {
                "title": page["title"],
                "filename": name,
                "summary": page.get("summary", ""),
                "header": header,
                "source_start": owner[0][0],
                "source_end": owner[-1][1],
            }
        )
        metadata.append({"name": name, "header": header})
        manifest_page = by_number.get(int(page["number"]), {})
        files.append(
            {
                "filename": filename,
                "title": page["title"],
                "source_ranges": owner,
                "reference_ranges": page.get("reference_ranges", []),
                "judge_score": manifest_page.get("judge_score"),
                "verbatim_sections": manifest_page.get("verbatim_sections", []),
            }
        )

    try:
        write_json_atomic(
            planning / "metadata.json",
            {
                "original_file_name": document_name,
                "inferred_file_name": document_name,
                "files": metadata,
            },
        )
        write_json_atomic(
            planning / "coverage.json",
            {
                "source_line_count": plan["source_line_count"],
                "file_count": len(coverage),
                "files": coverage,
            },
        )
        write_json_atomic(
            planning / "manifest.json",
            {
                "source": plan.get("source", ""),
                "source_sha256": plan.get("source_sha256", ""),
                "planning": {
                    "ingest_mode": "wiki",
                    "strategy": "wiki",
                    "file_count": len(files),
                    "prompt_version": plan.get("prompt_version", ""),
                },
                "files": files,
            },
        )
    except (OSError, ValueError) as exc:
        # _atomic_write already named the file and the errno; this adds what the caller loses.
        log.error("構成ファイルの書き出しが未完了です。再度書き出すまで取り込み用データを使えません: 保存先=%s、文書=%s、エラー=%s: %s",
                  dest, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    review = run_root / "wiki" / "_review.md"
    if review.exists():
        try:
            shutil.copyfile(review, planning / "_review.md")
        except OSError as exc:
            log.error("確認用メモをコピーできません。ページの書き出しは完了しています: パス=%s、コピー元=%s、文書=%s、エラー=%s: %s",
                      planning / "_review.md", review, document_name, type(exc).__name__, exc, exc_info=True)
            raise
    return dest
