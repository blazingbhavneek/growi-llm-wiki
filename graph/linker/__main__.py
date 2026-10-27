from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import main
from graph.common.async_tools import run_async_blocking
from graph.config import Settings, resolve_project_path
from graph.workspace.project import Project, open_project
from graph.workspace.writer import wiki_config
from common.policy import policy_of

from . import link_document
from .catalog import Catalog


def _documents(project: Project) -> list[str]:
    return sorted(
        path.parent.parent.relative_to(project.wiki).as_posix()
        for path in project.wiki.rglob("_planning/metadata.json")
    )


def _raw_rel(project: Project, document: str) -> str:
    marker = project.wiki / document / "_planning" / "source.json"
    if marker.exists():
        return str(__import__("json").loads(marker.read_text(encoding="utf-8")).get("raw", document))
    chunks = project.wiki / document / "_planning" / "chunks.json"
    if chunks.exists():
        cached = str(__import__("json").loads(chunks.read_text(encoding="utf-8")).get("raw_rel") or "")
        if cached:
            return cached
    # Older output without a stamp: invert the wiki folder naming rule.
    from pathlib import PurePosixPath

    from graph.workspace.project import raw_name_for

    path = PurePosixPath(document)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _model_and_embedder(settings: Settings, project: Project, document: str):
    cfg = wiki_config(settings, run_dir=project.metadata / "state" / document)
    model = policy_of(cfg).model_port(cfg)
    try:
        from graph.clients.embeddings import Embedder
        embedder = Embedder(settings)
    except Exception:
        embedder = None
    return model, embedder


def rebuild(project: Project, settings: Settings, mode: str, no_edges: bool = False) -> None:
    if project.linker_database.exists():
        project.linker_database.unlink()
    documents = _documents(project)
    for document in documents:
        (project.wiki / document / "_planning" / "links.json").unlink(missing_ok=True)
    settings.wiki_linker_mode = mode
    for document in documents:
        planning = project.wiki / document / "_planning"
        if no_edges:
            run_async_blocking(link_document(project, _raw_rel(project, document), model=None, embedder=None, settings=settings))
            continue
        raw_rel = _raw_rel(project, document)
        model, embedder = _model_and_embedder(settings, project, document)
        run_async_blocking(link_document(project, raw_rel, model=model, embedder=embedder, settings=settings))


async def _calibrate(engine, rows):
    from jev import JevQuestion, JevRequest
    requests = [JevRequest({"target": str(row["text_a"])[:6000], "candidate": str(row["text_b"])[:6000]},
                           JevQuestion("候補の節は、対象の節の読者が内容を理解・実行するために読むべき具体的な情報（前提・結果・制約・代替・同じ対象の別の側面など）を含んでいますか？ 同じ語が出てくるだけ、一般的な関連があるだけなら いいえ と答えてください。\n選択肢: はい / いいえ", key=str(i)))
                for i, row in enumerate(rows)]
    return await engine.adecide_batch(requests, return_exceptions=True)


def calibrate_jev(project: Project, settings: Settings, sample: int) -> None:
    from .prompts import EDGE_VERSION_NEO
    catalog = Catalog.open(project.linker_database, mode="neo")
    try:
        rows = catalog.conn.execute(
            "SELECT d.accepted,a.body AS text_a,b.body AS text_b FROM edge_decisions d "
            "JOIN chunks a ON a.text_sha256=d.hash_a JOIN chunks b ON b.text_sha256=d.hash_b "
            "WHERE d.mode='neo' AND d.edge_version=? ORDER BY d.hash_a,d.hash_b LIMIT ?",
            (EDGE_VERSION_NEO, sample)).fetchall()
    finally:
        catalog.close()
    if not rows:
        print("No LLM edge decisions available for calibration")
        return
    from jev import get_engine_for
    results = run_async_blocking(_calibrate(get_engine_for(settings), rows))
    pairs = [(float(result.p_yes), bool(row["accepted"])) for result, row in zip(results, rows)
             if not isinstance(result, BaseException)]
    positives = sum(actual for _probability, actual in pairs)
    best = None
    for threshold in (i / 10 for i in range(3, 10)):
        tp = sum(p >= threshold and actual for p, actual in pairs)
        predicted = sum(p >= threshold for p, _actual in pairs)
        precision = tp / predicted if predicted else 0.0
        recall = tp / positives if positives else 0.0
        print(f"threshold={threshold:.1f} precision={precision:.3f} recall={recall:.3f} sample={len(pairs)}")
        if precision >= .9 and (best is None or recall > best[1]): best = (threshold, recall)
    print(f"best_precision_at_least_0.9={best[0]:.1f} recall={best[1]:.3f}" if best else "no threshold reached 0.9 precision")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m graph.linker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--data-root", help="override selected INI data_root")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild_parser = sub.add_parser("rebuild")
    rebuild_parser.add_argument("--mode", choices=("legacy", "neo"), required=True)
    rebuild_parser.add_argument("--no-edges", action="store_true")
    relink_parser = sub.add_parser("relink")
    relink_parser.add_argument("document")
    calibrate_parser = sub.add_parser("calibrate-jev")
    calibrate_parser.add_argument("--sample", type=int, default=300)
    sub.add_parser("status")
    args = parser.parse_args()
    settings = Settings.from_env(args.project)
    if args.data_root:
        settings.data_root = str(resolve_project_path(args.data_root).resolve())
    project = open_project(settings)
    if args.command == "status":
        if not project.linker_database.exists():
            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0})
            return
        with sqlite3.connect(project.linker_database) as conn:
            mode = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("documents", "chunks", "edges")
            }
        print({"mode": mode[0] if mode else None, **counts})
        return
    if args.command == "rebuild":
        rebuild(project, settings, args.mode, args.no_edges)
        return
    if args.command == "calibrate-jev":
        calibrate_jev(project, settings, args.sample)
        return
    rel = args.document.strip("/")
    if rel in _documents(project):
        rel = _raw_rel(project, rel)
    elif not project.raw_file(rel).exists():
        raise SystemExit(f"unknown document: {args.document} (expected a wiki folder or raw rel path)")
    model, embedder = _model_and_embedder(settings, project, Path(rel).parent.as_posix())
    settings.wiki_linker_mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings))


if __name__ == "__main__":
    main()
