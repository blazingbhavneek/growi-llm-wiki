"""Small logging guards shared by the CLI entry points."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


# Third-party HTTP stacks log every socket event at DEBUG; debug.log is for our own steps.
# httpcore2 is the transport openai/httpx2 pull in, and is NOT a child of "httpcore".
MUTED_LOGGERS = (
    "asyncio",
    "anyio",
    "h11",
    "httpcore",
    "httpcore2",
    "httpx",
    "httpx2",
    "urllib3",
    "openai",
    "langchain",
    "langchain_openai",
    "langchain-openai",
    "anthropic",
)


def mute_third_party_logs() -> None:
    for name in MUTED_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


@contextmanager
def startup_logs():
    """Buffer settings diagnostics until file handlers exist, without stderr fallbacks."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    root = logging.getLogger()
    previous, level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    mute_third_party_logs()
    try:
        yield records
    finally:
        root.handlers = previous
        root.setLevel(level)
        handler.close()


def startup_failed(exc: BaseException) -> None:
    """Settings may not identify a log directory yet; never dump a traceback here."""
    reason = f"{type(exc).__name__}: {exc}"
    for name, value in os.environ.items():
        if value and value != "local" and name.endswith(("API_KEY", "TOKEN")):
            reason = reason.replace(value, "[redacted]")
    try:
        sys.__stderr__.write(f"設定の読み込みに失敗し、ログを保存できません: {' '.join(reason.split())}\n")
        sys.__stderr__.flush()
    except Exception:
        pass


@contextmanager
def file_logging(handlers: list[logging.Handler]):
    """Install owned handlers without closing an enclosing CLI invocation's handlers."""
    root = logging.getLogger()
    previous, level, hook = root.handlers[:], root.level, threading.excepthook
    root.handlers = handlers or [logging.NullHandler()]
    root.setLevel(logging.DEBUG)
    mute_third_party_logs()
    try:
        yield root
    finally:
        root.handlers = previous
        root.setLevel(level)
        threading.excepthook = hook
        for handler in handlers:
            handler.close()


def run_standalone(main: Callable[[], Any]) -> Any:
    """Give module CLIs the same file handlers without changing their stdout contracts."""
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return main()
    try:
        with startup_logs() as pending:
            # Only inspect logging destinations here; each command keeps its real parser.
            parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
            for name in ("config", "project", "out", "output"):
                parser.add_argument(f"--{name}", default="")
            args, _ = parser.parse_known_args()
            from runner import cli
            from common.settings import load_settings
            from publisher.failure_logs import capture_run_logs

            selector = args.config or args.project or os.environ.get("WIKI_PROJECT", "")
            settings = load_settings(selector) if selector else SimpleNamespace(
                target_name=Path(args.out or args.output or "standalone").name or "standalone")
    except Exception as exc:
        startup_failed(exc)
        return 1

    handlers: list[logging.Handler] = []
    run_id = uuid.uuid4().hex[:12]
    try:
        with capture_run_logs(settings) as log_dir:
            handlers = cli._file_handlers(log_dir, run_id=run_id, settings=settings)
    except OSError as exc:
        cli._emergency("ログフォルダー", f"{type(exc).__name__}: {exc}")
    with file_logging(handlers) as root:
        for record in pending:
            root.handle(record)
        threading.excepthook = cli._thread_excepthook
        try:
            return main()
        except Exception as exc:
            root.error("コマンドの実行に失敗しました: コマンド=%s、エラー=%s: %s",
                       sys.argv[0], type(exc).__name__, exc, exc_info=True)
            return 1
