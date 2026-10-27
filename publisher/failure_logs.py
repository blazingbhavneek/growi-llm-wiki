"""Failure evidence lives outside disposable candidate worktrees."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from graph import config
from graph.workspace.parser_client import safe_endpoint


def _redact(text: str, settings: Any) -> str:
    for name in ("growi_token", "chat_api_key", "embed_api_key"):
        secret = str(getattr(settings, name, "") or "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s\"'<>]+", lambda m: safe_endpoint(m.group()), text)


def _log_root(settings: Any) -> Path:
    return Path(getattr(settings, "failure_log_root", "") or ((config.PROJECT_ROOT or Path.cwd()) / "logs"))


def _project_log_root(settings: Any) -> Path:
    return _log_root(settings) / str(settings.target_name)


class _Tee:
    """Write a console stream to its original destination and the live log."""

    def __init__(self, stream: Any, transcript: Any, lock: threading.RLock):
        self.stream = stream
        self.transcript = transcript
        self.lock = lock

    def write(self, text: str) -> int:
        with self.lock:
            written = self.stream.write(text)
            self.write_transcript(text)
            return written

    @property
    def terminal_stream(self) -> Any:
        """The real console stream, used for transient tqdm redraws."""

        return self.stream

    def write_transcript(self, text: str) -> None:
        """Write stable output to the live log without touching the console."""

        with self.lock:
            self.transcript.write(text)
            self.transcript.flush()

    def flush(self) -> None:
        with self.lock:
            try:
                self.stream.flush()
            finally:
                self.transcript.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


@contextmanager
def capture_run_logs(settings: Any):
    """Start fresh project logs and tee this invocation's console output live."""

    root = _project_log_root(settings)
    root.mkdir(parents=True, exist_ok=True)

    legacy_summary = _log_root(settings) / f"{settings.target_name}_failed_docs.log"
    legacy_summary.unlink(missing_ok=True)
    (root / "failed_docs.log").unlink(missing_ok=True)

    (root / "failed_docs.md").write_text("", encoding="utf-8")
    lock = threading.RLock()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with (root / "live.log").open("w", encoding="utf-8", buffering=1) as transcript:
        sys.stdout = _Tee(original_stdout, transcript, lock)
        sys.stderr = _Tee(original_stderr, transcript, lock)
        try:
            yield root
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                sys.stdout, sys.stderr = original_stdout, original_stderr


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
                    logs.extend(["", trace.rstrip()])
    if sections:
        blocks = [
            "\n".join([f"## {path}", "", "```bash", *logs, "```"])
            for path, logs in sections.items()
        ]
        body = _redact(
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
        print(_redact(f"エラーログの保存に失敗: {path}: {type(exc).__name__}: {exc}", settings), file=sys.stderr)
    summary = [f"失敗ドキュメント一覧（{len(failed_paths)}件）:"]
    summary.extend(f"  - {failed_path}" for failed_path in failed_paths)
    summary.append(f"インデックスのエラー: {len(warnings)}件。詳細: {path}")
    print(_redact("\n".join(summary), settings), file=sys.stderr, flush=True)

