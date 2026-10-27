"""Failure evidence lives outside disposable candidate worktrees."""

from __future__ import annotations

import json
import datetime
import logging
import os
import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from graph import config
from graph.workspace.parser_client import safe_endpoint

log = logging.getLogger(__name__)


def redact_log_text(text: str, settings: Any) -> str:
    """Keep credentials and endpoint query values out of every log and summary."""
    secrets = {
        str(getattr(settings, name, "") or "")
        for name in ("growi_token", "chat_api_key", "embed_api_key", "writer_api_key",
                     "judge_api_key", "wiki_jev_api_key", "jev_api_key")
    }
    secrets.update(value for name, value in os.environ.items()
                   if name.endswith(("API_KEY", "TOKEN")))
    for secret in sorted(secrets, key=len, reverse=True):
        # "local" is the explicit no-auth placeholder, not a minimum secret length.
        if secret and secret != "local":
            text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s\"'<>]+", lambda m: safe_endpoint(m.group()), text)


def _log_root(settings: Any) -> Path:
    return Path(getattr(settings, "failure_log_root", "") or ((config.PROJECT_ROOT or Path.cwd()) / "logs"))


def _project_log_root(settings: Any) -> Path:
    return _log_root(settings) / str(settings.target_name)


@contextmanager
def capture_run_logs(settings: Any):
    """Create this project's log folder; the log handlers own live.log/debug.log."""

    root = _project_log_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    yield root


def reset_failure_summaries(settings: Any) -> None:
    """Clear the previous run's summary; a logging-only failure must not stop the run."""

    root = _project_log_root(settings)
    paths = [_log_root(settings) / f"{settings.target_name}_failed_docs.log", root / "failed_docs.log"]
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.error("failed to remove legacy failure summary %s: %s: %s", path, type(exc).__name__, exc)
    path = root / "failed_docs.md"
    try:
        path.write_text("", encoding="utf-8")  # Also clear the previous run after success.
    except OSError as exc:
        log.error("failed to reset failure summary %s: %s: %s", path, type(exc).__name__, exc)


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
    def short_reason(failure: str) -> str:
        prefix, marker, trace = failure.partition("Traceback (most recent call last):")
        if not marker:
            return failure
        # Older conversion results can already contain a formatted traceback.
        log.debug("failure details: %s", failure)
        reason = next((line.strip() for line in reversed(trace.splitlines()) if line.strip()),
                      "see debug.log")
        return f"{prefix}{reason} (details: debug.log)"

    failures = list(dict.fromkeys(short_reason(failure) for failure in failures))
    warnings = list(dict.fromkeys(short_reason(warning) for warning in warnings))
    entries = failures + warnings
    aliases = _mount_paths(settings) if entries else {}
    sections: dict[str, list[str]] = {}
    failed_paths: list[str] = []
    used_traces: set[str] = set()
    for title, rows, failed in (
        ("失敗ドキュメント", failures, True),
        ("インデックスのエラー（パブリッシュは継続）", warnings, False),
    ):
        for failure in rows:
            path = failure.split(": ", 1)[0].removeprefix("scan ")
            for prefix in ("index: ", "inventory: "):
                if failure.startswith(prefix):
                    path = failure[len(prefix):].split(": ", 1)[0]
            mount = aliases.get(path) or aliases.get(path.rpartition("/")[0])
            label = (mount or path or "<shared>").replace("\n", " ")
            if failed and label not in failed_paths:
                failed_paths.append(label)
            logs = sections.setdefault(label, [])
            if logs:
                logs.append("")
            logs.append(f"{title}: {failure}")
            for message, error, trace, context in traces:
                # Retried errors that recovered are deliberately omitted.
                documents = {aliases.get(arg) or aliases.get(arg.rpartition("/")[0]) for arg in context} - {None}
                if documents and mount and mount not in documents:
                    continue
                reason = error.partition(": ")[2]
                if trace not in used_traces and (error[:500] in failure or (path and path in message and reason and reason[:200] in failure)):
                    used_traces.add(trace)
                    # Full traces belong to debug.log only (audit 2.2); this report keeps the reason.
                    logs.extend(["", f"理由: {error}", "", f"発生時刻: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}",
                                 "詳細: logs/<project>/debug.log（サイズローテーションのため行番号は固定ではありません）"])
    if sections:
        blocks = [
            "\n".join([f"## {path}", "", "```bash", *logs, "```"])
            for path, logs in sections.items()
        ]
        body = redact_log_text(
            f"# 失敗ドキュメント\n\nプロジェクト: `{settings.target_name}`\n\n"
            + "\n\n---\n\n\n".join(blocks)
            + "\n",
            settings,
        )
    else:
        body = ""
    path = _project_log_root(settings) / "failed_docs.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")  # Also clear the previous run after success.
    except OSError as exc:
        log.error("failed to save failure evidence to %s: %s: %s",
                  path, type(exc).__name__, redact_log_text(str(exc), settings))
    # Records, not prints: the live handler must keep the run's failure list.
    for failed_path in failed_paths:
        log.error("document failed: %s", redact_log_text(failed_path, settings))
    log.info("failed documents=%d index errors=%d details=%s", len(failed_paths), len(warnings), path)

