"""Export one wiki run into the planning layout consumed by the librarian."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic

_PREFIX_RE = re.compile(r"^\d+-(.+\.md)$")


def canonical_name(filename: str) -> str:
    match = _PREFIX_RE.match(filename)
    return match.group(1) if match else filename


def export_ingest_layout(run_root: Path, dest: Path, *, document_name: str) -> Path:
    """Copy published pages and write the ``ingest_md_output`` planning files."""

    run_root = Path(run_root)
    dest = Path(dest)
    plan = read_json(run_root / "state" / "plan.json")
    manifest = read_json(run_root / "state" / "manifest.json", default={})
    by_number = {int(item["number"]): item for item in manifest.get("pages", [])}

    if dest.exists():
        shutil.rmtree(dest)
    docs = dest / "docs"
    planning = dest / "_planning"
    docs.mkdir(parents=True)
    planning.mkdir(parents=True)

    coverage: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for page in plan["pages"]:
        filename = page["filename"]
        source = run_root / "wiki" / filename
        if not source.exists():
            raise FileNotFoundError(f"wiki run has no published page {filename}")
        shutil.copyfile(source, docs / filename)
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
    review = run_root / "wiki" / "_review.md"
    if review.exists():
        shutil.copyfile(review, planning / "_review.md")
    return dest
