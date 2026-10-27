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
        log.error("wiki export stopped, this run has no seed plan file=%s run=%s document=%s error=%s: %s (the run never produced a plan, so there is nothing to export)",
                  plan_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    except (OSError, ValueError) as exc:
        # storage.read_json already names the file and the errno; this names the export it stopped.
        log.error("wiki export stopped, the seed plan exists but could not be read file=%s run=%s document=%s error=%s: %s (corrupt or unreadable run state, not a missing file)",
                  plan_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    try:
        pages = plan["pages"]
    except KeyError as exc:
        log.error("wiki plan has no page list file=%s key=%s document=%s error=%s: %s",
                  plan_path, exc, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    manifest_path = run_root / "state" / "manifest.json"
    try:
        manifest = read_json(manifest_path, default={})
    except (OSError, ValueError) as exc:
        log.error("wiki export stopped, the run manifest could not be read file=%s run=%s document=%s error=%s: %s (judge scores and verbatim-section marks cannot be recovered without it)",
                  manifest_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    if not manifest:
        # shortcut: a run without a manifest is a normal older run, not a failure; only the
        # judge fields stay empty.  Upgrade to ERROR if the manifest ever becomes mandatory.
        log.debug("run has no judge manifest file=%s run=%s; the exported planning files carry no judge_score or verbatim_sections",
                  manifest_path, run_root)
    try:
        by_number = {int(item["number"]): item for item in manifest.get("pages", [])}
    except (KeyError, TypeError, ValueError) as exc:
        log.error("wiki run manifest has no usable page numbers file=%s run=%s document=%s error=%s: %s (the manifest exists but is corrupt, not missing)",
                  manifest_path, run_root, document_name, type(exc).__name__, exc, exc_info=True)
        raise

    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            log.error("export destination delete failed dest=%s document=%s error=%s: %s (the destination may be half-deleted)",
                      dest, document_name, type(exc).__name__, exc, exc_info=True)
            raise
    docs = dest / "docs"
    planning = dest / "_planning"
    try:
        docs.mkdir(parents=True)
        planning.mkdir(parents=True)
    except OSError as exc:
        log.error("export destination could not be created dest=%s document=%s error=%s: %s (the previous export contents may already be deleted)",
                  dest, document_name, type(exc).__name__, exc, exc_info=True)
        raise

    coverage: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for page in pages:
        try:
            filename = page["filename"]
        except KeyError as exc:
            log.error("planned page has no file name file=%s run=%s document=%s page_number=%s error=%s: %s",
                      plan_path, run_root, document_name, page.get("number"), type(exc).__name__, exc, exc_info=True)
            raise
        source = run_root / "wiki" / filename
        if not source.exists():
            log.error("wiki run has no published page for a planned page run=%s document=%s page=%s file=%s (the plan lists a page this run never wrote, so the export cannot be complete)",
                      run_root, document_name, filename, source)
            raise FileNotFoundError(f"wiki run has no published page {filename}")
        try:
            shutil.copyfile(source, docs / filename)
        except OSError as exc:
            log.error("export page copy failed page=%s path=%s run=%s document=%s error=%s: %s (disk full, permissions, or the source page disappeared)",
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
        log.error("export planning files are incomplete dest=%s document=%s error=%s: %s (the ingest layout at this path is unusable until the run is exported again)",
                  dest, document_name, type(exc).__name__, exc, exc_info=True)
        raise
    review = run_root / "wiki" / "_review.md"
    if review.exists():
        try:
            shutil.copyfile(review, planning / "_review.md")
        except OSError as exc:
            log.error("review file copy failed path=%s source=%s document=%s error=%s: %s (the pages are exported; only the review notes are missing)",
                      planning / "_review.md", review, document_name, type(exc).__name__, exc, exc_info=True)
            raise
    return dest
