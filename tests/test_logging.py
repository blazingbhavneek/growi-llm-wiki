"""Acceptance tests for the logging design in logs-audit.md (Part 2 + Part 6.1).

Run with the project runtime:  python -m unittest discover -s tests -t .
Everything runs in temporary directories and simulates failures; nothing fills a real disk.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from publisher import failure_logs  # noqa: E402
from publisher.progress import LiveLogFormatter, SyncProgress, _cells, shorten_name  # noqa: E402
from runner import cli  # noqa: E402

TRACE_HEAD = "Traceback (most recent call last)"
LIVE_LINE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+\+\d\d:\d\d \S+ run=\S+ .+$")


class Settings:
    """The attributes the log machinery reads from project settings."""

    target_name = "demo"
    data_root = "/tmp"
    failure_log_root = ""
    growi_token = "TOPSECRETVALUE123"
    chat_api_key = ""
    embed_api_key = ""


def settings_for(tmp: Path) -> Settings:
    return type("S", (Settings,), {"data_root": str(tmp), "failure_log_root": str(tmp / "runs")})


def record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.getLogger("test").makeRecord("test", level, __file__, 1, message, (), None)


class TempLogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.log = logging.getLogger("test.logging")
        cli._emergency_done.clear()
        self.reset()

    def tearDown(self) -> None:
        self.reset()

    def reset(self) -> None:
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            handler.close()
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False

    def handlers(self, *, live_bytes: int = 1_000_000, debug_bytes: int = 10_000_000,
                 run_id: str = "testrun") -> tuple[logging.Handler, logging.Handler]:
        live = cli._ProjectLogHandler(self.tmp / "live.log", maxBytes=live_bytes,
                                      backupCount=99, encoding="utf-8")
        live.setLevel(logging.INFO)
        live.setFormatter(LiveLogFormatter(run_id=run_id))
        debug = cli._ProjectLogHandler(self.tmp / "debug.log", maxBytes=debug_bytes,
                                       backupCount=99, encoding="utf-8")
        debug.setLevel(logging.DEBUG)
        debug.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        return live, debug

    def close(self, *handlers: logging.Handler) -> None:
        for handler in handlers:
            self.log.removeHandler(handler)
            handler.close()

    def text(self, name: str) -> str:
        path = self.tmp / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TracebackSeparationTests(TempLogCase):
    """6.1-1: live keeps one timestamped reason line, debug keeps the full trace."""

    def log_failure(self, live_first: bool) -> tuple[str, str]:
        live, debug = self.handlers()
        for handler in ((live, debug) if live_first else (debug, live)):
            self.log.addHandler(handler)
        try:
            raise ValueError("broken source")
        except ValueError as exc:
            self.log.error("parse failed path=%s error=%s: %s", "team/a.docx",
                           type(exc).__name__, exc, exc_info=True)
        self.close(live, debug)
        return self.text("live.log"), self.text("debug.log")

    def test_one_live_line_and_full_debug_trace_in_both_handler_orders(self):
        for live_first in (True, False):
            self.tmp = Path(tempfile.mkdtemp())
            live, debug = self.log_failure(live_first)
            self.assertEqual(len(live.splitlines()), 1, live)
            self.assertRegex(live.strip(), LIVE_LINE.pattern)
            self.assertNotIn(TRACE_HEAD, live)
            self.assertIn("team/a.docx", live)
            self.assertEqual(live.count("ValueError: broken source"), 1)
            self.assertIn(TRACE_HEAD, debug)
            self.assertIn("ValueError: broken source", debug)

    def test_live_formatting_leaves_the_records_exception_intact(self):
        live, debug = self.handlers()
        try:
            raise KeyError("missing")
        except KeyError as exc:
            entry = self.log.makeRecord("test.logging", logging.ERROR, __file__, 1,
                                        "cache failed", (), (type(exc), exc, exc.__traceback__))
        debug.format(entry)  # the debug formatter is allowed to populate the exc_text cache
        cached = entry.exc_text
        live_text = live.format(entry)
        self.assertTrue(cached and TRACE_HEAD in cached)
        self.assertIs(entry.exc_text, cached)
        self.assertIsNotNone(entry.exc_info)
        self.assertNotIn(TRACE_HEAD, live_text)
        self.assertIn("エラー=KeyError: 'missing'", live_text)
        self.close(live, debug)

    def test_chaining_stack_info_and_multiline_stay_one_live_line(self):
        live, debug = self.handlers()
        self.log.addHandler(live)
        self.log.addHandler(debug)
        try:
            try:
                raise OSError(28, "No space left on device")
            except OSError as inner:
                raise RuntimeError("publish failed") from inner
        except RuntimeError as exc:
            self.log.error("publish failed path=%s", "a\rb\n.docx", exc_info=True, stack_info=True)
        live_text, debug_text = self.text("live.log"), self.text("debug.log")
        self.assertEqual(len(live_text.splitlines()), 1, live_text)
        self.assertNotIn("\r", live_text)
        self.assertNotIn(TRACE_HEAD, live_text)
        self.assertIn("The above exception was the direct cause", debug_text)
        self.assertIn("Stack (most recent call last)", debug_text)

    def test_live_redacts_credentials(self):
        live, debug = self.handlers(run_id="r1")
        live.setFormatter(LiveLogFormatter(
            run_id="r1",
            redact=lambda text: failure_logs.redact_log_text(text, settings_for(self.tmp))))
        self.log.addHandler(live)
        self.log.error("request failed token=%s url=%s", "TOPSECRETVALUE123",
                       "https://growi.example.com/_api?access_token=QUERYSECRET")
        self.close(live, debug)
        live_text = self.text("live.log")
        self.assertNotIn("TOPSECRETVALUE123", live_text)
        self.assertNotIn("QUERYSECRET", live_text)

    def test_production_handlers_redact_short_keys_and_exception_chains(self):
        settings = settings_for(self.tmp)
        settings.chat_api_key = "key123"
        handlers = cli._file_handlers(self.tmp, run_id="redaction", settings=settings)
        for handler in reversed(handlers):  # debug formatting must not poison live's record
            self.log.addHandler(handler)
        try:
            try:
                raise ValueError(f"token={settings.growi_token}")
            except ValueError as inner:
                raise RuntimeError("key=key123 url=https://example.invalid/api?token=QUERYSECRET") from inner
        except RuntimeError:
            self.log.exception("request failed key=%s", settings.chat_api_key)
        self.close(*handlers)
        for name in ("live.log", "debug.log"):
            body = self.text(name)
            for secret in (settings.growi_token, settings.chat_api_key, "QUERYSECRET"):
                self.assertNotIn(secret, body, name)
        self.assertIn(TRACE_HEAD, self.text("debug.log"))
        self.assertNotIn(TRACE_HEAD, self.text("live.log"))

    def test_live_strips_colour_and_terminal_hyperlinks(self):
        live, debug = self.handlers()
        self.log.addHandler(live)
        self.log.error("\x1b[31mfailed\x1b[0m \x1b]8;;https://example.invalid\x1b\\document\x1b]8;;\x1b\\")
        self.close(live, debug)
        body = self.text("live.log")
        self.assertNotIn("\x1b", body)
        self.assertIn("failed document", body)


class LevelAndShapeTests(TempLogCase):
    """6.1-2: DEBUG only in debug; INFO and above in both files; live stays Japanese single-line."""

    def test_levels_reach_the_right_files(self):
        live, debug = self.handlers()
        self.log.addHandler(live)
        self.log.addHandler(debug)
        for level, name in ((logging.DEBUG, "debug"), (logging.INFO, "info"), (logging.WARNING, "warning"),
                            (logging.ERROR, "error"), (logging.CRITICAL, "critical")):
            self.log.log(level, "公開処理: パス=%s、レベル=%s", "team/a.docx", name)
        live_text, debug_text = self.text("live.log"), self.text("debug.log")
        self.assertEqual(len(live_text.splitlines()), 4, live_text)
        self.assertEqual(len(debug_text.splitlines()), 5, debug_text)
        lines = live_text.splitlines()
        self.assertTrue(all(LIVE_LINE.match(line) for line in lines), lines)
        self.assertIn("公開処理", " ".join(lines))
        self.assertNotIn("\x1b", " ".join(lines))
        self.close(live, debug)

    def test_messages_are_written_as_supplied_in_both_files(self):
        live, debug = self.handlers()
        self.log.addHandler(live)
        self.log.addHandler(debug)
        self.log.error("処理に失敗した文書: %s", "team/docs/Broken.docx")
        self.log.info("processed path=%s status=%s", "team/a.docx", "unchanged")
        self.close(live, debug)
        for name in ("live.log", "debug.log"):
            body = self.text(name)
            self.assertIn("処理に失敗した文書: team/docs/Broken.docx", body)
            self.assertIn("processed path=team/a.docx status=unchanged", body)
            self.assertNotIn("ログ詳細（原文）", body)


class RotationAndRetentionTests(TempLogCase):
    """6.1-3/4/5: 100 files per type, .1 newest, append on restart, Japanese-safe."""

    def test_rotation_keeps_current_plus_99_archives(self):
        live, debug = self.handlers(live_bytes=1)  # force one rollover per record
        for i in range(140):
            live.emit(record("document failed: docs/%03d.docx" % i))
        self.close(live, debug)
        names = [path.name for path in self.tmp.glob("live.log*") if not path.name.endswith(".lock")]
        self.assertNotIn("live.log.100", names)
        self.assertEqual(len(names), 100, sorted(names))
        self.assertIn("docs/139.docx", self.text("live.log"))
        self.assertIn("docs/040.docx", self.text("live.log.99"))
        self.assertNotIn("docs/000.docx", "".join(path.read_text(encoding="utf-8") for path in self.tmp.glob("live.log*")))

    def test_production_defaults(self):
        live, debug = cli._file_handlers(self.tmp, run_id="r", settings=settings_for(self.tmp))
        try:
            self.assertEqual((live.maxBytes, live.backupCount, live.level), (1_000_000, 99, logging.INFO))
            self.assertEqual((debug.maxBytes, debug.backupCount, debug.level), (10_000_000, 99, logging.DEBUG))
        finally:
            live.close()
            debug.close()

    def test_reopen_appends_and_distinguishes_runs(self):
        live, debug = self.handlers(run_id="run-one")
        self.log.addHandler(live)
        self.log.info("run started project=%s run=%s command=%s", "demo", "run-one", "sync")
        self.close(live, debug)
        live, debug = self.handlers(run_id="run-two")
        self.log.addHandler(live)
        self.log.info("run started project=%s run=%s command=%s", "demo", "run-two", "publish")
        self.close(live, debug)
        text = self.text("live.log")
        self.assertIn("run=run-one", text)
        self.assertIn("run=run-two", text)
        self.assertEqual(len(text.splitlines()), 2, text)

    def test_japanese_records_larger_than_the_threshold_stay_intact(self):
        live, debug = self.handlers(live_bytes=400)
        message = "インデックスのエラー: " + "あ" * 600
        for _ in range(20):
            live.emit(record(message))
        self.close(live, debug)
        body = "".join(path.read_text(encoding="utf-8") for path in self.tmp.glob("live.log*"))
        self.assertEqual(body.count("あ" * 600), 20)


class HardeningTests(TempLogCase):
    """6.1-6: a logging-only failure disables one destination once and never crashes."""

    def capture_stderr(self):
        class Redirect:
            def __init__(self) -> None:
                self.buffer = ""

            def write(self, text: str) -> int:
                self.buffer += text
                return len(text)

            def flush(self) -> None:
                pass

        return Redirect()

    def test_unopenable_destination_is_skipped_and_the_other_is_kept(self):
        broken = cli._open_log_handler(self.tmp / "missing-dir" / "live.log", level=logging.INFO,
                                       max_bytes=1000, formatter=LiveLogFormatter())
        healthy = cli._open_log_handler(self.tmp / "debug.log", level=logging.DEBUG,
                                        max_bytes=1000, formatter=logging.Formatter("%(message)s"))
        self.assertIsNone(broken)
        self.assertIsNotNone(healthy)
        self.assertIn("missing-dir", " ".join(cli._emergency_done))
        healthy.close()

    def test_write_failure_disables_once_and_healthy_log_continues(self):
        live, debug = self.handlers()
        self.log.addHandler(live)
        self.log.addHandler(debug)
        live.stream.close()  # the destination can no longer be written
        original, sys.__stderr__ = sys.__stderr__, self.capture_stderr()
        try:
            for _ in range(3):
                self.log.error("document failed: %s", "team/a.docx")
        finally:
            notice, sys.__stderr__ = sys.__stderr__.buffer, original
        self.assertTrue(live.broken)
        self.assertEqual(notice.count("ログの保存を停止しました:"), 1, notice)
        self.assertNotIn(TRACE_HEAD, notice)
        self.assertEqual(self.text("live.log"), "")  # a failed destination gets no more writes
        self.assertEqual(len(self.text("debug.log").splitlines()), 3)
        self.close(live, debug)

    def test_rollover_rename_failure_disables_without_a_stderr_dump(self):
        live, debug = self.handlers(live_bytes=1)
        original_rename, os.rename = os.rename, self._failing_rename()
        original, sys.__stderr__ = sys.__stderr__, self.capture_stderr()
        try:
            for _ in range(3):
                live.emit(record("document failed: docs/x.docx"))
        finally:
            notice, sys.__stderr__ = sys.__stderr__.buffer, original
            os.rename = original_rename
        self.assertTrue(live.broken)
        self.assertEqual(notice.count("ログの保存を停止しました:"), 1, notice)
        self.assertNotIn(TRACE_HEAD, notice)
        self.close(live, debug)

    @staticmethod
    def _failing_rename():
        def explode(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        return explode

    def test_broken_stderr_cannot_crash_logging(self):
        class Broken:
            def write(self, *_args):
                raise OSError("stderr is gone")

            def flush(self):
                raise OSError("stderr is gone")

        original, sys.__stderr__ = sys.__stderr__, Broken()
        try:
            cli._emergency("live.log", "OSError: disk full")  # must not raise
        finally:
            sys.__stderr__ = original

    def test_flush_and_close_failures_do_not_break_shutdown(self):
        live, debug = self.handlers()
        live.stream.close()
        live.close()
        self.close(debug)


class ConcurrencyTests(TempLogCase):
    """6.1-7: threads and two processes may share one project's log files."""

    def test_many_threads_write_every_record_once(self):
        live, debug = self.handlers(live_bytes=1_000_000_000)
        worker_log = logging.getLogger("test.threads")
        worker_log.setLevel(logging.INFO)
        worker_log.propagate = False
        worker_log.addHandler(live)

        def worker(index: int) -> None:
            for i in range(200):
                worker_log.info("thread=%d line=%d", index, i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        live.close()
        worker_log.removeHandler(live)
        lines = self.text("live.log").splitlines()
        self.assertEqual(len(lines), 1600)
        self.assertTrue(all(LIVE_LINE.match(line) for line in lines), lines[:3])
        self.close(debug)

    def test_two_processes_rotate_one_file_without_corrupting_it(self):
        """Retention prunes old records by design; racing rollovers must not corrupt or crash."""

        children = []
        for index in range(2):
            script = textwrap.dedent(f"""
                import logging, sys
                sys.path.insert(0, {str(Path(cli.__file__).resolve().parent.parent)!r})
                from runner import cli
                handler = cli._ProjectLogHandler({str(self.tmp / 'shared.log')!r}, maxBytes=2000,
                                                 backupCount=99, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(message)s'))
                log = logging.getLogger('p')
                for i in range(300):
                    handler.emit(log.makeRecord('p', logging.ERROR, 'child.py', 1,
                                                'P{index} line %04d %s' % (i, 'x' * 40), (), None))
                handler.close()
            """)
            children.append(subprocess.Popen([sys.executable, "-c", script],
                                             stderr=subprocess.PIPE, text=True))
        for child in children:
            _, err = child.communicate(timeout=180)
            self.assertEqual(child.returncode, 0, err)
        files = sorted(path for path in self.tmp.glob("shared.log*") if not path.name.endswith(".lock"))
        self.assertGreater(len(files), 1, "concurrent writers must still roll the file")
        lines = [line for path in files for line in path.read_text(encoding="utf-8").splitlines()]
        torn = [line for line in lines if not re.fullmatch(r"P[01] line \d{4} x{40}", line)]
        self.assertEqual(torn, [], "every record must stay whole across concurrent rotation")
        self.assertEqual(len(lines), 600, "no record may be lost or duplicated while two processes rotate")

    def test_repeated_setup_leaves_no_handlers_or_leaked_files(self):
        open_before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            handlers = cli._file_handlers(self.tmp, run_id="r", settings=settings_for(self.tmp))
            logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
            for handler in handlers:
                logging.getLogger().removeHandler(handler)
                handler.close()
        self.assertEqual(logging.getLogger().handlers, [])
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), open_before + 3)


class SummaryTests(TempLogCase):
    """6.1-8: failed_docs.md keeps the grouped redacted reason, not a traceback."""

    def test_summary_keeps_reason_and_debug_reference(self):
        settings = settings_for(self.tmp)
        (self.tmp / "runs" / "demo").mkdir(parents=True)
        trace = TRACE_HEAD + "\n  File \"parse.py\", line 1, in run\nValueError: broken source\n"
        seen: list[str] = []
        capture = _ListHandler(seen)
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            failure_logs._write_summary(
                settings,
                ["team/docs/Broken.docx: ValueError: broken source"],
                [],
                [("team/docs/Broken.docx", "ValueError: broken source", trace, ["team/docs/Broken.docx"])],
            )
        finally:
            root.removeHandler(capture)
        body = (self.tmp / "runs" / "demo" / "failed_docs.md").read_text(encoding="utf-8")
        self.assertIn("team/docs/Broken.docx", body)
        self.assertIn("理由: ValueError: broken source", body)
        self.assertIn("debug.log", body)
        self.assertNotIn(TRACE_HEAD, body)
        self.assertIn("ERROR 処理に失敗した文書: team/docs/Broken.docx", seen)

    def test_summary_reset_failure_is_reported_and_the_run_continues(self):
        settings = settings_for(self.tmp)
        summary = self.tmp / "runs" / "demo" / "failed_docs.md"
        summary.parent.mkdir(parents=True)
        summary.write_text("old", encoding="utf-8")
        summary.chmod(0o444)
        seen: list[str] = []
        capture = _ListHandler(seen)
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            failure_logs.reset_failure_summaries(settings)  # must not raise
        finally:
            summary.chmod(0o644)
            root.removeHandler(capture)
        if os.geteuid() != 0:
            self.assertTrue(any("失敗一覧を初期化できません" in line for line in seen), seen)

    def test_legacy_traceback_in_failure_string_goes_only_to_debug(self):
        trace = TRACE_HEAD + ':\n  File "parse.py", line 1, in run\nValueError: broken source\n'
        sink = io.StringIO()
        debug = logging.StreamHandler(sink)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(debug)
        try:
            failure_logs._write_summary(settings_for(self.tmp), ["team/broken.docx: " + trace], [], [])
        finally:
            root.removeHandler(debug)
            root.setLevel(old_level)
            debug.close()
        body = (self.tmp / "runs" / "demo" / "failed_docs.md").read_text(encoding="utf-8")
        self.assertNotIn(TRACE_HEAD, body)
        self.assertIn("ValueError: broken source", body)
        self.assertIn("debug.log", body)
        self.assertIn(TRACE_HEAD, sink.getvalue())


class EntryPointTests(TempLogCase):
    def test_bad_project_config_has_one_notice_and_no_terminal_traceback(self):
        config = self.tmp / "bad.ini"
        config.write_text("[wrong-section]\nvalue=example\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "main.py", "check", "--project", str(config)],
            cwd=Path(cli.__file__).resolve().parent.parent, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("NoSectionError", result.stderr)
        self.assertNotIn(TRACE_HEAD, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)

    def test_standalone_bad_config_does_not_print_traceback(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "publisher", "publish", "--config",
             str(self.tmp / "missing.ini"), "--out", str(self.tmp / "out"), "--dry-run"],
            cwd=Path(cli.__file__).resolve().parent.parent, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(TRACE_HEAD, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)

    def test_standalone_success_keeps_stdout_and_writes_both_logs(self):
        script = textwrap.dedent(f"""
            import runpy, sys
            from pathlib import Path
            from runner import cli
            from graph import config
            config.PROJECT_ROOT = Path({str(self.tmp)!r})
            sys.argv = ['publisher', 'assemble', '--out', {str(self.tmp / 'project')!r}]
            runpy.run_module('publisher', run_name='__main__')
        """)
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                cwd=Path(cli.__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'action': 'assemble'", result.stdout)
        self.assertEqual(result.stderr, "")
        for name in ("live.log", "debug.log"):
            self.assertIn("assemble", (self.tmp / "logs" / "project" / name).read_text(encoding="utf-8"))

    def test_successful_startup_replays_buffered_diagnostics(self):
        from runner.logging_support import startup_logs
        root = logging.getLogger()
        before, level = root.handlers[:], root.level
        with startup_logs() as pending:
            logging.getLogger("test.startup").error("deferred settings failure")
        self.assertEqual([entry.getMessage() for entry in pending], ["deferred settings failure"])
        self.assertEqual(root.handlers, before)
        self.assertEqual(root.level, level)

    def test_nested_cli_logging_does_not_close_outer_handlers(self):
        from runner.logging_support import file_logging
        settings = settings_for(self.tmp)
        outer = cli._file_handlers(self.tmp, run_id="outer", settings=settings)
        inner = cli._file_handlers(self.tmp, run_id="inner", settings=settings)
        with file_logging(outer) as root:
            root.info("outer before nested CLI")
            with file_logging(inner):
                root.info("inner CLI")
            self.assertIsNotNone(outer[0].stream)
            root.info("outer after nested CLI")
        body = self.text("live.log")
        self.assertIn("outer before nested CLI", body)
        self.assertIn("inner CLI", body)
        self.assertIn("outer after nested CLI", body)


class GrowiStatusTests(unittest.TestCase):
    def test_expected_missing_page_is_not_an_error(self):
        import httpx
        from graph.growi.client import GrowiClient
        client = GrowiClient("https://example.invalid", "", transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"message": "Page not found"})))
        with patch("graph.growi.client.log.error") as error:
            self.assertIsNone(asyncio.run(client.get_page(path="/new-page")))
        error.assert_not_called()

    def test_unexpected_http_error_is_still_reported(self):
        import httpx
        from graph.growi.client import GrowiAPIError, GrowiClient
        for status in (401, 500):
            client = GrowiClient("https://example.invalid", "", transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"message": "Request failed"})))
            with patch("graph.growi.client.log.error") as error:
                with self.assertRaises(GrowiAPIError):
                    asyncio.run(client.get_page(path="/existing-page"))
            error.assert_called_once()


class ProgressReadabilityTests(unittest.TestCase):
    def test_japanese_names_keep_identifying_text_within_terminal_width(self):
        name = "(社外秘)制御機能仕様書【3章】_docx.md"
        self.assertEqual(shorten_name(name), name)
        for name in (name * 3, "long-source-document-name_" * 4, "長い日本語とASCII_" * 8):
            for width in (10, 25, 48):
                shortened = shorten_name(name, width)
                self.assertLessEqual(_cells(shortened), width)
                self.assertIn("…", shortened)

    def test_similar_document_names_and_paths_keep_independent_progress(self):
        documents = [
            "(社外秘)制御機能仕様書【3章】_docx.md",
            "(社外秘)融通計算機能仕様書_docx.md",
            "/team-a/shared.docx", "/team-b/shared.docx",
        ]
        progress = SyncProgress(stream=io.StringIO())
        try:
            with self.assertLogs("progress", level="INFO") as captured:
                for document in documents:
                    progress.on_event({"stage": "writer", "step": "page_start",
                                       "document": document, "total": 1})
                for document in documents:
                    progress.on_event({"stage": "writer", "step": "page_done",
                                       "document": document, "total": 1})
            self.assertEqual(len(progress._phases), 4)
            self.assertTrue(all(phase.reported for phase in progress._phases.values()))
            for document in documents:
                self.assertIn(f"[{document}] Wikiの生成・確認: 完了", "\n".join(captured.output))
        finally:
            progress.close()

    def test_fast_writer_supplies_total_before_the_first_page_finishes(self):
        from graph.fast.writer import _generate_page

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        progress = SyncProgress(stream=terminal)
        session = SimpleNamespace(page=SimpleNamespace(title="操作条件"), prompt_page=None, page_units=[])

        def on_event(event):
            progress.on_event({**event, "document": "制御機能仕様書_docx.md"})

        try:
            with patch("graph.fast.writer._source_body", return_value="原文"):
                asyncio.run(_generate_page(session, lines=[], model=None, config=None,
                                           stop_check=None, on_progress=on_event, total_pages=28))
            self.assertEqual(progress._stage_bar.total, 28)
            self.assertIn("0/28ページ", terminal.getvalue())
            self.assertNotIn("?件/s", terminal.getvalue())
        finally:
            progress.close()

    def test_unknown_total_does_not_show_a_misleading_zero_count(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        progress = SyncProgress(stream=terminal)
        try:
            progress.on_event({"stage": "parse", "step": "start", "document": "仕様書.docx"})
            self.assertIn("文書の変換", terminal.getvalue())
            self.assertNotIn("0件", terminal.getvalue())
            self.assertNotIn("?件/s", terminal.getvalue())
        finally:
            progress.close()

    def test_eighty_column_terminal_keeps_names_counts_and_labels_readable(self):
        from tqdm import tqdm

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        def bar(*args, **kwargs):
            return tqdm(*args, **{**kwargs, "dynamic_ncols": False, "ncols": 80})

        document = "(社外秘)制御機能仕様書【3章】_docx.md"
        with patch("publisher.progress.tqdm", side_effect=bar):
            progress = SyncProgress(stream=Terminal())
            try:
                progress.add_documents([document, "別の仕様書.docx"])
                progress.on_event({"stage": "writer", "step": "page_start", "document": document,
                                   "page": "機器制御可・不可条件", "total": 28})
                self.assertIn(document, str(progress._doc_bar))
                self.assertIn("0/2件", str(progress._doc_bar))
                self.assertIn("0/28ページ", str(progress._stage_bar))
                self.assertIn("機器制御可・不可条件", str(progress._stage_bar))
                progress.on_event({"stage": "writer", "step": "page_judged", "document": document})
                self.assertIn("内容を確認済み", str(progress._stage_bar))
                self.assertNotIn("page_judged", str(progress._stage_bar))
                self.assertLessEqual(_cells(str(progress._stage_bar)), 80)
            finally:
                progress.close()

    def test_wiki_heartbeat_reports_the_active_phase_and_page_count(self):
        progress = SyncProgress(stream=io.StringIO())
        try:
            progress.on_event({"stage": "writer", "step": "page_start",
                               "document": "仕様書.docx", "total": 28})
            phase = progress._phases[("仕様書.docx", "writer")]
            with patch("publisher.progress.time.monotonic", return_value=phase.started + 60):
                with self.assertLogs("progress", level="INFO") as captured:
                    progress.on_event({"stage": "wiki", "step": "waiting", "document": "仕様書.docx"})
            self.assertIn("Wikiの生成・確認を継続中（1分00秒経過、0/28ページ完了）", captured.output[0])
            self.assertEqual(len(progress._phases), 1)
            progress.on_event({"stage": "writer", "step": "done", "document": "仕様書.docx"})
            with patch("publisher.progress.log.info") as info:
                progress.on_event({"stage": "wiki", "step": "waiting", "document": "仕様書.docx"})
            info.assert_not_called()
        finally:
            progress.close()

    def test_long_running_heartbeat_uses_minutes_and_completed_page_count(self):
        progress = SyncProgress(stream=io.StringIO())
        try:
            progress.on_event({"stage": "writer", "step": "page_start",
                               "document": "仕様書.docx", "total": 28})
            for _ in range(7):
                progress.on_event({"stage": "writer", "step": "page_done",
                                   "document": "仕様書.docx", "total": 28})
            phase = progress._phases[("仕様書.docx", "writer")]
            with patch("publisher.progress.time.monotonic", return_value=phase.started + 1079):
                with self.assertLogs("progress", level="INFO") as captured:
                    progress.on_event({"stage": "wiki", "step": "waiting", "document": "仕様書.docx"})
            self.assertIn("17分59秒経過、7/28ページ完了", captured.output[0])
        finally:
            progress.close()


class FastWriterProgressTests(unittest.TestCase):
    def test_progress_advances_while_another_page_is_generating_then_tracks_saves(self):
        from graph.fast.repair import RepairJudgeResult
        from graph.fast.writer import rewrite_pending_pages
        from graph.wiki.config import WikiConfig
        from graph.wiki.pipeline import RewriteResult, SeedPage

        # Exercise real generation, retries, title finalization and local writes;
        # only the two model-call boundaries are simulated.
        for resumed, fallback in ((0, False), (1, False), (0, True)):
            with self.subTest(resumed=resumed, fallback=fallback), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pages = [SeedPage(number=n, title=f"元の見出し{n}", chapter="", summary="",
                                  owner_ranges=[(n, n)], filename=f"{n:03d}.md", page_id=str(n))
                         for n in range(1, resumed + 3)]
                results = [RewriteResult(page=pages[0], markdown="既存のページ", attempts=1)] if resumed else []
                progress = SyncProgress(stream=io.StringIO())
                events = []

                async def scenario():
                    release = asyncio.Event()
                    first_finished = asyncio.Event()
                    last_started = asyncio.Event()

                    def on_event(event):
                        events.append(event)
                        progress.on_event({**event, "document": "仕様書.docx"})
                        if event["stage"] == "writer" and event["step"] == "page_done" and not event.get("resumed"):
                            first_finished.set()

                    async def writer(page, *_args, **_kwargs):
                        if page.number == pages[-1].number:
                            last_started.set()
                            await release.wait()
                        elif fallback:
                            raise ValueError("simulated model failure")
                        return f"確認後の見出し{page.number}", f"本文{page.number}"

                    async def judge(page, *_args, stem, **_kwargs):
                        if page.number == pages[resumed].number and stem == "rewrite-01-judge":
                            return RepairJudgeResult(coverage_score=95, body_defects=["修正が必要"]), ""
                        return RepairJudgeResult(approved=True, coverage_score=100), ""

                    if resumed:
                        on_event({"stage": "writer", "step": "page_start", "total": len(pages), "resumed": True})
                        on_event({"stage": "writer", "step": "page_done", "total": len(pages), "resumed": True})

                    with patch("graph.fast.writer._writer", side_effect=writer), \
                            patch("graph.fast.writer._judge", side_effect=judge):
                        task = asyncio.create_task(rewrite_pending_pages(
                            pending=pages[resumed:], results=results, pages=pages,
                            lines=[f"原文{page.number}" for page in pages], units=[], model=object(),
                            config=WikiConfig(policy="fast", rewrite_concurrency=2),
                            work_root=root / "work", wiki_root=root / "wiki", state_root=root / "state",
                            source_path=root / "仕様書.md", source_snapshot_path=root / "source.md",
                            source_sha256="a" * 64, source_line_count=len(pages), rewrite_version="test",
                            stop_check=None, on_progress=on_event, parents=None,
                        ))
                        try:
                            await asyncio.wait_for(first_finished.wait(), timeout=3)
                            await asyncio.wait_for(last_started.wait(), timeout=3)
                            phase = progress._phases[("仕様書.docx", "writer")]
                            self.assertEqual(phase.current, resumed + 1)
                            self.assertFalse(phase.reported)
                            self.assertNotIn(("仕様書.docx", "writer-save"), progress._phases)
                            self.assertFalse((root / "wiki" / pages[resumed].filename).exists())
                            self.assertFalse(task.done())
                        finally:
                            release.set()
                            await asyncio.wait_for(task, timeout=3)

                try:
                    with self.assertLogs("graph.fast.writer", level="INFO") as captured:
                        asyncio.run(scenario())
                    generated = [event for event in events if event["stage"] == "writer" and event["step"] == "page_done"]
                    self.assertEqual(len(generated), len(pages))
                    self.assertEqual(generated[resumed]["status"], "verbatim" if fallback else "generated")
                    self.assertTrue(progress._phases[("仕様書.docx", "writer")].reported)
                    saves = progress._phases[("仕様書.docx", "writer-save")]
                    self.assertTrue(saves.reported)
                    self.assertEqual((saves.current, saves.total), (2, 2))
                    for page in pages[resumed:]:
                        self.assertTrue((root / "wiki" / page.filename).exists())
                        self.assertTrue((root / "state" / "pages" / f"{page.number:03d}.json").exists())
                    first_body = (root / "wiki" / pages[resumed].filename).read_text()
                    self.assertIn(f"確認後の見出し{pages[-1].number}", first_body)
                    self.assertIn("原文を使用" if fallback else "生成・確認済み", "\n".join(captured.output))
                finally:
                    progress.close()


class ActivityReadabilityTests(TempLogCase):
    def test_unavailable_activity_api_has_one_actionable_warning_and_debug_trace(self):
        import httpx
        from graph.growi.client import GrowiClient
        from publisher.activity import ActivityDetector
        from runner.logging_support import file_logging

        for message, expected in (("AuditLog is not enabled", "監査ログが無効"),
                                  ("Method not allowed", "変更履歴APIを利用できません")):
            with self.subTest(message=message):
                self.tmp = Path(tempfile.mkdtemp())
                client = GrowiClient("https://example.invalid", "", transport=httpx.MockTransport(
                    lambda request: httpx.Response(405, json={"errors": [{"message": message}]})))
                detector = ActivityDetector(SimpleNamespace(metadata=self.tmp),
                                            endpoint=client.url, boundary="/test")
                live, debug = self.handlers()
                with file_logging([live, debug]):
                    batch = detector.poll(client, {})
                self.assertEqual(batch.fallback_reason, "activity_api_unavailable")
                body = self.text("live.log")
                self.assertEqual(len(body.splitlines()), 1, body)
                self.assertIn("警告", body)
                self.assertIn(expected, body)
                self.assertIn("全ページを確認", body)
                self.assertIn("対象=/test", body)
                self.assertNotIn("GrowiAPIError", body)
                self.assertNotIn(TRACE_HEAD, body)
                self.assertIn(TRACE_HEAD, self.text("debug.log"))
                self.assertIn(message, self.text("debug.log"))


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(f"{record.levelname} {record.getMessage()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
