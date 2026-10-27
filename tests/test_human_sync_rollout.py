"""Offline acceptance tests for human-sync rollout phases 2 through 7."""

from __future__ import annotations

import asyncio
import json
import os
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from graph import config
from graph.config import HumanSyncMode, HumanSyncPolicy, Settings
from graph.growi.client import GrowiActivity, GrowiClient, GrowiPage, GrowiPublisher, wrap_page
from graph.wiki.storage import read_json, write_json_atomic
from graph.workspace import writer
from graph.workspace.project import Project
from publisher.activity import ActivityDetector
from publisher.human_changes import HumanStore, apply_generated
from graph.wiki.storage import write_text_atomic
from publisher.live_verification import LiveVerificationReport, verify_boundary_confirmation


BASE = "# Page\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n"
HUMAN = BASE.replace("40°C", "60°C")


class GrowiClientContractTest(unittest.TestCase):
    def test_update_uses_view_origin_to_enforce_revision_compare_and_swap(self):
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(201, json={
                "page": {
                    "_id": "p1", "path": "/docs/001", "status": "published",
                    "revision": {"_id": "r2", "body": "new"},
                },
            })

        client = GrowiClient(
            "https://growi.invalid", "token", transport=httpx.MockTransport(handler),
        )
        page = asyncio.run(client.update_page("p1", "r1", "new"))
        self.assertEqual(requests, [{
            "pageId": "p1", "revisionId": "r1", "body": "new", "origin": "view",
        }])
        self.assertEqual((page.revision_id, page.status), ("r2", "published"))

    def test_deleted_status_is_retained_for_remote_delete_classification(self):
        page = GrowiClient._page_from_payload({
            "page": {
                "_id": "p1", "path": "/trash/docs/001", "status": "deleted",
                "revision": {"_id": "r2", "body": "old"},
            },
        })
        self.assertEqual(page.status, "deleted")


class ConfigPolicyTest(unittest.TestCase):
    def test_default_values_validation_and_policy(self):
        self.assertEqual(Settings().human_sync_mode, HumanSyncMode.off)
        for value in ("off", "observe", "apply"):
            settings = Settings.model_validate({"human_sync_mode": value})
            self.assertEqual(settings.human_sync_mode.value, value)
            self.assertEqual(HumanSyncPolicy.resolve(value).captures, value == "apply")
        with self.assertRaisesRegex(ValueError, "human_sync_mode"):
            Settings.model_validate({"human_sync_mode": "unsafe"})
        with self.assertRaisesRegex(ValueError, "off, observe, apply"):
            HumanSyncPolicy.resolve("unsafe")

    def test_ini_overrides_environment_and_legacy_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mount"
            mount.mkdir()
            config.PROJECT_ROOT = root
            ini = root / "project.ini"
            ini.write_text(
                f"[project]\nsource_mount={mount}\ntarget_name=test\ndata_root=data\n"
                "[settings]\nhuman_sync_mode=apply\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WIKI_HUMAN_SYNC_MODE": "observe"}, clear=False):
                self.assertEqual(Settings.from_env(str(ini)).human_sync_mode, HumanSyncMode.apply)
            ini.write_text(
                f"[project]\nsource_mount={mount}\ntarget_name=test\ndata_root=data\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WIKI_HUMAN_SYNC_MODE", None)
                self.assertEqual(Settings.from_env(str(ini)).human_sync_mode, HumanSyncMode.off)


class ModePullTest(unittest.TestCase):
    def harness(self, mode: str, remote_body: str = HUMAN, *, revision: str = "r1"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Project(Path(temporary.name) / "project").ensure()
        project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
        folder = project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        write_text_atomic(folder / "001.md", BASE)
        apply_generated(project, "doc.md")
        store = HumanStore(project)
        row = {"marker_id": "b1", "page_id": "p1", "growi_path": "/docs/doc/001", "revision_id": "r0"}
        store.remember_page("doc/001.md", row, BASE, BASE, published=True)

        class Client:
            page = GrowiPage(page_id="p1", revision_id=revision, path="/docs/doc/001",
                             body=wrap_page(remote_body, page_id="b1"))

            async def get_page(self, **_kwargs):
                return self.page

        client = Client()
        publisher = GrowiPublisher(
            client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"),
            human_sync_policy=HumanSyncPolicy.resolve(mode),
        )
        return project, store, row, client, publisher

    def test_same_remote_replacement_in_all_modes(self):
        for mode in ("off", "observe", "apply"):
            with self.subTest(mode=mode):
                project, store, row, _client, publisher = self.harness(mode)
                before = (project.wiki / "doc/001.md").read_bytes()
                pulled, failures, _blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                if mode == "apply":
                    self.assertFalse(failures)
                    self.assertIn("60°C", (project.wiki / "doc/001.md").read_text(encoding="utf-8"))
                    self.assertEqual(publisher.human_sync_summary["captured"], 1)
                else:
                    self.assertFalse(pulled)
                    self.assertTrue(failures)
                    self.assertEqual((project.wiki / "doc/001.md").read_bytes(), before)
                    self.assertEqual(row["revision_id"], "r0")
                    self.assertIsNone(store.state("doc.md"))
                self.assertEqual(len(store.observations()), 0 if mode == "off" else 1)

    def test_only_apply_calls_the_model(self):
        from tests.test_human_changes import FakeModel

        for mode in ("off", "observe", "apply"):
            with self.subTest(mode=mode):
                project, _store, row, _client, publisher = self.harness(mode)
                fake = FakeModel(classes=[{"kind": "structure", "instruction": "tidy"}])
                publisher.human_model_factory = lambda _project: fake
                publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                self.assertEqual(sum(fake.calls.values()), 1 if mode == "apply" else 0)

    def test_a_transport_only_revision_is_not_a_human_change_in_any_mode(self):
        for mode in ("off", "observe", "apply"):
            with self.subTest(mode=mode):
                project, store, row, _client, publisher = self.harness(mode, remote_body=BASE + "\n\n")
                before = (project.wiki / "doc/001.md").read_bytes()
                pulled, failures, blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                self.assertEqual((pulled, failures, blocked), ([], [], set()))
                self.assertEqual((project.wiki / "doc/001.md").read_bytes(), before)
                self.assertEqual(row["revision_id"], "r1")
                self.assertIsNone(store.state("doc.md"))
                self.assertEqual(len(store.observations()), 0)

    def test_safety_failures_block_in_every_mode(self):
        for mode in ("off", "observe", "apply"):
            for case in ("missing", "moved", "marker"):
                with self.subTest(mode=mode, case=case):
                    project, _store, row, client, publisher = self.harness(mode)
                    if case == "missing":
                        client.page = None
                    elif case == "moved":
                        client.page = client.page.model_copy(update={"path": "/elsewhere"})
                    else:
                        client.page = client.page.model_copy(update={"body": HUMAN})
                    _pulled, failures, blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                    self.assertTrue(failures)
                    self.assertEqual(blocked, {"doc"})

    def test_the_same_blocked_revision_is_observed_after_switching_from_off_to_observe(self):
        project, store, row, client, off = self.harness("off")
        self.assertTrue(off.pull_changes(project, {"doc/001.md": row}, {"doc"})[1])
        observer = GrowiPublisher(client, off.connection, human_sync_policy=HumanSyncPolicy.resolve("observe"))
        failures = observer.pull_changes(project, {"doc/001.md": row}, {"doc"})[1]
        self.assertIn("current mode: observe", failures[0])
        self.assertEqual(len(store.observations()), 1)

    def test_a_page_moved_back_is_unblocked_without_a_new_revision(self):
        project, _store, row, client, publisher = self.harness("apply")
        original = client.page
        client.page = original.model_copy(update={"path": "/elsewhere"})
        self.assertTrue(publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})[1])
        client.page = original  # moved back with the same revision
        self.assertEqual(publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})[1], [])

    def test_an_interrupted_pull_leaves_no_blocked_page_behind(self):
        project, store, row, client, publisher = self.harness("apply")
        publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertEqual(row["revision_id"], "r1")
        captures = len(list((store._dir("doc.md") / "captures").glob("*.json")))
        row["revision_id"] = "r0"  # the process died after the page baseline was written, before the ledger was saved
        _pulled, failures, blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertEqual((failures, blocked, row["revision_id"]), ([], set(), "r1"))
        self.assertEqual(len(list((store._dir("doc.md") / "captures").glob("*.json"))), captures)
        self.assertEqual((project.wiki / "doc/001.md").read_text(encoding="utf-8").count("60°C"), 1)
        client.page = client.page.model_copy(update={"revision_id": "r2"})
        row["revision_id"] = "r-other"  # a ledger that matches neither the baseline nor the remote still blocks
        self.assertTrue(publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})[1])

    def test_observe_to_apply_refetches_same_revision_and_apply_off_apply_is_stable(self):
        project, store, row, client, observer = self.harness("observe")
        observer.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertEqual(len(store.observations()), 1)
        applier = GrowiPublisher(
            client, observer.connection, human_sync_policy=HumanSyncPolicy.resolve("apply")
        )
        pulled, failures, _ = applier.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertFalse(failures)
        self.assertTrue(pulled)
        self.assertEqual(store.status()["counts"]["human_information"], 1)
        write_text_atomic(project.wiki_dir("doc.md") / "001.md", BASE.replace("AUTO", "MANUAL"))
        apply_generated(project, "doc.md")
        off = GrowiPublisher(client, observer.connection, human_sync_policy=HumanSyncPolicy.resolve("off"))
        off.pull_changes(project, {"doc/001.md": row}, {"doc"})
        applier.pull_changes(project, {"doc/001.md": row}, {"doc"})
        text = (project.wiki / "doc/001.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("60°C"), 1)
        self.assertIn("MANUAL", text)

class ParallelPullTest(unittest.TestCase):
    def test_pages_are_classified_at_the_model_concurrency_and_captured_in_page_order(self):
        import threading
        import time

        names = ["001.md", "002.md", "003.md", "004.md"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Project(Path(temporary.name) / "project").ensure()
        project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
        folder = project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        pages = {name: BASE.replace("Page", f"Page {name}") for name in names}
        for name, text in pages.items():
            write_text_atomic(folder / name, text)
        apply_generated(project, "doc.md")
        store = HumanStore(project)
        rows = {}
        for name, text in pages.items():
            rows[f"doc/{name}"] = {"marker_id": f"b{name[:3]}", "page_id": f"p{name[:3]}",
                                   "growi_path": f"/docs/doc/{name}", "revision_id": "r0"}
            store.remember_page(f"doc/{name}", rows[f"doc/{name}"], text, text, published=True)

        class Client:
            async def get_page(self, *, page_id, **_kwargs):
                name = f"{page_id[1:]}.md"
                return GrowiPage(page_id=page_id, revision_id="r1", path=f"/docs/doc/{name}",
                                 body=wrap_page(pages[name].replace("40°C", "60°C"), page_id=f"b{page_id[1:]}"))

        class Model:
            concurrency = 3
            inflight = peak = 0
            lock = threading.Lock()

            def classify(self, _pairs):
                with self.lock:
                    self.inflight += 1
                    self.peak = max(self.peak, self.inflight)
                time.sleep(0.3)
                with self.lock:
                    self.inflight -= 1
                return []

        model = Model()
        publisher = GrowiPublisher(
            Client(), SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"),
            human_sync_policy=HumanSyncPolicy.resolve("apply"), human_model_factory=lambda _project: model,
        )
        pulled, failures, blocked = publisher.pull_changes(project, rows, {"doc"})
        self.assertEqual((failures, blocked), ([], set()))
        self.assertEqual(model.peak, 3)
        self.assertEqual(publisher.human_sync_summary["captured"], 4)
        self.assertEqual(pulled, [f"doc/{name}" for name in names])
        for name in names:
            self.assertIn("60°C", (project.wiki / "doc" / name).read_text(encoding="utf-8"))
            self.assertEqual(rows[f"doc/{name}"]["revision_id"], "r1")


class ActivityDetectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()

    def test_the_first_pull_of_a_fresh_project_checkpoints_its_cursor(self):
        from publisher import pipeline
        from publisher.history import candidate_is_clean, ensure_repository, last_good

        class Client:
            async def list_activities(self, *, offset=0, limit=100):
                return [], 0
            async def list_all_pages(self, _boundary):
                return []

        (Path(self.tmp.name) / "mount").mkdir()
        settings = SimpleNamespace(data_root=self.tmp.name, target_name="project",
                                   mount_path=str(Path(self.tmp.name) / "mount"), growi_url="https://growi")
        ensure_repository(self.project)
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
        with patch.object(pipeline, "_publisher", return_value=publisher):
            self.assertEqual(pipeline.pull_growi_once(settings)["failures"], [])
        self.assertTrue((self.project.metadata / "human-sync" / "activity-cursor.json").exists())
        self.assertTrue(candidate_is_clean(self.project, last_good(self.project)))

    def test_idle_poll_reads_no_page_bodies_and_same_timestamp_restart_is_idempotent(self):
        stamp = "2026-10-01T00:00:00+00:00"
        rows = [GrowiActivity(activity_id=f"a{i:03}", created_at=stamp, action="page.update", page_id="p1")
                for i in range(125)]

        class Client:
            body_reads = 0
            async def list_activities(self, *, offset=0, limit=100):
                return rows[offset:offset + limit], len(rows)
            async def get_page(self, **_kwargs):
                self.body_reads += 1
                raise AssertionError("detector hints must not fetch bodies")

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        batch = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertEqual(len(batch.events), 125)
        self.assertEqual(batch.selected_page_ids, {"p1"})
        self.assertEqual(batch.metrics["pages_fetched"], 0)
        detector.commit(batch)
        repeated = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertFalse(repeated.events)

    def test_official_activity_response_contract_is_parsed(self):
        payload = {"serializedPaginationResult": {"docs": [{
            "_id": "a1", "target": "p1", "targetModel": "Page", "action": "PAGE_UPDATE",
            "snapshot": {"pageId": "p1", "pagePath": "/docs/a"},
            "createdAt": "2026-10-01T00:00:00.000Z", "user": {"_id": "u1"},
        }], "totalDocs": 1}}

        class Response:
            def json(self): return payload

        client = GrowiClient("https://growi", "")
        async def request(*_args, **_kwargs): return Response()
        client._request = request
        rows, total = asyncio.run(client.list_activities())
        self.assertEqual(total, 1)
        self.assertEqual((rows[0].activity_id, rows[0].page_id, rows[0].path, rows[0].updated_by),
                         ("a1", "p1", "/docs/a", "u1"))

    def test_inventory_classifies_owned_foreign_and_duplicate_markers(self):
        pages = [
            GrowiPage(page_id="p1", revision_id="r2", path="/docs/a", body=""),
            GrowiPage(page_id="p2", revision_id="x", path="/docs/foreign", body="foreign"),
            GrowiPage(page_id="p3", revision_id="x", path="/docs/lost", body=wrap_page("ours", page_id="b1")),
        ]

        class Client:
            async def list_all_pages(self, _boundary): return pages
            async def get_page(self, *, page_id): return next(row for row in pages if row.page_id == page_id)

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        result = detector.inventory(Client(), {
            "doc/001.md": {"page_id": "p1", "revision_id": "r1", "growi_path": "/docs/a", "marker_id": "b1"}
        })
        kinds = [row["classification"] for row in result.classifications]
        self.assertIn("changed_owned", kinds)
        self.assertIn("unmanaged_foreign", kinds)
        self.assertIn("newly_discovered_owned", kinds)
        self.assertIn("duplicate_ownership", kinds)

    def test_reset_cursor_accepts_first_new_event_and_late_overlap_once(self):
        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs", overlap_seconds=60)
        write_json_atomic(detector.path, {
            "schema_version": 1, "endpoint_identity": detector.endpoint_identity,
            "last_processed_at": "2026-10-01T00:01:00+00:00", "ids_at_last_timestamp": [],
            "recent_ids": [], "last_sequence": None,
        })
        rows = [GrowiActivity(activity_id="late", created_at="2026-10-01T00:00:30+00:00",
                              action="page.update", page_id="p1")]

        class Client:
            async def list_activities(self, *, offset=0, limit=100): return rows[offset:offset + limit], len(rows)

        first = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertEqual([row.activity_id for row in first.events], ["late"])
        detector.commit(first)
        self.assertFalse(detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}}).events)

    def test_malformed_activity_and_permission_gap_force_inventory_without_advancing_cursor(self):
        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")

        class Malformed:
            async def list_activities(self, **_kwargs): raise ValueError("bad payload")

        malformed = detector.poll(Malformed(), {})
        self.assertEqual(malformed.fallback_reason, "malformed_activity_response")
        detector.commit(malformed)
        self.assertFalse(detector.path.exists())

        class DeniedError(RuntimeError):
            status_code = 403

        class Denied:
            async def list_activities(self, **_kwargs): raise DeniedError("denied")

        self.assertEqual(detector.poll(Denied(), {}).fallback_reason, "activity_permission_gap")

    def test_inventory_rejects_results_outside_boundary(self):
        class Client:
            async def list_all_pages(self, _boundary):
                return [GrowiPage(page_id="p1", revision_id="r1", path="/other/page")]

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        with self.assertRaisesRegex(ValueError, "outside"):
            detector.inventory(Client(), {})


class OperatorAndLiveGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()
        self.project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
        folder = self.project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, self.project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        write_text_atomic(folder / "001.md", BASE)
        apply_generated(self.project, "doc.md")
        self.store = HumanStore(self.project)
        row = {"marker_id": "b1", "page_id": "p1", "growi_path": "/docs/doc/001", "revision_id": "r0",
               "observed_revision_id": "r1"}
        self.store.remember_page("doc/001.md", row, BASE, BASE, published=True)
        self.store.accept("doc.md", "doc/001.md", "b1", "r1", BASE, HUMAN)
        baseline = self.store.page("b1")
        baseline["observed_revision"] = "r1"
        self.store.save_page(baseline)

    def test_status_is_redacted_and_reports_blocked_pages(self):
        self.store.block_page("doc/001.md", {"marker_id": "b1"}, "remote_moved")
        status = self.store.status()
        self.assertEqual(status["blocked"], [{"page": "doc/001.md", "reason": "remote_moved"}])
        self.assertEqual(status["counts"]["human_information"], 1)
        self.assertNotIn("60°C", json.dumps(status, ensure_ascii=False))

    def test_human_status_rejects_a_dirty_live_tree(self):
        from publisher.history import last_good
        from runner import cli

        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "empty-project").ensure()
            settings = SimpleNamespace()
            args = SimpleNamespace(human_command="status")
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 patch("builtins.print"):
                self.assertEqual(cli.cmd_human(args), 0)
            head = subprocess.run(
                ["git", "-C", str(project.root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(last_good(project), head)
            # Status writes nothing, so a second status must not create a checkpoint.
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 patch("builtins.print"):
                self.assertEqual(cli.cmd_human(args), 0)
            self.assertEqual(last_good(project), head)

            summary_path = project.metadata / "human-sync" / "dirty.json"
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text("dirty\n", encoding="utf-8")
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 self.assertRaisesRegex(RuntimeError, "dirty last-good"):
                cli.cmd_human(args)
            self.assertEqual(summary_path.read_text(encoding="utf-8"), "dirty\n")

    def test_live_report_is_local_redacted_and_requires_exact_boundary_confirmation(self):
        settings = SimpleNamespace(target_name="docs", growi_url="https://secret-token@example.test/growi",
                                   growi_mode="attach", human_sync_mode=HumanSyncMode.off)
        report = LiveVerificationReport.create(self.project, settings, "/docs/disposable/human-sync")
        body = report.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-token", body)
        with self.assertRaises(PermissionError):
            verify_boundary_confirmation(settings.growi_url, report.data["disposable_path"], "wrong")
        verify_boundary_confirmation(settings.growi_url, report.data["disposable_path"], report.data["confirmation_code"])
        self.assertTrue(all(row["status"] == "pending" for row in report.data["cases"].values()))

    def test_live_report_can_initialize_before_any_human_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "fresh-project").ensure()
            settings = SimpleNamespace(target_name="docs", growi_url="https://example.test/growi",
                                       growi_mode="attach", human_sync_mode=HumanSyncMode.off)
            LiveVerificationReport.create(project, settings, "/docs/disposable/report-first")
            store = HumanStore(project)
            self.assertEqual(read_json(store.root / "schema.json"), {"schema_version": 1})

    def test_an_old_report_without_the_new_cases_never_finalizes_as_complete(self):
        from publisher.live_verification import LIVE_CASES

        settings = SimpleNamespace(target_name="docs", growi_url="https://example.test/growi",
                                   growi_mode="attach", human_sync_mode=HumanSyncMode.off)
        report = LiveVerificationReport.create(self.project, settings, "/docs/disposable/old")
        self.assertEqual(len(LIVE_CASES), 62)
        old = {name: report.data["cases"][name] for name in LIVE_CASES[:41]}
        report.data["cases"] = old
        for name in LIVE_CASES[:41]:
            report.record_case(name, passed=True, initial_hashes={}, final_hashes={}, revisions={}, operation="",
                               expected="", actual="", calls={})
        reopened = LiveVerificationReport.open(report.path)
        self.assertEqual(len(reopened.data["cases"]), 41)  # not rewritten on open
        self.assertEqual(reopened.finalize(cleanup_status="done", recovery_possible=True)["status"], "incomplete")
        reopened.record_case("human_revert", passed=True, initial_hashes={}, final_hashes={}, revisions={},
                             operation="", expected="", actual="", calls={})
        self.assertEqual(reopened.data["cases"]["human_revert"]["status"], "passed")

    def test_live_report_records_constraints_without_claiming_a_pass(self):
        settings = SimpleNamespace(target_name="docs", growi_url="https://example.test/growi",
                                   growi_mode="attach", human_sync_mode=HumanSyncMode.off)
        report = LiveVerificationReport.create(self.project, settings, "/docs/disposable/constrained")
        report.record_constraint("activity_cursor_restart", reason_codes=["global_feed_disallowed"])
        self.assertEqual(report.data["cases"]["activity_cursor_restart"]["status"], "constrained")
        final = report.finalize(cleanup_status="not_started", recovery_possible=True)
        self.assertEqual(final["status"], "incomplete")
