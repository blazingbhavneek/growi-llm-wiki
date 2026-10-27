"""Bounded failure handling through the real queue, candidates and publisher."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import main
from graph.growi.client import GrowiAPIError, GrowiPage, GrowiPublisher
from graph.wiki.storage import write_json_atomic
from graph.workspace import parser_client, writer
from graph.workspace.project import open_project
from publisher import pipeline, queue
from publisher.history import candidate, commit_candidate, ensure_repository, last_good
from publisher.human_changes import apply_generated
from publisher.ledger import load_ledger


class StoppedBuildRecoveryTest(unittest.TestCase):
    """A sync stopped mid-document, then `publish`, then `sync` again must continue."""

    def setUp(self) -> None:
        from graph.workspace.project import Project

        self.tmp = tempfile.TemporaryDirectory()
        self.project = Project(Path(self.tmp.name) / "p", Path(self.tmp.name) / "p" / "mount").ensure()
        ensure_repository(self.project)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _advance_last_good(self, name: str) -> str:
        from publisher.history import checkpoint_live

        (self.project.wiki / name).write_text("built\n", encoding="utf-8")
        return checkpoint_live(self.project, f"publish {name}")

    def _stopped_build(self, base: str, commit: str = "") -> None:
        with queue._connect(self.project) as conn:
            conn.execute(
                "INSERT INTO transactions(operation_id,candidate_commit,base_commit,phase,started_at) VALUES('op-x',?,?,'building',0)",
                (commit, base),
            )
            conn.execute(
                "INSERT INTO jobs(rel,raw_rel,operation,lane,status,available_at,created_at,updated_at,base_commit) "
                "VALUES('a.xls','a_xls.md','upsert','slow','running',0,0,0,?)",
                (base,),
            )

    def _state(self) -> tuple[list, list]:
        with queue._connect(self.project) as conn:
            return (list(conn.execute("SELECT operation_id FROM transactions")),
                    [tuple(row) for row in conn.execute("SELECT rel,status FROM jobs")])

    def test_unpublished_build_is_requeued_after_publish_moved_last_good(self) -> None:
        self._stopped_build(last_good(self.project))
        self._advance_last_good("published.md")
        queue.recover(self.project, SimpleNamespace(wiki_linker_enabled=False))
        self.assertEqual(self._state(), ([], [("a.xls", "queued")]))

    def test_build_promoted_before_the_stop_is_not_redone(self) -> None:
        base = last_good(self.project)
        built = self._advance_last_good("built.md")  # the build's own promoted commit
        self._stopped_build(base, commit=built)
        self._advance_last_good("published.md")
        queue.recover(self.project, SimpleNamespace(wiki_linker_enabled=False))
        self.assertEqual(self._state(), ([], []))

    def test_interrupted_publication_still_refuses_a_moved_base(self) -> None:
        base = last_good(self.project)
        self._advance_last_good("published.md")
        with queue._connect(self.project) as conn:
            conn.execute(
                "INSERT INTO transactions(operation_id,candidate_commit,base_commit,phase,started_at) VALUES('op-p','',?,'publishing',0)",
                (base,),
            )
        with self.assertRaisesRegex(RuntimeError, "last-good changed"):
            queue.recover(self.project, SimpleNamespace(wiki_linker_enabled=False))


class ParserFallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "doc.pdf"
        self.path.write_bytes(b"complete immutable document")
        self.settings = SimpleNamespace(parser_fallback_base_url="http://fallback", parser_describe_images=False)

    def response(self, payload=None, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response

    def parse(self, **kwargs):
        return parser_client.parse_document(self.path, base_url="http://primary", settings=self.settings, **kwargs)

    def test_success_does_not_call_fallback(self):
        with patch.object(parser_client.requests, "post", return_value=self.response({"markdown": "# Good"})) as post:
            self.assertEqual(self.parse(), "# Good")
            self.assertEqual(post.call_count, 1)

    def test_fallback_gets_full_stream_and_identical_profile(self):
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs["files"]["file"][1].read(), kwargs["params"], kwargs["timeout"]))
            return self.response({}, 503) if len(calls) == 1 else self.response({"markdown": "# Good", "pages": ["# Good"]})
        with patch.object(parser_client.requests, "post", side_effect=post):
            self.assertEqual(self.parse(timeout_s=17), "# Good")
        self.assertEqual([row[0] for row in calls], ["http://primary/parse/llm-wiki", "http://fallback/parse/llm-wiki"])
        self.assertEqual(calls[0][1:], calls[1][1:])
        self.assertEqual(calls[1][1], self.path.read_bytes())

    def test_invalid_output_uses_same_validation_on_fallback(self):
        for payload in ({}, [], {"markdown": None}, {"markdown": 1}, {"markdown": " "},
                        {"markdown": "ok", "pages": "invalid"}):
            with self.subTest(payload=payload), patch.object(parser_client.requests, "post", side_effect=[
                self.response(payload), self.response({"markdown": "# Good"}),
            ]) as post:
                self.assertEqual(self.parse(), "# Good")
                self.assertEqual(post.call_count, 2)

    def test_size_rejection_uses_fallback(self):
        def validate(text):
            pipeline._check_parse_size("doc.pdf", "x" * 3000, text)
        with patch.object(parser_client.requests, "post", side_effect=[
            self.response({"markdown": "small"}), self.response({"markdown": "x" * 3000}),
        ]) as post:
            self.assertEqual(len(self.parse(validate_markdown=validate)), 3000)
            self.assertEqual(post.call_count, 2)

    def test_failure_is_bounded_with_missing_duplicate_or_failing_fallback(self):
        for endpoint, expected in (("", 1), ("http://primary/", 1), ("http://fallback", 2)):
            self.settings.parser_fallback_base_url = endpoint
            with self.subTest(endpoint=endpoint), patch.object(parser_client.requests, "post", side_effect=requests.Timeout("slow")) as post:
                with self.assertRaises(requests.Timeout):
                    self.parse()
                self.assertEqual(post.call_count, expected)

    def test_local_preparation_or_image_failure_does_not_reupload(self):
        with patch.object(parser_client, "build_manifest", side_effect=RuntimeError("local workbook")), patch.object(parser_client.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "local workbook"):
                self.parse()
            post.assert_not_called()
        self.settings.parser_describe_images = True
        with patch.object(parser_client.requests, "post", return_value=self.response({"markdown": "# ok"})) as post, \
             patch.object(parser_client, "reuse_image_descriptions", side_effect=RuntimeError("image model")):
            with self.assertRaisesRegex(RuntimeError, "image model"):
                self.parse(previous_markdown="# before")
            self.assertEqual(post.call_count, 1)


class ParseAheadTest(unittest.TestCase):
    def test_cache_hits_consume_the_two_position_window(self) -> None:
        from graph.workspace.project import Project
        from publisher import history
        from publisher.ahead import ParseAhead

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = Project(root / "project", root / "mount").ensure()
            cache = root / "cache"
            cache.mkdir()
            targets = [cache / f"{index}.md" for index in range(3)]
            targets[0].write_text("cached zero", encoding="utf-8")
            targets[1].write_text("cached one", encoding="utf-8")
            seen = [threading.Event() for _ in targets]
            parsed = threading.Event()
            written = threading.Event()
            ready: list[tuple[int, str]] = []
            jobs = [
                SimpleNamespace(
                    rel=f"doc-{index}.pptx", raw_rel=f"doc-{index}_pptx.md",
                    target_blob_oid=f"blob-{index}", target_sha256=str(index),
                    from_rel="", classification="none",
                )
                for index in range(3)
            ]

            def cache_file(_settings, source_sha256, _previous, _validating):
                index = int(source_sha256)
                seen[index].set()
                return targets[index]

            def parse(*_args, **_kwargs):
                parsed.set()
                return "parsed two"

            def write(path, text):
                path.write_text(text, encoding="utf-8")
                written.set()

            with (
                patch.object(pipeline, "parse_cache_file", side_effect=cache_file),
                patch.object(pipeline, "_write_raw", side_effect=write),
                patch.object(history, "read_blob", return_value=b"source"),
                patch.object(parser_client, "parse_document", side_effect=parse),
            ):
                worker = ParseAhead(
                    project,
                    SimpleNamespace(parser_base_url="http://parser"),
                    jobs,
                    on_ready=lambda position, job, _markdown: ready.append((position, job.rel)),
                )
                self.addCleanup(worker.stop)

                self.assertTrue(seen[0].wait(1))
                self.assertTrue(seen[1].wait(1))
                self.assertFalse(parsed.wait(0.1))

                worker.advance()
                self.assertTrue(parsed.wait(1))
                self.assertTrue(written.wait(1))
                self.assertEqual(targets[2].read_text(encoding="utf-8"), "parsed two")
                self.assertEqual(ready, [(0, "doc-0.pptx"), (1, "doc-1.pptx"), (2, "doc-2.pptx")])


class PlannerAheadTest(unittest.TestCase):
    def test_current_document_is_skipped_and_future_documents_stay_fifo(self) -> None:
        from publisher.ahead import PlannerAhead

        with patch.object(threading.Thread, "start"):
            worker = PlannerAhead(object(), object())
        jobs = [SimpleNamespace(rel=f"doc-{index}.pdf") for index in range(3)]
        worker.add(0, jobs[0], "current")
        worker.add(1, jobs[1], "next")
        worker.add(2, jobs[2], "after")

        self.assertEqual(worker._queue.get_nowait()[0].rel, "doc-1.pdf")
        self.assertEqual(worker._queue.get_nowait()[0].rel, "doc-2.pdf")


class PlannerCacheTest(unittest.TestCase):
    def test_serial_pipeline_consumes_cached_plan_without_replanning(self) -> None:
        from graph.wiki import pipeline as wiki_pipeline
        from graph.wiki.config import WikiConfig
        from graph.wiki.schemas import CompiledSeedPlan
        from graph.wiki.wire import SeedRange

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.md"
            source.write_text("one line\n", encoding="utf-8")
            config = WikiConfig(
                policy="fast",
                chat_base_url="",
                run_dir=str(root / "run"),
                plan_cache_dir=str(root / "cache"),
            )
            text = source.read_text(encoding="utf-8")
            lines = wiki_pipeline.split_source_lines(wiki_pipeline.normalize_source(text))
            plan = CompiledSeedPlan(pages=[SeedRange(
                title="Cached",
                summary="Cached page summary.",
                source_start=1,
                source_end=1,
            )])
            wiki_pipeline._store_plan_cache(
                wiki_pipeline._plan_cache_path(text, config), text, lines, config, plan,
            )
            with patch.object(wiki_pipeline, "_compute_seed_plan", side_effect=AssertionError("replanned")):
                result = asyncio.run(wiki_pipeline.run_pipeline(source, config=config))

            self.assertTrue((result / "wiki" / "001-Cached.md").exists())


class LinkAheadTest(unittest.TestCase):
    def test_smaller_ready_document_moves_before_larger_queued_document(self) -> None:
        from graph.workspace.project import Project
        from publisher.ahead import LinkAhead

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = Project(root / "project", root / "mount").ensure()
            for rel, text in (("large.md", "x" * 100), ("small.md", "x")):
                planning = project.wiki_dir(rel) / "_planning"
                (planning / "pages").mkdir(parents=True)
                (planning / "pages" / "001.md").write_text(text, encoding="utf-8")
                (planning / "linker.json").write_text('{"status":"pending"}', encoding="utf-8")

            with patch.object(threading.Thread, "start"):
                worker = LinkAhead(project, SimpleNamespace(), model=object())
            worker.add(project, ["large.md", "small.md"])

            self.assertEqual(worker._queue.get_nowait()[2][0], "small.md")


class Client:
    def __init__(self):
        self.pages = {}
        self.counter = 0

    def copy(self, page):
        return page.model_copy(deep=True) if page else None

    async def get_page(self, *, path=None, page_id=None):
        return self.copy(self.pages.get(page_id) if page_id else next((p for p in self.pages.values() if p.path == path), None))

    async def create_page(self, path, body):
        self.counter += 1
        page = GrowiPage(page_id=f"p{self.counter}", revision_id=f"r{self.counter}", path=path, body=body)
        self.pages[page.page_id] = page
        return self.copy(page)

    async def update_page(self, page_id, revision_id, body):
        page = self.pages[page_id]
        if page.revision_id != revision_id:
            raise GrowiAPIError(409, "PUT", "/page")
        self.counter += 1
        page.revision_id, page.body = f"r{self.counter}", body
        return self.copy(page)

    async def list_all_pages(self, prefix):
        return [self.copy(p) for p in self.pages.values() if p.path.startswith(prefix + "/")]

    async def delete_pages(self, revisions):
        for page_id, revision in revisions.items():
            if self.pages[page_id].revision_id != revision:
                raise GrowiAPIError(409, "DELETE", "/page")
        for page_id in revisions:
            del self.pages[page_id]


class IsolatedSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        mount = self.base / "mount"
        mount.mkdir()
        self.settings = SimpleNamespace(
            data_root=str(self.base / "data"), target_name="test", mount_path=str(mount),
            ingest_mode="wiki", wiki_linker_enabled=False, sync_isolated=True,
            failure_log_root=str(self.base / "logs"), growi_token="private-test-secret",
        )
        self.project = open_project(self.settings)
        self.client = Client()
        self.publisher = GrowiPublisher(self.client, SimpleNamespace(write_path="/test", root_path="/test", mode="attach"))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(pipeline, "_publisher", return_value=self.publisher))
        self.stack.enter_context(patch.object(pipeline, "republish_if_stale", return_value=None))
        self.stack.enter_context(patch.object(pipeline, "_model", return_value=object()))
        self.stack.enter_context(patch.object(pipeline, "Embedder", return_value=None))
        self.stack.enter_context(patch("publisher.index.build_index", return_value={"done": [], "failures": []}))
        self.stack.enter_context(patch("publisher.index.delete_document_index"))
        self.stack.enter_context(patch.object(pipeline, "write_wiki_pages", side_effect=self.generate))
        self.stack.enter_context(patch.object(main, "_settings", return_value=self.settings))

    def add(self, rel):
        path = self.project.mount / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n\nUseful source content.", encoding="utf-8")

    def generate(self, project, raw_rel, **kwargs):
        folder = project.wiki_dir(raw_rel)
        folder.mkdir(parents=True, exist_ok=True)
        writer.write_source_stamp(folder, project.raw_file(raw_rel), raw_rel, identity_seed=kwargs.get("identity_seed"))
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "disabled"})
        (folder / "001.md").write_text(project.raw_file(raw_rel).read_text(), encoding="utf-8")
        apply_generated(project, raw_rel)
        return writer.WriteResult(target=folder, touched=[raw_rel])

    def run_sync(self, only=None):
        args = SimpleNamespace(items=only or [], force=False, verbose=False, isolated=None)
        with contextlib.redirect_stdout(io.StringIO()):
            return main.cmd_sync(args)

    def test_sync_cli_defaults_to_configured_isolation_with_batch_escape_hatch(self):
        parser = main.build_parser()
        self.assertIsNone(parser.parse_args(["sync", "--project", "test"]).isolated)
        self.assertTrue(parser.parse_args(["sync", "--project", "test", "--isolated"]).isolated)
        self.assertFalse(parser.parse_args(["sync", "--project", "test", "--no-isolated"]).isolated)

    def test_isolated_sync_finishes_builder_pass_before_linker_phase(self):
        self.settings.wiki_linker_enabled = True
        for rel in ("a.md", "b.md"):
            self.add(rel)
        order = []

        def parse(item, path, settings, **kwargs):
            order.append(f"build:{item.rel}")
            return path.read_text()

        def link(settings, **kwargs):
            order.append("link")
            return {"done": [], "failures": []}

        with patch.object(pipeline, "_parse", side_effect=parse), \
             patch.object(pipeline, "link_pending_isolated", side_effect=link):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(order, ["build:a.md", "build:b.md", "link"])

    def test_idle_sync_captures_a_ui_edit_before_any_scan_or_build(self):
        from publisher.human_changes import HumanStore

        self.add("a.md")
        self.settings.policy = "fast"  # linking off: the first sync publishes what it built
        self.assertEqual(self.run_sync(), 0)
        page = next(iter(self.client.pages.values()))
        marker_text = page.body
        human = marker_text.replace("Useful source content.", "Useful source content, as edited in GROWI.")
        self.assertNotEqual(human, marker_text)
        page.body, page.revision_id = human, "human-1"
        order = []
        real_pull = pipeline.pull_growi_once

        def pull(settings, **kwargs):
            order.append("pull")
            return real_pull(settings, **kwargs)

        def build(*args, **kwargs):
            order.append("build")
            return self.generate(*args, **kwargs)

        with patch.object(pipeline, "pull_growi_once", side_effect=pull), \
             patch.object(pipeline, "write_wiki_pages", side_effect=build):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(order, ["pull"])  # nothing changed in the source: no build, but the edit is taken
        folder = self.project.wiki_dir("a_md.md")
        self.assertIn("as edited in GROWI", (folder / "001.md").read_text(encoding="utf-8"))
        self.assertEqual(HumanStore(self.project).status()["counts"]["human_information"], 1)
        # A repeat run with no new change is a no-op.
        with patch.object(pipeline, "write_wiki_pages", side_effect=build):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(order, ["pull"])

    def test_sync_does_not_pull_after_republishing_for_a_changed_endpoint(self):
        self.stack.enter_context(patch.object(pipeline, "republish_if_stale", return_value={"failures": [], "done": []}))
        with patch.object(pipeline, "pull_growi_once") as pull:
            self.assertEqual(self.run_sync(), 0)
        pull.assert_not_called()

    def test_parse_ahead_advances_once_for_each_slow_claim(self):
        for rel in ("a.md", "b.md"):
            self.add(rel)
        advanced = []

        class Parser:
            def __init__(self, project, settings, jobs, **_kwargs):
                pass

            def advance(self):
                advanced.append("advance")

            def stop(self):
                pass

        with patch("publisher.ahead.ParseAhead", Parser):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(advanced, ["advance", "advance"])

    def test_three_queues_link_earlier_built_documents_while_new_ones_build(self):
        """A document built by a stopped run goes to the linker queue before any new build."""
        self.add("a.md")
        self.assertEqual(self.run_sync(), 0)  # built; linker disabled
        self.settings.wiki_linker_enabled = True  # a.md is now built but not linked
        self.add("b.md")
        order = []

        class Linker:
            def __init__(self, project, settings, **_kwargs):
                pass

            def add(self, project, raw_rels):
                order.extend(f"link-ahead:{rel}" for rel in raw_rels)

            def close(self, *, wait):
                pass

        def parse(item, path, settings, **kwargs):
            order.append(f"build:{item.rel}")
            return path.read_text()

        def link(settings, **kwargs):
            order.append("link")
            return {"done": [], "failures": []}

        with patch("publisher.ahead.LinkAhead", Linker), \
             patch.object(pipeline, "_parse", side_effect=parse), \
             patch.object(pipeline, "link_pending_isolated", side_effect=link):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(order, ["link-ahead:a_md.md", "build:b.md", "link-ahead:b_md.md", "link"])

    def test_a_sync_without_linking_publishes_what_it_built(self):
        self.add("a.md")
        self.settings.policy = "fast"  # linking off: the CLI also turns the linker switch off
        calls = []
        with patch.object(pipeline, "publish_only", side_effect=lambda settings, **kwargs: calls.append(kwargs) or {"done": [], "failures": []}), \
             patch.object(pipeline, "link_pending_isolated") as link:
            self.assertEqual(self.run_sync(), 0)
        link.assert_not_called()
        self.assertEqual(calls, [{"only": None, "allow_unlinked": True, "link_pending": False}])

    def test_unlinked_folder_requires_explicit_publication_opt_in(self):
        folder = self.project.wiki_dir("pending.md")
        (folder / "_planning").mkdir(parents=True)
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "pending"})
        document = folder.relative_to(self.project.wiki).as_posix()
        self.assertNotIn(document, pipeline._folders(self.project))
        self.assertIn(document, pipeline._folders(self.project, allow_unlinked=True))

    def test_first_pass_continues_final_retry_recovers_and_does_not_retry_success(self):
        for rel in ("a.md", "b.md", "c.md"):
            self.add(rel)
        calls = []
        def parse(item, path, settings, **kwargs):
            calls.append(item.rel)
            if item.rel == "b.md" and calls.count("b.md") == 1:
                raise RuntimeError("injected document failure private-test-secret")
            return path.read_text()
        with patch.object(pipeline, "_parse", side_effect=parse):
            self.assertEqual(self.run_sync(), 0)
        self.assertEqual(calls, ["a.md", "b.md", "c.md", "b.md"])
        self.assertEqual(set(load_ledger(self.project.metadata / "pipeline.json").sources), {"a.md", "b.md", "c.md"})
        self.assertEqual(queue.status(self.project), [])
        logs = list((self.base / "logs").rglob("*.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0].read_text())["attempt"], 1)
        for path in (self.base / "logs").rglob("*"):
            if path.is_file():
                self.assertNotIn("private-test-secret", path.read_text())
        self.assertIn("Traceback", logs[0].with_suffix(".log").read_text())

    def test_final_failure_retains_good_documents_and_retries_on_next_sync(self):
        for rel in ("a.md", "b.md", "c.md"):
            self.add(rel)
        calls = []
        def parse(item, path, settings, **kwargs):
            calls.append(item.rel)
            if item.rel == "b.md":
                raise RuntimeError("permanent failure")
            return path.read_text()
        with patch.object(pipeline, "_parse", side_effect=parse):
            self.assertEqual(self.run_sync(), 1)
        self.assertEqual(calls, ["a.md", "b.md", "c.md", "b.md"])
        self.assertEqual(set(load_ledger(self.project.metadata / "pipeline.json").sources), {"a.md", "c.md"})
        self.assertEqual([(row["rel"], row["status"]) for row in queue.status(self.project)], [("b.md", "failed")])
        self.assertEqual(len(list((self.base / "logs").rglob("*.json"))), 2)
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(queue.status(self.project), [])

    def test_writer_failure_is_also_deferred_and_candidate_is_not_promoted(self):
        for rel in ("a.md", "b.md", "c.md"):
            self.add(rel)
        calls = []
        def generate(project, raw_rel, **kwargs):
            calls.append(raw_rel)
            if raw_rel == "b_md.md":
                raise RuntimeError("writer failed")
            return self.generate(project, raw_rel, **kwargs)
        with patch.object(pipeline, "write_wiki_pages", side_effect=generate):
            self.assertEqual(self.run_sync(), 1)
        self.assertEqual(calls, ["a_md.md", "b_md.md", "c_md.md", "b_md.md"])
        self.assertFalse(self.project.raw_file("b_md.md").exists())
        self.assertFalse(self.project.wiki_dir("b_md.md").exists())

    def test_selected_paths_leave_other_failed_jobs_untouched(self):
        for rel in ("a.md", "b.md"):
            self.add(rel)
        queue.scan(self.settings, settle_seconds=0)
        jobs = queue.claim(self.project, "slow", only=["b.md"])
        queue.finish(self.project, jobs, error="unrelated")
        self.assertEqual(self.run_sync(["a.md"]), 0)
        self.assertEqual([(row["rel"], row["status"]) for row in queue.status(self.project)], [("b.md", "failed")])

    def test_failed_delete_does_not_block_unrelated_new_source(self):
        self.add("old.md")
        self.assertEqual(self.run_sync(), 0)
        (self.project.mount / "old.md").unlink()
        self.add("new.md")
        with patch.object(pipeline, "delete_sources", side_effect=RuntimeError("delete unavailable")):
            self.assertEqual(self.run_sync(), 1)
        sources = load_ledger(self.project.metadata / "pipeline.json").sources
        self.assertEqual(set(sources), {"old.md", "new.md"})
        self.assertEqual([(row["rel"], row["status"]) for row in queue.status(self.project)], [("old.md", "failed")])

    def test_omitted_claim_is_rejected_before_promotion(self):
        self.add("a.md")
        queue.scan(self.settings, settle_seconds=0)
        baseline = last_good(self.project)
        with patch.object(pipeline, "sync_once", return_value={"run_id": "fake", "done": [], "failures": [], "cancelled": False}), \
             patch.object(queue, "promote") as promote:
            result = queue.work_once(self.settings, isolated=True)
        promote.assert_not_called()
        self.assertIn("omitted", " ".join(result["failures"]))
        self.assertEqual(last_good(self.project), baseline)

    def test_generation_row_without_publication_evidence_is_rejected(self):
        self.add("a.md")
        queue.scan(self.settings, settle_seconds=0)
        with patch.object(pipeline, "sync_once", return_value={"run_id": "fake", "done": [{"path": "a.md", "status": "added"}], "failures": [], "cancelled": False}), \
             patch.object(queue, "promote") as promote:
            result = queue.work_once(self.settings, isolated=True)
        promote.assert_not_called()
        self.assertIn("source did not complete", " ".join(result["failures"]))

    def test_candidate_keep_preserves_original_exception(self):
        ensure_repository(self.project)
        with self.assertRaisesRegex(RuntimeError, "original"):
            with candidate(self.project, "exception-test", keep=True):
                raise RuntimeError("original")

    def test_newer_version_survives_older_failure(self):
        self.add("a.md")
        queue.scan(self.settings, settle_seconds=0)
        old = queue.claim(self.project, "slow", limit=1)
        (self.project.mount / "a.md").write_text("# Newer source")
        queue.scan(self.settings, settle_seconds=0, verify_content=True)
        queue.finish(self.project, old, error="older failed")
        self.assertEqual(queue.status(self.project)[0]["status"], "queued")
        self.assertGreater(queue.status(self.project)[0]["version"], old[0].version)

    def test_unreadable_scan_holds_deletions_and_continues_healthy_work(self):
        self.add("old.md")
        self.assertEqual(self.run_sync(), 0)
        (self.project.mount / "old.md").unlink()
        self.add("unreadable.md")
        self.add("good.md")
        original = queue.stage_blob
        def stage(project, path):
            if path.name == "unreadable.md":
                raise PermissionError("injected read failure")
            return original(project, path)
        with patch.object(queue, "stage_blob", side_effect=stage):
            self.assertEqual(self.run_sync(), 1)
        self.assertEqual(set(load_ledger(self.project.metadata / "pipeline.json").sources), {"old.md", "good.md"})
        self.assertFalse(any(row["operation"] == "delete" for row in queue.status(self.project)))

    def test_recovery_precedes_cleanup_without_continue(self):
        self.add("a.md")
        queue.scan(self.settings, settle_seconds=0)
        base = last_good(self.project)
        with candidate(self.project, "interrupted", keep=True) as staged:
            evidence = staged.metadata / "human-sync" / "keep-evidence.txt"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("prepared request evidence")
            prepared = commit_candidate(staged, "prepared evidence", {})
        queue._transaction(self.project, "interrupted", base, "publishing", prepared)
        def restore(settings, candidate_project, jobs, **kwargs):
            self.assertEqual(evidence.read_text(), "prepared request evidence")
            raise RuntimeError("unresolved remote revision")
        with patch.object(pipeline, "candidate_publication_complete", return_value=False), \
             patch.object(pipeline, "restore_publication", side_effect=restore):
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                queue.work_once(self.settings, isolated=True)
        self.assertTrue(evidence.exists())

    def test_failed_newer_parse_cannot_reuse_old_raw_text(self):
        self.add("a.md")
        self.assertEqual(self.run_sync(), 0)
        original = self.project.raw_file("a_md.md").read_text()
        (self.project.mount / "a.md").write_text("# Changed source")
        calls = []
        def failed(item, path, settings, **kwargs):
            calls.append(item.rel)
            raise RuntimeError("failed newer extraction")
        with patch.object(pipeline, "_parse", side_effect=failed):
            self.assertEqual(self.run_sync(), 1)
        self.assertEqual(calls, ["a.md", "a.md"])
        self.assertEqual(self.project.raw_file("a_md.md").read_text(), original)


if __name__ == "__main__":
    unittest.main()
