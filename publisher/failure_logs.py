"""Failure evidence lives outside disposable candidate worktrees."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from graph import config
from graph.wiki.storage import write_json_atomic
from graph.workspace.parser_client import safe_endpoint


def _redact(text: str, settings: Any) -> str:
    for name in ("growi_token", "chat_api_key", "embed_api_key"):
        secret = str(getattr(settings, name, "") or "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s\"'<>]+", lambda m: safe_endpoint(m.group()), text)


def _log_root(settings: Any) -> Path:
    return Path(getattr(settings, "failure_log_root", "") or ((config.PROJECT_ROOT or Path.cwd()) / "logs"))


class _FailureCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.tracebacks: list[tuple[str, str, str, list[str]]] = []
        self.warnings: list[str] = []
        self.pending: tuple[Any, list[str], list[str]] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        warning = getattr(record, "failure_summary", "")
        if warning:
            self.warnings.append(str(warning))
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            args = record.args.values() if isinstance(record.args, dict) else (record.args or ())
            self.tracebacks.append((record.getMessage(), f"{type(exc).__name__}: {exc}",
                                    "".join(traceback.format_exception(*record.exc_info)),
                                    [arg for arg in args if isinstance(arg, str)]))


_capture: ContextVar[_FailureCapture | None] = ContextVar("failure_capture", default=None)


@contextmanager
def capture_failure_logs():
    """Collect diagnostics for one CLI command; print after progress bars close."""
    if _capture.get() is not None:
        yield
        return
    handler = _FailureCapture()
    token = _capture.set(handler)
    logging.getLogger().addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(handler)
        _capture.reset(token)
        handler.close()
        if handler.pending is not None:
            settings, failures, warnings = handler.pending
            _write_summary(settings, failures, warnings + handler.warnings, handler.tracebacks)


def report_failure_summary(settings: Any, failures: list[str], warnings: list[str] | None = None) -> None:
    handler = _capture.get()
    if handler is not None:
        handler.pending = (settings, list(failures), list(warnings or []))
    else:
        _write_summary(settings, failures, warnings or [], [])


def _mount_paths(settings: Any) -> dict[str, str]:
    """Use existing metadata, never walk a potentially huge source mount."""
    from common.paths import generated_rels

    root = Path(settings.data_root) / settings.target_name / "metadata"
    aliases: dict[str, str] = {}
    for filename, key in (("pipeline.json", "sources"), ("convert.json", None)):
        try:
            data = json.loads((root / filename).read_text(encoding="utf-8"))
            sources = data.get(key, {}) if key else data
            for rel, row in sources.items():
                mount = str(row.get("mount_rel") or rel)
                raw, document = generated_rels(mount)
                aliases[mount] = aliases[raw] = aliases[document] = mount
                if row.get("raw_rel") or row.get("raw"):
                    aliases[str(row.get("raw_rel") or row["raw"])] = mount
            if key:
                for local, row in data.get("published_pages", {}).items():
                    document = local.rpartition("/")[0]
                    if document in aliases and row.get("growi_path"):
                        aliases[str(row["growi_path"])] = aliases[document]
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # Missing/broken metadata must not hide the original error.
    return aliases


def _write_summary(settings: Any, failures: list[str], warnings: list[str],
                   traces: list[tuple[str, str, str, list[str]]]) -> None:
    failures = list(dict.fromkeys(failures))
    warnings = list(dict.fromkeys(warnings))
    entries = failures + warnings
    aliases = _mount_paths(settings) if entries else {}
    lines = [f"プロジェクト: {settings.target_name}"]
    used_traces: set[str] = set()
    for title, rows in (("失敗ドキュメント", failures), ("インデックスのエラー（パブリッシュは継続）", warnings)):
        if not rows:
            continue
        lines.extend(["", f"{title}: {len(rows)}件"])
        for failure in rows:
            path = failure.split(": ", 1)[0].removeprefix("scan ")
            for prefix in ("index: ", "inventory: "):
                if failure.startswith(prefix):
                    path = failure[len(prefix):].split(": ", 1)[0]
            mount = aliases.get(path) or aliases.get(path.rpartition("/")[0])
            label = f"マウント内パス: {mount}" if mount else f"パス／ステージ: {path}"
            lines.extend(["", f"  {label}", f"  エラー詳細（原文）: {failure}"])
            for message, error, trace, context in traces:
                # Retried errors that recovered are deliberately omitted.
                documents = {aliases.get(arg) or aliases.get(arg.rpartition("/")[0]) for arg in context} - {None}
                if documents and mount and mount not in documents:
                    continue
                reason = error.partition(": ")[2]
                if trace not in used_traces and (error[:500] in failure or (path and path in message and reason and reason[:200] in failure)):
                    used_traces.add(trace)
                    lines.extend(["  トレースバック（原文）:", trace.rstrip()])
    body = _redact("\n".join(lines) + "\n", settings) if entries else ""
    path = _log_root(settings) / f"{settings.target_name}_failed_docs.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")  # Also clear the previous run after success.
    except OSError as exc:
        print(_redact(f"エラーログの保存に失敗: {path}: {type(exc).__name__}: {exc}", settings), file=sys.stderr)
    if body:
        print(body, file=sys.stderr, end="", flush=True)
    print(_redact(f"失敗ドキュメント: {len(failures)}件、インデックスのエラー: {len(warnings)}件。ログ: {path}", settings),
          file=sys.stderr, flush=True)


def save_failure(settings: Any, project: Any, run_id: str, attempt: int,
                 rel: str, details: dict[str, Any], transcript: str = "") -> str:
    root = _log_root(settings)
    identity = str(details.get("source_id") or rel)
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    directory = root / "sync" / project.root.name / run_id / key
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"attempt-{attempt}-{uuid.uuid4().hex[:8]}"
    # Redact both exception text and structured evidence, including URLs.
    body = json.dumps({"path": rel, "attempt": attempt, "recorded_at": time.time(), **details},
                      ensure_ascii=False, default=str)
    write_json_atomic(directory / f"{stem}.json", json.loads(_redact(body, settings)))
    (directory / f"{stem}.log").write_text(_redact(transcript, settings), encoding="utf-8")
    return str(directory / f"{stem}.json")


class AttemptLog:
    def __init__(self, settings: Any, project: Any, run_id: str, attempt: int):
        self.settings, self.project = settings, project
        self.run_id = run_id or ("sync-" + uuid.uuid4().hex)
        self.attempt = attempt
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.events: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def __enter__(self):
        logging.getLogger().addHandler(self.handler)
        return self

    def event(self, row: dict[str, Any]) -> None:
        self.events.append(dict(row))

    def save(self, result: dict[str, Any]) -> list[str]:
        jobs = result.get("job_versions") or [{"rel": rel} for rel in result.get("paths", ["<shared>"])]
        return [save_failure(
            self.settings, self.project, self.run_id, self.attempt, job["rel"],
            {**job, "result": result, "events": self.events,
             "elapsed_seconds": round(time.monotonic() - self.started, 2),
             "accepted_base": job.get("base_commit", ""),
             "recovery": "required" if result.get("recovery_required") else "settled; accepted main retained"},
            self.stream.getvalue(),
        ) for job in jobs]

    def __exit__(self, kind, value, tb):
        try:
            if value is not None:
                self.save({"failures": ["".join(traceback.format_exception(kind, value, tb))],
                           "recovery_required": True})
        finally:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()

