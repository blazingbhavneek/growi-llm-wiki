"""One entry point for the publisher. `python main.py -h`.

check                       ping chat/embed/parser/GROWI endpoints + the local Jev backend
convert                     external mount -> raw Markdown only
build wiki [<raw-rel>...]   raw/ -> wiki pages only
build link [<raw-rel>...]   link pending wiki pages only
build all [<raw-rel>...]    wiki batch first, then link batch (bare build is an alias)
publish                     publish the current wiki/ tree to GROWI
pull                        capture GROWI edits and update the local human overlay
index [<raw-rel>...]        publish per-document + root index pages for growi-search
sync [<mount-rel>...]       scan and drain queue -> candidate -> GROWI -> commit
                            then reconcile every index page against the wiki tree
watch [<mount-rel>...]      queued 10-second metadata watcher + worker
queue scan|work|status      operate the persistent watcher queue
reset                       trash all publisher-owned GROWI pages
link                        link every pending wiki (same as `build link`)
link status | relink <doc> | rebuild --mode legacy|neo [--no-edges]

Every command selects one INI from configs/ (or an absolute INI) with --project.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from common.policy import policy_of
from pathlib import Path
from typing import Any

from graph import config
from graph.config import Settings, resolve_project_path
from graph.workspace.parser_client import parser_skip_scope
from graph.workspace.project import open_project
from publisher.failure_logs import capture_failure_logs, capture_run_logs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
config.PROJECT_ROOT = PROJECT_ROOT

log = logging.getLogger(__name__)
LOG_FORMAT = "%(levelname)s %(message)s"


def _interrupted_fast_repair(
    project: Any,
) -> tuple[bool, frozenset[str], frozenset[str]]:
    """Identify the narrow dirty tree and page checkpoints from a stopped repair.

    A repair commits page outcomes into ``metadata/state`` as model calls finish,
    then promotes/publishes the whole document. Stopping inside that window must
    be resumable, but it must not turn ``--continue`` into a general dirty-tree
    bypass for wiki, ledger, source, or human-sync changes.
    """

    root = Path(project.root)
    if not (root / ".git").is_dir():
        return False, frozenset(), frozenset()
    durable = (
        ".gitignore", "sources", "raw", "wiki", "metadata/state",
        "metadata/pipeline.json", "metadata/source-identities.json",
        "metadata/human-sync",
    )
    try:
        status = subprocess.run(
            [
                "git", "-C", str(root), "status", "--porcelain=v1", "-z",
                "--untracked-files=all", "--", *durable,
            ],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False, frozenset(), frozenset()
    entries = [entry for entry in status.split(b"\0") if entry]
    if not entries:
        return False, frozenset(), frozenset()

    saw_repair_artifact = False
    page_states: list[Path] = []
    for entry in entries:
        if len(entry) < 4 or entry[2:3] != b" ":
            return False, frozenset(), frozenset()
        path = entry[3:].decode("utf-8", errors="surrogateescape")
        if not path.startswith("metadata/state/"):
            return False, frozenset(), frozenset()
        if "/work/repair/" in path:
            saw_repair_artifact = True
            continue
        if "/state/pages/" in path:
            page_states.append(root / path)
            continue
        if "/wiki/" in path:
            continue
        if path.endswith((
            "/state/plan.json", "/state/manifest.json", "/state/run.json",
        )):
            continue
        return False, frozenset(), frozenset()
    if not saw_repair_artifact:
        return False, frozenset(), frozenset()

    versions: set[str] = set()
    checkpoints: set[str] = set()
    for path in page_states:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True, frozenset(), frozenset()
        version = str(row.get("repair_version") or "") if isinstance(row, dict) else ""
        status = str(row.get("repair_status") or "") if isinstance(row, dict) else ""
        if not version or status not in {
            "clean", "repaired", "review", "initial-judge-pending",
            "rewrite-pending", "judge-pending"
        }:
            return True, frozenset(), frozenset()
        versions.add(version)
        checkpoints.add(path.relative_to(root).as_posix())
    return True, frozenset(checkpoints), frozenset(versions)


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(getattr(args, "project", ""))
    # .env (OPENAI_*) is the default; the project INI is
    # project-specific and already wins inside Settings.from_env.
    if getattr(args, "data_root", None):
        settings.data_root = str(resolve_project_path(args.data_root).resolve())
    if getattr(args, "mode", None) and args.command != "link":
        settings.ingest_mode = args.mode
    linker = getattr(args, "linker", None)
    if linker == "off":
        settings.wiki_linker_enabled = False
    elif linker:
        settings.wiki_linker_enabled = True
        settings.wiki_linker_mode = linker
    if getattr(args, "timeout", None):
        settings.wiki_request_timeout = args.timeout
    if getattr(args, "command", "") == "sync" and not getattr(args, "full", False):
        settings.policy = "fast"
    elif getattr(args, "command", "") == "sync":
        settings.policy = "standard"
    if getattr(args, "skip_excel_and_small", False):
        settings.skip_excel_and_small = True
    if not policy_of(settings).link:
        # every stage already honours this switch: no link phase or link-ahead, and
        # each document gets a "disabled" marker, which publication accepts
        settings.wiki_linker_enabled = False
    return settings


def _progress(event: dict[str, Any]) -> None:
    event = dict(event)
    stage = str(event.pop("stage", "work"))
    step = str(event.pop("step", ""))
    current, total = event.get("current"), event.get("total")
    progress = ""
    if isinstance(current, int) and isinstance(total, int) and total > 0:
        progress = f" {current}/{total} ({current * 100 // total}%)"
        event.pop("current", None)
        event.pop("total", None)
    details = json.dumps(event, ensure_ascii=False, default=str)
    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())


def _report(result: dict[str, Any], settings: Settings | None = None) -> int:
    for row in result.get("done", []):
        print(json.dumps(row, ensure_ascii=False))
    failures = result.get("failures", []) + result.get("scan_failures", [])
    if settings is not None and getattr(settings, "target_name", ""):
        from publisher.failure_logs import report_failure_summary

        report_failure_summary(settings, failures, result.get("warnings", []))
    else:
        for failure in failures:
            logging.error("ドキュメントの処理に失敗: %s", failure)
    return 1 if failures else 0


def _batch_failures(result: dict[str, Any]) -> list[str]:
    """Keep mount paths when a queue operation reports only a shared exception."""
    paths = result.get("paths", [])
    return [failure if not paths or any(path in failure for path in paths)
            else f"{', '.join(paths)}: {failure}" for failure in result.get("failures", [])]


def _jev_check(settings: Settings) -> tuple[str, int]:
    """Resolve the project's Jev backend and, for gguf, start the local runtime once.

    The linker and the index builder only log a warning when the Jev engine fails to
    start and then fall back to the LLM, so a broken GGUF path would surface as slow,
    differently-scored links instead of an error. This proves the configured snapshot
    and jev-score binary answer one decision, without any sync, and it never downloads:
    a missing snapshot is reported rather than fetched.
    """

    from jev import JevConfig, JevQuestion, get_engine_for, reset_engine

    try:
        config = JevConfig.from_settings(settings)
    except Exception as exc:
        return f"jev     DOWN config ({type(exc).__name__}: {exc})", 1
    wanted = (str(getattr(settings, "wiki_linker_judge", "llm")) == "jev"
              or bool(getattr(settings, "wiki_index_related_docs", False)))
    if not wanted:
        return f"jev     skipped (judge={settings.wiki_linker_judge}, related-docs off)", 0
    if config.backend != "gguf":
        return f"jev     {config.backend} backend (local start not checked)", 0
    if not config.gguf_local_path:
        return "jev     DOWN wiki_jev_gguf_local_path is empty", 1
    folder = Path(config.gguf_local_path).expanduser()
    files = ("jev_style_decision_gguf.py", "readout_config.json", "tokenizer/tokenizer.json",
             f"Jev-Style-0.8B-Decision-v3-{config.gguf_quant}.gguf")
    missing = [name for name in files if not (folder / name).is_file()]
    binary = Path(config.gguf_binary).expanduser() if config.gguf_binary else folder / "build" / "jev-score"
    if not binary.is_file():
        missing.append(str(binary))
    elif not os.access(binary, os.X_OK):
        return f"jev     DOWN {binary} is not executable", 1
    if missing:
        return f"jev     DOWN missing from {folder}: {', '.join(missing)}", 1
    try:
        engine = get_engine_for(settings)
        result = engine.decide(
            {"section": {"page": "check", "heading": "接続手順",
                         "text": "本節ではシステムAからシステムBへの接続手順を説明する。"}},
            JevQuestion("この節は接続手順を説明しているか？"),
        )
    except Exception as exc:
        return f"jev     DOWN {type(exc).__name__}: {exc}", 1
    finally:
        reset_engine()
    return (f"jev     gguf {config.gguf_quant}/{config.gguf_many_mode} "
            f"p_yes={float(result.p_yes):.3f} {binary}"), 0


def cmd_check(args: argparse.Namespace) -> int:
    import requests

    settings = _settings(args)
    growi = str(settings.growi_url or os.environ.get("GROWI_URL", "")).rstrip("/")
    targets = {
        "chat": f"{settings.chat_base_url.rstrip('/')}/models",
        "embed": f"{settings.embed_base_url.rstrip('/')}/models",
        "parser": f"{settings.parser_base_url.rstrip('/')}/health" if settings.parser_base_url else "",
        "growi": f"{growi}/_api/v3/healthcheck" if growi else "",
    }
    bad = 0
    for name, url in targets.items():
        if not url:
            print(f"{name:7} skipped (not configured)")
            continue
        try:
            response = requests.get(url, timeout=5)
            print(f"{name:7} {response.status_code} {url}")
            bad += response.status_code >= 400
        except Exception as exc:
            print(f"{name:7} DOWN {url} ({type(exc).__name__})")
            bad += 1
    jev_line, jev_bad = _jev_check(settings)
    print(jev_line)
    bad += jev_bad
    project = open_project(settings)
    print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    return 1 if bad else 0


@capture_failure_logs()
@parser_skip_scope()  # TEMPORARY: parser rejections expire at the end of this sync.
def cmd_sync(args: argparse.Namespace) -> int:
    fast_repair = bool(getattr(args, "fast_repair", False))
    if getattr(args, "limit", None) is not None and not fast_repair:
        raise SystemExit("--limit is only valid with sync --repair")
    if (
        getattr(args, "fast_link", False)
        or getattr(args, "fast_link_reset", False)
        or fast_repair
    ):
        if getattr(args, "full", False):
            raise SystemExit("--link, --link-reset and --repair cannot be combined with --full")
        if fast_repair:
            return _cmd_fast_repair(args)
        if getattr(args, "items", None):
            raise SystemExit("--link and --link-reset operate on the whole generated wiki project")
        return _cmd_fast_link(args)

    from publisher import pipeline
    from publisher.queue import retry_failed, scan, work_once, worker_lock
    from publisher.progress import SyncProgress

    settings = _settings(args)
    requested_isolation = getattr(args, "isolated", None)
    isolated = (
        bool(getattr(settings, "sync_isolated", True))
        if requested_isolation is None
        else bool(requested_isolation)
    )
    if isolated:
        return _cmd_sync_isolated(args, settings)
    project = open_project(settings)
    fast_linking = (
        policy_of(settings).name == "fast" and getattr(settings, "wiki_linker_enabled", True)
    )
    combined: dict[str, Any] = {"done": [], "failures": []}
    progress = SyncProgress()

    def on_event(event: dict[str, Any]) -> None:
        progress.on_event(event)
        _progress(event)

    try:
        stale = pipeline.republish_if_stale(settings)
        if stale is not None:
            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
            combined["failures"].extend(stale["failures"])
        with worker_lock(project):
            with pipeline._lock(project):
                from publisher.history import checkpoint_legacy_fast_links

                checkpoint_legacy_fast_links(project)
            retry_failed(project)
            first = True
            resume = bool(getattr(args, "continue_run", False))
            while True:
                scan_result = scan(
                    settings,
                    only=args.items or None,
                    settle_seconds=0,
                    force=args.force and first,
                    verify_content=True,
                )
                progress.add_scan_result(scan_result)
                combined.setdefault("scan_failures", []).extend(
                    f"scan {rel}: {error}" for rel, error in scan_result.get("errors", {}).items()
                )
                first = False
                work_args = {"defer_linker": True} if fast_linking else {}
                result = work_once(settings, on_event=on_event, continue_run=resume, **work_args)
                # Only the first batch can resume a kept worktree; later batches
                # in the same process are always fresh.
                resume = False
                if result is None:
                    break
                paths = result.get("paths", [])
                progress.add_documents(paths)
                combined["done"].extend(result.get("done", []))
                combined["failures"].extend(_batch_failures(result))
                combined.setdefault("warnings", []).extend(result.get("index", {}).get("failures", []))
                if result.get("failures"):
                    break
                progress.mark_completed(paths)
            if fast_linking and not combined["failures"]:
                linked = _run_fast_link(
                    args, settings, worker_locked=True, on_progress=on_event,
                    only=_sync_link_scope(project, args.items),
                )
                combined["done"].extend(linked.get("done", []))
                combined["failures"].extend(linked.get("failures", []))
        if not combined["failures"]:
            # Index pages are derived output, so reconcile them against the whole wiki tree
            # here: a wiki built before its index exists catches up, and a page whose body
            # already matches GROWI is read but never rewritten.
            from publisher.index import build_index

            try:
                index = build_index(settings, on_progress=on_event)
            except Exception as exc:  # a stale table of contents must not fail a sync
                log.exception("インデックス処理に失敗")
                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
            combined["done"].extend(index["done"])
            combined["failures"].extend(index["failures"])
        return _report(combined, settings)
    finally:
        progress.close()


def _cmd_fast_link(args: argparse.Namespace) -> int:
    settings = _settings(args)
    return _report(_run_fast_link(args, settings), settings)


def _sync_link_scope(project: Any, items: list[str] | None) -> list[str] | None:
    if not items:
        return None
    from publisher.ledger import load_ledger

    wanted = {item.strip().lstrip("/") for item in items}
    ledger = load_ledger(project.metadata / "pipeline.json")
    return sorted({
        str(row["raw_rel"])
        for rel, row in ledger.sources.items()
        if (rel in wanted or str(row.get("mount_rel") or "") in wanted) and row.get("raw_rel")
    })


def _run_fast_link(
    args: argparse.Namespace, settings: Settings, *, worker_locked: bool = False,
    preflight: bool = True, on_progress: Any = None, only: list[str] | None = None,
) -> dict[str, Any]:
    """Keep analysis resumable; accept and publish only a complete fast-link pass."""
    import uuid
    from contextlib import nullcontext

    from graph.fast.inline_linker import (
        _checkpoint, _manifest, _manifest_path, _purge_extractions, link_project, reset_project,
    )
    from graph.wiki.storage import read_json, write_json_atomic
    from publisher import pipeline
    from publisher.history import (
        candidate, candidate_is_clean, checkpoint_legacy_fast_links, commit_candidate,
        last_good, promote, restore_last_good,
    )
    from publisher.ledger import load_ledger
    from publisher.queue import _candidate_settings, _finish_transaction, _transaction, recover, worker_lock

    project = open_project(settings)
    callback = on_progress if on_progress is not None else _progress
    resetting = bool(getattr(args, "fast_link_reset", False))
    result: dict[str, Any] = {"done": [], "failures": []}
    accepted = False
    try:
        with nullcontext() if worker_locked else worker_lock(project):
            if preflight:
                with pipeline._lock(project):
                    recover(project, settings)
                    checkpoint_legacy_fast_links(project)
            if preflight and not resetting:
                pulled = pipeline.pull_growi_once(settings)
                if pulled.get("failures"):
                    return pulled
            with pipeline._lock(project):
                base = last_good(project)
                if not candidate_is_clean(project, base):
                    raise RuntimeError("ファストリンク: ローカルの未確定の変更があります")
                operation = "fast-link-" + uuid.uuid4().hex
                with candidate(project, operation) as staged:
                    if resetting:
                        write_json_atomic(_manifest_path(staged), _manifest(project))
                        result = reset_project(_candidate_settings(settings, staged))
                    else:
                        result = link_project(
                            settings, force=bool(getattr(args, "force", False)),
                            continue_run=bool(getattr(args, "continue_run", False)),
                            on_progress=callback, output_project=staged, only=only,
                        )
                    if result.get("failures"):
                        result["done"] = []
                        return result
                    if not resetting:
                        # Indexes use this ready marker; a distinct mode lets --full
                        # recognize that its own linker still needs to run.
                        for doc in result.get("linked_documents", []):
                            marker = staged.wiki / doc / "_planning" / "linker.json"
                            write_json_atomic(marker, {
                                **read_json(marker, default={}),
                                "schema_version": 2, "status": "complete", "mode": "fast-inline",
                            })
                    if not candidate_is_clean(project, base):
                        raise RuntimeError("ファストリンク中にローカルのページが変更されました")
                    if not candidate_is_clean(staged, base):
                        commit = commit_candidate(staged, f"fast link {'reset' if resetting else 'complete'}",
                                                  {"base": base, "operation": operation})
                        _transaction(project, operation, base, "building", commit)
                        try:
                            promote(project, staged, commit)
                        except BaseException:
                            restore_last_good(project)
                            raise
                        _finish_transaction(project, operation)
                accepted = True
            if resetting:
                _purge_extractions(project)
                return result
            _checkpoint(project, "complete", documents=_manifest(project).get("documents", {}), failures=[])
            # Already-published documents are not rewritten. A failed publish can be retried
            # without redoing the accepted linker work.
            ledger = load_ledger(project.metadata / "pipeline.json")
            publish_scope: list[str] = []
            for doc in result.get("linked_documents", []):
                if ledger.published_documents.get(doc, {}).get("content_sha256") == pipeline._content_hash(project.wiki / doc):
                    continue
                raw = read_json(project.wiki / doc / "_planning" / "source.json", default={}).get("raw")
                if not raw:
                    raise RuntimeError(f"ファストリンク: ソースのマッピングがありません: {doc}")
                publish_scope.append(str(raw))
            if publish_scope:
                published = pipeline.publish_only(settings, only=publish_scope, allow_unlinked=True,
                                                   link_pending=False, captured=True, on_progress=callback)
                result["done"].extend(published.get("done", []))
                result["failures"].extend(published.get("failures", []))
                if published.get("failures"):
                    _checkpoint(project, "publish-failed", failures=published["failures"])
    except KeyboardInterrupt:
        if not accepted:
            result["done"] = []
        _checkpoint(project, "interrupted")
        result["failures"].append("ファストリンクを中断しました。メタデータを保存しました。sync --continue で再開できます")
    except Exception as exc:
        if not accepted:
            result["done"] = []
        log.exception("ファストリンクに失敗")
        result["failures"].append(f"fast link: {type(exc).__name__}: {exc}")
    return result


def _cmd_fast_repair(args: argparse.Namespace) -> int:
    """Repair existing fast pages, promoting and publishing one document at a time."""

    from contextlib import closing

    from graph.fast.repair import FastRepair
    from publisher.pipeline import publish_only, pull_growi_once
    from publisher.progress import SyncProgress
    from publisher.queue import worker_lock

    settings = _settings(args)
    project = open_project(settings)
    limit = getattr(args, "limit", None)
    if limit is not None and int(limit) <= 0:
        raise SystemExit("--limit must be a positive integer")
    combined: dict[str, Any] = {"done": [], "failures": []}
    with closing(SyncProgress()) as progress, worker_lock(project):
        continuing = bool(getattr(args, "continue_run", False))
        try:
            resume_dirty, resume_page_states, resume_versions = (
                _interrupted_fast_repair(project)
                if continuing else (False, frozenset(), frozenset())
            )
        except Exception as exc:
            log.exception("fast repair resume inspection failed")
            combined["failures"].append(
                f"fast repair resume inspection: {type(exc).__name__}: {exc}"
            )
            return _report(combined, settings)
        if resume_dirty and not resume_page_states:
            combined["failures"].append(
                "fast repair setup: interrupted repair checkpoints are unreadable; "
                "refusing to restart completed pages"
            )
            return _report(combined, settings)

        def on_event(event: dict[str, Any]) -> None:
            progress.on_event(event)
            _progress(event)

        try:
            repair = FastRepair(
                settings,
                project=project,
                on_progress=on_event,
                resume_page_states=resume_page_states,
            )
            targets = repair.targets(getattr(args, "items", None) or None)
        except Exception as exc:
            log.exception("ファストリペアのセットアップに失敗")
            combined["failures"].append(f"fast repair setup: {type(exc).__name__}: {exc}")
            return _report(combined, settings)

        if not targets:
            log.info("fast repair project=%s has no eligible documents", settings.target_name)
            return _report(combined, settings)

        if limit is None:
            progress.add_documents(targets)
        log.info(
            "fast repair project=%s documents=%d page_limit=%s concurrency=%d",
            settings.target_name,
            len(targets),
            limit if limit is not None else "all",
            repair.concurrency,
        )
        has_growi = bool(str(getattr(settings, "growi_url", "") or "").strip())
        if has_growi:
            if resume_dirty:
                log.warning(
                    "continuing interrupted fast repair versions=%s checkpoints=%d "
                    "without a preflight GROWI pull; accepted pages will be reused",
                    ",".join(sorted(resume_versions)),
                    len(resume_page_states),
                )
            else:
                log.info("checking GROWI edits before repair")
                try:
                    pulled = pull_growi_once(settings)
                    combined["done"].extend(pulled.get("done", []))
                    combined["failures"].extend(pulled.get("failures", []))
                except Exception as exc:
                    log.exception("fast repair GROWI preflight failed")
                    combined["failures"].append(
                        f"fast repair preflight: {type(exc).__name__}: {exc}"
                    )
                    return _report(combined, settings)
                # A page that failed the pull ("<document>/<page>.md: reason") holds back only its own
                # document; anything else (GROWI unreachable, inventory problems) stops the repair.
                if not all(f.split(": ", 1)[0].endswith(".md") for f in combined["failures"]):
                    return _report(combined, settings)
                held = {f.split(": ", 1)[0].rpartition("/")[0] for f in combined["failures"]}
                targets = [t for t in targets if project.wiki_dir(t).relative_to(project.wiki).as_posix() not in held]

        remaining = int(limit) if limit is not None else None
        for raw_rel in targets:
            if remaining is not None and remaining <= 0:
                break
            try:
                progress.add_documents([raw_rel])
                log.info("repairing document path=%s", raw_rel)
                result = repair.repair_document(
                    raw_rel,
                    limit=remaining,
                    force=bool(getattr(args, "force", False)),
                )
                combined["done"].append({"path": raw_rel, **result})
                repair_failures = list(result.get("failures", []))
                combined["failures"].extend(repair_failures)
                if remaining is not None:
                    remaining -= int(result.get("examined", 0))
                if not result.get("promoted"):
                    if not repair_failures:
                        progress.mark_completed([raw_rel])
                    continue
                if has_growi:
                    log.info("publishing repaired document path=%s", raw_rel)
                    published = publish_only(
                        settings,
                        only=[raw_rel],
                        allow_unlinked=True,
                        link_pending=False,
                    )
                    combined["done"].extend(published.get("done", []))
                    combined["failures"].extend(published.get("failures", []))
                else:
                    from publisher.index import build_index

                    indexed = build_index(
                        settings,
                        only=[raw_rel],
                        publish=False,
                        on_progress=on_event,
                    )
                    combined["done"].extend(indexed.get("done", []))
                    combined["failures"].extend(indexed.get("failures", []))
                # Failures are accumulated for the final exit status, but a
                # publish/index failure for one document must not prevent the
                # remaining repair targets from being examined.
                document_failures = (
                    repair_failures
                    + (published.get("failures", []) if has_growi else indexed.get("failures", []))
                )
                if document_failures:
                    continue
                progress.mark_completed([raw_rel])
            except Exception as exc:
                log.exception("fast repair failed for %s", raw_rel)
                combined["failures"].append(
                    f"{raw_rel}: {type(exc).__name__}: {exc}"
                )
                # Keep the failed document resumable and continue with the
                # next target.  --continue can revisit it after the rest of
                # the project has been processed.
                continue
    return _report(combined, settings)


def _cmd_sync_isolated(args: argparse.Namespace, settings: Settings) -> int:
    """One retry owner; each operation starts from the latest accepted main."""
    from publisher import pipeline
    from publisher.ahead import LinkAhead, ParseAhead, PlannerAhead
    from publisher.queue import queued_jobs, recover, retry_failed, scan, status, work_once, worker_lock
    from publisher.progress import SyncProgress

    project = open_project(settings)
    fast_linking = policy_of(settings).name == "fast"
    parse_ahead: ParseAhead | None = None
    planner_ahead: PlannerAhead | None = None
    link_ahead: LinkAhead | None = None
    combined: dict[str, Any] = {"done": [], "failures": []}
    progress = SyncProgress()
    wanted = {item.strip().lstrip("/") for item in args.items or ()}
    discovery: dict[str, str] = {}
    recovered: list[str] = []
    skipped_versions: dict[str, int] = {}
    blocked = False

    def selected(row: dict[str, Any]) -> bool:
        return not wanted or row["rel"] in wanted or row.get("from_rel") in wanted

    def link_scope() -> list[str] | None:
        """Raw paths of the selected sources (None: every document)."""
        return _sync_link_scope(project, args.items)

    def on_event(event: dict[str, Any]) -> None:
        if (
            parse_ahead is not None
            and event.get("stage") == "queue-claim"
            and event.get("lane") == "slow"
        ):
            parse_ahead.advance()
        progress.on_event(event)
        _progress(event)

    try:
        with worker_lock(project):
            # Uncertain remote state must be examined before pruning, scanning,
            # retrying jobs, or issuing any unrelated publication.
            recover(project, settings)
            with pipeline._lock(project):
                from publisher.history import checkpoint_legacy_fast_links

                checkpoint_legacy_fast_links(project)
            stale = pipeline.republish_if_stale(settings)
            if stale is not None and stale.get("failures"):
                combined["failures"].extend(stale["failures"])
                return _report(combined, settings)
            if stale is None:
                # Capture GROWI edits before any scan or build, so a UI-only edit is taken even
                # when no source changed. A blocked page is reported; its document is blocked
                # again by its own capture before it is published.
                pulled = pipeline.pull_growi_once(settings)
                combined["failures"].extend(pulled.get("failures", []))
            retry_failed(project, only=args.items or None)
            scanned = scan(
                settings,
                only=args.items or None,
                settle_seconds=0,
                force=args.force,
                verify_content=True,
            )
            progress.add_scan_result(scanned)
            progress.add_documents(
                row["rel"] for row in status(project)
                if selected(row) and row["status"] in {"queued", "running", "failed"}
            )
            discovery = {
                rel: error for rel, error in scanned.get("errors", {}).items()
                if not wanted or rel in wanted or rel == "." or any(
                    item.startswith(rel.rstrip("/") + "/") for item in wanted
                )
            }
            for rel, error in discovery.items():
                log.error("source discovery failed: %s: %s", rel, error)
            # Parse, plan and build form one bounded train; every ahead worker only
            # fills a content-addressed cache (publisher/ahead.py).
            settings.cache_dir = getattr(settings, "cache_dir", "") or str((project.metadata / "cache").resolve())
            if str(getattr(settings, "ingest_mode", "wiki")) == "wiki":
                planner_ahead = PlannerAhead(project, settings)
            parse_ahead = ParseAhead(
                project,
                settings,
                queued_jobs(project, only=args.items or None),
                on_ready=planner_ahead.add if planner_ahead is not None else None,
            )
            if getattr(settings, "wiki_linker_enabled", True) and not fast_linking:
                link_ahead = LinkAhead(project, settings)
                # Built earlier but not linked yet (e.g. a stopped run): these go first.
                link_ahead.add(project, pipeline._pending_link_rels(project, settings, link_scope()))
            attempt = 1
            while True:
                result = work_once(settings, on_event=on_event, isolated=True,
                                   only=args.items or None,
                                   exclude=list(discovery), defer_linker=True)
                if result is None:
                    if attempt == 2:
                        break
                    # Snapshot exact current versions once. No scan or worker
                    # loop below automatically requeues failed final attempts.
                    targets = {row["rel"]: row["version"] for row in status(project)
                               if selected(row) and row["status"] == "failed"
                               and skipped_versions.get(row["rel"]) != row["version"]}
                    retry_failed(project, only=args.items or None, versions=targets)
                    attempt = 2
                    log.info("first pass complete; final retry documents=%d discovery=%d", len(targets), len(discovery))
                    continue
                paths = result.get("paths", [])
                skipped_paths = set(result.get("parser_skips", []))
                skipped_versions.update({row["rel"]: row["version"] for row in result.get("job_versions", [])
                                         if row["rel"] in skipped_paths})
                progress.add_documents(paths)
                if result.get("recovery_required") or result.get("cancelled"):
                    blocked = True
                    combined["failures"].extend(result.get("failures") or ["sync cancelled; pending jobs retained"])
                    break
                if result.get("failures"):
                    if skipped_paths and set(paths) <= skipped_paths:
                        log.warning("ドキュメントをスキップ: %s", paths)
                    else:
                        log.warning("document attempt %d failed: %s", attempt, paths)
                    continue
                combined["done"].extend(result.get("done", []))
                progress.mark_completed(paths)
                if link_ahead is not None and result.get("lane") == "slow":
                    link_ahead.add(project, [str(job["raw_rel"]) for job in result.get("job_versions", [])])
                if attempt == 2:
                    recovered.extend(paths)
            parse_ahead.stop()
            if planner_ahead is not None:
                planner_ahead.close()
            if link_ahead is not None:
                link_ahead.close(wait=not blocked)
            if not blocked and getattr(settings, "wiki_linker_enabled", True):
                if fast_linking:
                    linked = _run_fast_link(
                        args, settings, worker_locked=True, preflight=False,
                        on_progress=on_event, only=link_scope(),
                    )
                else:
                    linked = pipeline.link_pending_isolated(
                        settings,
                        only=link_scope(),
                        on_progress=on_event,
                    )
                combined["done"].extend(linked.get("done", []))
                combined["failures"].extend(linked.get("failures", []))
                failed_links = list(linked.get("failed_documents", []))
                if failed_links:
                    # Linking is attempted first and remains pending for the next sync,
                    # but a linker outage must not withhold successfully built content.
                    log.info("リンク未完了のドキュメントを先に公開します: %d件", len(failed_links))
                    published = pipeline.publish_only(
                        settings,
                        only=failed_links,
                        allow_unlinked=True,
                        link_pending=False,
                    )
                    combined["done"].extend(published.get("done", []))
                    combined["failures"].extend(published.get("failures", []))
            elif not blocked and not policy_of(settings).link:
                # A policy without linking still publishes what it built.
                published = pipeline.publish_only(settings, only=link_scope(), allow_unlinked=True, link_pending=False)
                combined["done"].extend(published.get("done", []))
                combined["failures"].extend(published.get("failures", []))
            pending = [row for row in status(project) if selected(row)]
            for row in pending:
                combined["failures"].append(f"{row['rel']}: {row.get('error') or 'pending dependent work'}")
            for rel, error in discovery.items():
                combined["failures"].append(f"scan {rel}: {error}")
            if not blocked:
                from publisher.index import build_index

                try:
                    index = build_index(settings, on_progress=on_event)
                    combined["done"].extend(index.get("done", []))
                    combined["failures"].extend(index.get("failures", []))
                except Exception as exc:
                    log.exception("インデックス処理に失敗")
                    combined["failures"].append(f"index: {type(exc).__name__}: {exc}")
        if recovered:
            print(json.dumps({"recovered_on_final_retry": recovered}, ensure_ascii=False))
        return _report(combined, settings)
    except Exception as exc:
        log.exception("シンク処理に失敗")
        combined["failures"].append(f"shared sync failure: {type(exc).__name__}: {exc}")
        return _report(combined, settings)
    finally:
        if parse_ahead is not None:
            parse_ahead.stop()
        if planner_ahead is not None:
            planner_ahead.close()
        if link_ahead is not None:
            link_ahead.close(wait=False)
        progress.close()


@capture_failure_logs()
def cmd_pull(args: argparse.Namespace) -> int:
    from publisher.pipeline import pull_growi_once

    settings = _settings(args)
    result = (
        pull_growi_once(settings, force_inventory=True)
        if getattr(args, "inventory", False)
        else pull_growi_once(settings)
    )
    if result.get("human_sync"):
        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    return _report(result, settings)


def cmd_human(args: argparse.Namespace) -> int:
    from publisher.human_changes import HumanStore
    from publisher.history import candidate_is_clean, checkpoint_live, ensure_repository
    from publisher.pipeline import _lock

    settings = _settings(args)
    project = open_project(settings)
    with _lock(project):
        baseline = ensure_repository(project)
        if not candidate_is_clean(project, baseline):
            raise RuntimeError("cannot modify human state from a dirty last-good working tree")
        store = HumanStore(project)
        if args.human_command == "status":
            result = store.status()
            code = 0
        elif args.human_command == "live-plan":
            from publisher.live_verification import LiveVerificationReport

            report = LiveVerificationReport.create(project, settings, args.path)
            result = {
                "report": str(report.path),
                "resolved_boundary": report.data["disposable_path"],
                "endpoint": report.data["endpoint"],
                "confirmation_code": report.data["confirmation_code"],
                "status": report.data["status"],
            }
            code = 0
        else:
            raise ValueError(f"unknown human command: {args.human_command}")
        store.audit()
        checkpoint_live(project, f"human {args.human_command}")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return code


def cmd_watch(args: argparse.Namespace) -> int:
    from publisher.queue import serve

    settings = _settings(args)
    try:
        serve(
            settings,
            only=args.items or None,
            interval=args.interval,
            growi_interval=args.growi_interval,
            force=args.force,
            on_event=_progress,
            continue_run=bool(getattr(args, "continue_run", False)),
        )
    except KeyboardInterrupt:
        return 0


def cmd_queue(args: argparse.Namespace) -> int:
    from publisher.pipeline import _lock
    from publisher.queue import recover, resumable_operation_id, retry_failed, scan, status, work_once, worker_lock

    settings = _settings(args)
    project = open_project(settings)
    if args.queue_command == "scan":
        result = scan(settings, only=args.items or None, settle_seconds=args.settle, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        _report({"failures": [f"scan {rel}: {error}" for rel, error in result.get("errors", {}).items()]}, settings)
        return 0
    if args.queue_command == "status":
        for row in status(project):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.queue_command == "retry":
        print(json.dumps({"retried": retry_failed(project)}))
        return 0
    resume = bool(getattr(args, "continue_run", False))
    with worker_lock(project):
        with _lock(project):
            preserved = resumable_operation_id(project, adopt_orphan=True) if resume else None
            recover(project, settings, preserve_operation_id=preserved)
        while True:
            result = work_once(settings, continue_run=resume)
            # Only the first batch can resume a kept worktree.
            resume = False
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if args.once:
                index_failures = (result or {}).get("index", {}).get("failures", [])
                _report({"failures": _batch_failures(result or {}), "warnings": index_failures}, settings)
                return 1 if result and (result.get("failures") or index_failures) else 0
            if result is None:
                time.sleep(0.5)


@capture_failure_logs()
def cmd_convert(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from runner.steps import convert

    result = convert(settings, on_progress=_progress)
    print(json.dumps({key: value for key, value in result.items() if key != "failures"}, ensure_ascii=False))
    _report({"failures": result.get("failures") or result["failed"]}, settings)
    return 1 if result["failed"] else 0


@capture_failure_logs()
def cmd_build(args: argparse.Namespace) -> int:
    items = list(args.items)
    phase = items.pop(0) if items and items[0] in {"wiki", "link", "all"} else "all"
    rels = items
    if args.from_file:
        rels += [line.strip() for line in Path(args.from_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    from runner.steps import build

    settings = _settings(args)
    return _report(build(
        settings, phase=phase, only=rels or None, force=args.force,
        on_progress=_progress,
    ), settings)


@capture_failure_logs()
def cmd_publish(args: argparse.Namespace) -> int:
    from contextlib import closing

    allow_unlinked = True
    from publisher.phase import Config, Input, run
    from publisher.progress import SyncProgress

    settings = _settings(args)
    root = Path(settings.data_root) / settings.target_name
    stats: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "")
        step = str(event.get("step") or "")
        if stage == "growi-publish" and step in {"start", "done", "batch_done"}:
            stats.update({key: event[key] for key in ("documents", "pages") if key in event})
        elif stage == "index" and step in {"progress", "done"}:
            if "total" in event:
                stats["indexes"] = event["total"]
            if step == "done":
                stats["indexed"] = event.get("current", 0)
        progress.on_event(event)
        _progress(event)

    log.info("publish project=%s allow_unlinked=True; linking skipped", settings.target_name)
    with closing(SyncProgress()) as progress:
        result = run(
            Config(
                action="publish",
                publish=True,
                settings=settings,
                allow_unlinked=allow_unlinked,
                link_pending=not allow_unlinked,
            ),
            Input(root),
            root,
            on_progress=on_event,
        )
    log.info(
        "publish completed documents=%s pages=%s indexes=%s failures=%d",
        stats.get("documents", 0),
        stats.get("pages", 0),
        stats.get("indexed", stats.get("indexes", 0)),
        len(result.failures),
    )
    return _report({"done": list(result.pages), "failures": list(result.failures)}, settings)


@capture_failure_logs()
def cmd_index(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.delete:
        from publisher.index import delete_index_pages

        return _report(delete_index_pages(settings), settings)
    from runner.steps import index

    return _report(index(
        settings, only=args.items or None, publish=not args.no_publish,
        on_progress=_progress,
    ), settings)


@capture_failure_logs()
def cmd_reset(args: argparse.Namespace) -> int:
    from contextlib import closing

    from publisher.pipeline import reset_growi
    from publisher.progress import SyncProgress

    settings = _settings(args)
    with closing(SyncProgress()) as progress:
        def on_event(event: dict[str, Any]) -> None:
            progress.on_event(event)
            _progress(event)

        result = reset_growi(settings, on_progress=on_event)
    return _report(result, settings)


@capture_failure_logs()
def cmd_link(args: argparse.Namespace) -> int:
    from graph.linker import __main__ as linker_cli

    settings = _settings(args)
    project = open_project(settings)
    if args.link_command is None:
        from publisher.pipeline import link_raw
        return _report(link_raw(settings, force=args.force, on_progress=_progress), settings)
    if args.link_command == "status":
        import sqlite3

        if not project.linker_database.exists():
            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0}); return 0
        with sqlite3.connect(project.linker_database) as conn:
            mode = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("documents", "chunks", "edges")}
        print({"mode": mode[0] if mode else None, **counts})
    elif args.link_command == "rebuild":
        linker_cli.rebuild(project, settings, args.mode, args.no_edges)
    else:
        from graph.common.async_tools import run_async_blocking
        from graph.linker import link_document

        rel = args.document.strip("/")
        if rel in linker_cli._documents(project):
            rel = linker_cli._raw_rel(project, rel)
        elif not project.raw_file(rel).exists():
            raise SystemExit(f"unknown document: {args.document} (wiki folder or raw rel path)")
        model, embedder = linker_cli._model_and_embedder(settings, project, Path(rel).parent.as_posix())
        result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=_progress))
        print(json.dumps({"touched": result.touched_documents}, ensure_ascii=False))
        if result.touched_documents:
            print("更新したドキュメントをGROWIへ反映するには `python main.py publish` を実行してください", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=False, help="config name from configs/ or absolute INI path")
    parser.add_argument("--data-root", help="override the selected INI data_root")
    parser.add_argument("-v", "--verbose", action="store_true", help="print pipeline progress events")
    sub = parser.add_subparsers(dest="command", required=True)
    def project_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project", default=argparse.SUPPRESS, help="config name from configs/ or absolute INI path")
        p.add_argument("--data-root", default=argparse.SUPPRESS, help="override selected INI data_root (may also go before the command)")

    check = sub.add_parser("check", help="ping chat/embed/parser/GROWI endpoints and the local Jev backend"); project_flags(check); check.set_defaults(fn=cmd_check)

    def pipeline_flags(p: argparse.ArgumentParser) -> None:
        project_flags(p)
        p.add_argument("--mode", choices=("wiki", "chunks"), help="ingest mode (default WIKI_INGEST_MODE)")
        p.add_argument("--linker", choices=("legacy", "neo", "off"), help="linker mode (default WIKI_LINKER_MODE)")
        p.add_argument("--timeout", type=int, help="per-call model timeout in seconds (default WIKI_REQUEST_TIMEOUT)")

    convert = sub.add_parser("convert", help="convert the configured mount to raw Markdown only"); project_flags(convert); convert.set_defaults(fn=cmd_convert)
    sync = sub.add_parser("sync", help="scan and drain the configured project's durable queue"); pipeline_flags(sync)
    sync.description = "Drain the queue, then reconcile index pages for the whole wiki tree."
    sync.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    sync.add_argument("--force", action="store_true", help="regenerate unchanged sources; with --link, relink completed documents too")
    policy_flags = sync.add_mutually_exclusive_group()
    policy_flags.add_argument("--fast", action="store_true", help="use fast generation and linking (the default)")
    policy_flags.add_argument("--full", action="store_true", help="explicitly run standard generation, research and linking")
    fast_operation = sync.add_mutually_exclusive_group()
    fast_operation.add_argument(
        "--link", dest="fast_link", action="store_true",
        help="run only the same fast linker used by normal sync, then publish on completion",
    )
    fast_operation.add_argument(
        "--link-reset", dest="fast_link_reset", action="store_true",
        help="remove recorded fast inline links locally and checkpoint the reset",
    )
    fast_operation.add_argument(
        "--repair", dest="fast_repair", action="store_true",
        help="judge and repair existing fast wiki pages",
    )
    sync.add_argument(
        "--limit", type=int,
        help="with --repair, examine at most this many pages",
    )
    sync.add_argument("--skip-excel-and-small", action="store_true",
                      help="keep tabular source pages without extra analyses; rewrite documents under 10,000 readable characters as one page without planning")
    sync.add_argument(
        "--isolated", action=argparse.BooleanOptionalAction, default=None,
        help="build sources one candidate at a time with a final retry pass (default: enabled; use --no-isolated for legacy batch generation)",
    )
    sync.add_argument("--continue", dest="continue_run", action="store_true",
                      help="resume saved work, including matching fast-linker page/Jev checkpoints")
    sync.set_defaults(fn=cmd_sync)
    watch = sub.add_parser("watch", help="run the metadata scanner and persistent queue worker"); pipeline_flags(watch)
    watch.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    watch.add_argument("--force", action="store_true", help="queue selected sources once at startup even when unchanged")
    watch.add_argument("--continue", dest="continue_run", action="store_true",
                       help="resume the previous run's kept candidate worktree instead of discarding it")
    watch.add_argument("--interval", type=float, default=float(os.environ.get("PUBLISHER_INTERVAL_SECONDS", "86400")), help="seconds between mount scans (default 86400 = once a day)")
    watch.add_argument("--growi-interval", type=float, default=float(os.environ.get("GROWI_WATCH_INTERVAL_SECONDS", "300")), help="seconds between low-priority GROWI revision checks; 0 disables")
    watch.set_defaults(fn=cmd_watch)

    queue = sub.add_parser("queue", help="inspect or operate the persistent watcher queue"); project_flags(queue)
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_scan = queue_sub.add_parser("scan", help="run one cheap mount metadata scan")
    project_flags(queue_scan)
    queue_scan.add_argument("items", nargs="*", metavar="mount-rel")
    queue_scan.add_argument("--settle", type=float, default=10.0, help="seconds a changed file must remain unchanged before work starts")
    queue_scan.add_argument("--force", action="store_true", help="queue selected sources even when metadata is unchanged")
    queue_work = queue_sub.add_parser("work", help="process queued work, fast deletes before slow batches")
    project_flags(queue_work)
    queue_work.add_argument("--once", action="store_true", help="process at most one queue batch and exit")
    queue_work.add_argument("--continue", dest="continue_run", action="store_true",
                            help="resume the previous run's kept candidate worktree instead of discarding it")
    queue_status = queue_sub.add_parser("status", help="list pending, active, and failed work"); project_flags(queue_status)
    queue_retry = queue_sub.add_parser("retry", help="requeue failed work"); project_flags(queue_retry)
    queue.set_defaults(fn=cmd_queue)
    build = sub.add_parser("build", aliases=("wiki",), help="run wiki, link, or all local build phases"); pipeline_flags(build)
    build.add_argument("items", nargs="*", metavar="[wiki|link|all] [raw-rel ...]", help="phase followed by raw-relative paths; omit phase for all")
    build.add_argument("--from-file", help="text file with one raw-relative path per line")
    build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
    build.set_defaults(fn=cmd_build)
    publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish)
    publish.add_argument(
        "--allow-unlinked",
        action="store_true",
        default=True,
        help="compatibility flag: publish always allows unlinked wiki pages and skips linking",
    )
    publish.set_defaults(fn=cmd_publish)
    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    pull.set_defaults(fn=cmd_pull)
    human = sub.add_parser("human", help="inspect human edits (resolve a conflict by editing the page in GROWI)"); project_flags(human)
    human_sub = human.add_subparsers(dest="human_command", required=True)
    human_status = human_sub.add_parser("status", help="print blocked pages, documents with human information, Appendix entries and guidance")
    project_flags(human_status)
    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    project_flags(human_live)
    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    human.set_defaults(fn=cmd_human)
    index = sub.add_parser("index", help="publish per-document + root index pages for growi-search"); project_flags(index)
    index.add_argument("items", nargs="*", metavar="raw-rel", help="raw-relative paths; omit for every linked document")
    index.add_argument("--no-publish", action="store_true", help="only write metadata/index/, do not touch GROWI")
    index.add_argument("--delete", action="store_true", help="remove index pages from GROWI")
    index.set_defaults(fn=cmd_index)
    reset = sub.add_parser("reset", help="trash publisher-owned GROWI pages and reset publish state"); project_flags(reset); reset.set_defaults(fn=cmd_reset)

    link = sub.add_parser("link", help="cross-document linker")
    project_flags(link)
    link.add_argument("--linker", choices=("legacy", "neo"), help="mode for relink (default WIKI_LINKER_MODE)")
    link.add_argument("--force", action="store_true", help="relink all wiki documents, including completed ones")
    link_sub = link.add_subparsers(dest="link_command")
    link_sub.add_parser("status", help="catalog mode and counts")
    relink = link_sub.add_parser("relink", help="(re)link one document; free when nothing changed")
    relink.add_argument("document", help="wiki folder (team/docs/Input1.docx) or raw rel path")
    rebuild = link_sub.add_parser("rebuild", help="drop the catalog and relink everything (re-pays edge calls)")
    rebuild.add_argument("--mode", choices=("legacy", "neo"), required=True)
    rebuild.add_argument("--no-edges", action="store_true", help="strip footers/inline links, keep metadata")
    link.set_defaults(fn=cmd_link)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from publisher.progress import JapaneseFormatter, ShortNameFormatter, TqdmStreamHandler

    settings = _settings(args)
    with capture_run_logs(settings) as log_dir:
        handler = TqdmStreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
        handler.setFormatter(ShortNameFormatter(LOG_FORMAT) if args.verbose else JapaneseFormatter())
        debug_handler = logging.FileHandler(log_dir / "debug.log", mode="w", encoding="utf-8")
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.DEBUG, handlers=[handler, debug_handler], force=True)
        # -v means "show our debug", not third-party HTTP/client chatter.
        # Keep client warnings out of the progress display as well; real errors
        # from these clients remain visible.
        for noisy in (
            "asyncio",
            "httpx",
            "httpx2",
            "httpcore",
            "h11",
            "urllib3",
            "openai",
            "langchain",
            "langchain_openai",
            "langchain-openai",
            "anthropic",
        ):
            logging.getLogger(noisy).setLevel(logging.ERROR)
        try:
            # Daemons have no final report; do not accumulate their diagnostics forever.
            if args.command == "watch" or (args.command == "queue" and args.queue_command == "work" and not args.once):
                return int(args.fn(args) or 0)
            with capture_failure_logs():
                return int(args.fn(args) or 0)
        finally:
            root = logging.getLogger()
            for configured in (handler, debug_handler):
                root.removeHandler(configured)
                configured.close()


if __name__ == "__main__":
    raise SystemExit(main())
