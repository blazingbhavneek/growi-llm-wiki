"""Offline regressions for the AGG-1 human-sync findings; no live services."""
import asyncio
import json
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from graph.fast import inline_linker as linker
from graph.fast import repair, writer
from publisher import human_changes as hc


class Model:
    def __init__(self, result=None, *, kept=True):
        self.result, self.calls = result, 0
        self.kept, self.reviews = kept, []

    def merge(self, old, human, new, guidance, failure):
        self.calls += 1
        if self.result is None:
            raise ValueError("deliberate model failure")
        return {"edits": [{"find": new, "replace": self.result}]}

    def verify(self, *args, **kwargs):
        return hc.LlmHumanModel.verify(self, *args, **kwargs)

    def _ask(self, schema, system, data):
        self.reviews.append(data)
        return {"human_kept": self.kept, "source_kept": self.kept}


class FinalModel(hc.LlmHumanModel):
    def __init__(self, project, verdicts, rewrites=()):
        self.project = project
        self._local = threading.local()
        self.final_rewrite_attempts = len(rewrites)
        self.verdicts, self.rewrites = list(verdicts), list(rewrites)
        self.calls = []

    def _ask(self, schema, system, data, **kwargs):
        self.calls.append((schema, data, kwargs))
        if schema is hc._FinalVerdict:
            return self.verdicts.pop(0)
        if schema is hc._FinalRewrite:
            return self.rewrites.pop(0)
        raise AssertionError(schema)


class HumanSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = SimpleNamespace(metadata=root / "metadata", wiki=root / "wiki",
                                       wiki_dir=lambda _: root / "wiki" / "doc")
        self.store = hc.HumanStore(self.project)
        self.store.identity = lambda _: ("doc", {"source_id": "doc"})

    def seed(self, pages):
        doc = {"schema_version": 1, "source_id": "doc", "appendix": None, "guidance": {}}
        self.store._commit("input.md", doc, pages, {}, pure=pages)

    def rebase(self, old, human, new, model=None):
        pages, appendix = hc.rebase({"p.md": old}, {"p.md": human}, {"p.md": new}, model=model)
        self.assertEqual(appendix, [])
        return pages["p.md"]

    def test_both_paragraph_appends_and_valid_model_candidate(self):
        old, human, new = "Definition.\n", "Definition. Human addition.\n", "Definition. Source addition.\n"
        expected = "Definition. Human addition. Source addition.\n"
        self.assertEqual(self.rebase(old, human, new), expected)
        self.assertEqual(hc._attempt(Model(expected), old, human, new, [], "", Counter())[0], expected)
        with self.assertRaises(ValueError):
            hc._attempt(Model(expected.replace("Definition. ", ""), kept=False), old, human, new, [], "", Counter())

    def test_html_reflow_passes_but_missing_single_symbol_fails(self):
        old = "<table>\n<tr><td>Base</td></tr>\n</table>\n"
        human = old.replace("Base", "○")
        new = old.replace("</table>", "<tr><td>Source row</td></tr>\n</table>")
        valid = new.replace("Base", "○").replace("<tr>", "<tr>\n").replace("</tr>", "\n</tr>")
        self.assertEqual(hc._attempt(Model(valid), old, human, new, [], "", Counter())[0], valid)
        with self.assertRaises(ValueError):
            hc._attempt(Model(valid.replace("○", "×"), kept=False), old, human, new, [], "", Counter())

    def test_literal_findings_reach_existing_judge_without_another_merge(self):
        old = "共通、定義、説明。\n"
        human, new = old + "Human (QA-h-01).\n", old + "Source (QA-s-01).\n"
        result = "共通定義説明。\nHuman (QA-h-01).\nSource (QA-s-01).\n"
        for candidate, kept in ((result, True), (result.replace("(QA-h-01)", ""), False)):
            with self.subTest(kept=kept):
                model, stats = Model(candidate, kept=kept), Counter()
                if kept:
                    self.assertEqual(hc._attempt(model, old, human, new, [], "", stats)[0], candidate)
                else:
                    with self.assertRaisesRegex(ValueError, "verification failed"):
                        hc._attempt(model, old, human, new, [], "", stats)
                self.assertEqual((model.calls, len(model.reviews), stats["model_calls"]), (1, 1, 2))
                self.assertIn("、", str(model.reviews[0]["FINDINGS"]))
                self.assertEqual(model.reviews[0]["RESULT"], candidate)

    def test_final_document_judge_rewrites_then_approves(self):
        old, human, new = "Limit 10.\n", "Limit 12 (QA-human).\n", "Limit 15.\n"
        repaired = "Limit 12 (QA-human).\n（元文書の更新: Limit 15.）\n"
        model = FinalModel(
            self.project,
            verdicts=[
                {"approved": False, "changes": [{"id": "H0001", "status": "missing",
                                                   "reason": "human value is absent"}],
                 "defects": ["Restore the human value and record the source value."]},
                {"approved": True, "changes": [{"id": "H0001", "status": "conflict_recorded",
                                                  "page": "p.md", "evidence": "Limit 12 (QA-human).",
                                                  "reason": "both values are visible"}]},
            ],
            rewrites=[{"edits": [{"page": "p.md", "find": new, "replace": repaired}]}],
        )
        result = hc._finalize_pages(model, {"p.md": old}, {"p.md": human}, {"p.md": new}, {"p.md": new})
        self.assertEqual(result, {"p.md": repaired})
        self.assertEqual([call[2]["role"] for call in model.calls], ["judge", "writer", "judge"])
        self.assertEqual(model.calls[0][1]["NEW"], {"p.md": new})
        self.assertEqual(model.calls[0][1]["RESULT"], {"p.md": new})

    def test_twenty_eight_pages_have_isolated_calls_and_configured_parallelism(self):
        model = hc.LlmHumanModel(SimpleNamespace(concurrency=7, wiki_rewrite_concurrency=2), self.project)
        self.assertEqual(model.concurrency, 2)
        old = {f"{i:03d}.md": f"# Page {i}\nBody-only-{i:03d}. " + "context " * 1000 + "\nLimit 10.\n"
               for i in range(1, 29)}
        human, new = dict(old), dict(old)
        human["001.md"] = old["001.md"].replace("Limit 10.", "Limit 12 (QA-human).")
        new["001.md"] = old["001.md"].replace("Limit 10.", "Limit 15.")
        repaired = human["001.md"] + "（元文書の更新: Limit 15.）\n"
        barrier, lock = threading.Barrier(2), threading.Lock()
        calls, active, peak = [], 0, 0

        def ask(schema, system, data, **kwargs):
            nonlocal active, peak
            with lock:
                calls.append((schema, data))
                active += 1
                peak = max(peak, active)
                first_pair = len(calls) <= 2
            try:
                if first_pair:
                    barrier.wait(timeout=5)
                page = data["PAGE"]
                for name in ("OLD", "HUMAN", "NEW", "RESULT"):
                    self.assertEqual(set(data[name]), {page})
                payload = json.dumps(data)
                for other in old:
                    if other != page:
                        self.assertNotIn(f"Body-only-{int(other[:3]):03d}.", payload)
                self.assertLess(len(payload), sum(map(len, old.values())))
                if schema is hc._FinalRewrite:
                    self.assertEqual(page, "001.md")
                    return {"edits": [{"page": page, "find": new[page], "replace": repaired}]}
                missing = page == "001.md" and "QA-human" not in data["RESULT"][page]
                return {"approved": not missing, "changes": [
                    {"id": item["id"], "status": "missing" if missing else "conflict_recorded",
                     "page": page, "evidence": "Limit 12 (QA-human)."}
                    for item in data["OBLIGATIONS"]], "defects": ["Human value missing."] if missing else []}
            finally:
                with lock:
                    active -= 1

        with patch.object(model, "_ask", side_effect=ask):
            result = hc._finalize_pages(model, old, human, new, new)
        self.assertEqual(peak, 2)
        self.assertEqual(result, {**new, "001.md": repaired})
        self.assertEqual(Counter(data["PAGE"] for schema, data in calls if schema is hc._FinalVerdict),
                         Counter({page: 2 if page == "001.md" else 1 for page in old}))
        self.assertEqual([data["PAGE"] for schema, data in calls if schema is hc._FinalRewrite], ["001.md"])

    def test_page_loop_routes_moved_human_edit_to_appendix_without_copying_other_pages(self):
        old = {"p.md": "# Main\nOriginal page context.\n"}
        addition = "## Night rule\nMandatory human check (QA-moved).\n"
        human = {"p.md": old["p.md"] + addition}
        result = {"p.md": old["p.md"] + "The human night rule governs: [preserved edit](appendix.md).\n",
                  "appendix.md": "# Appendix\n" + addition}
        model = FinalModel(self.project, verdicts=[
            {"approved": True, "changes": [{"id": "H0001", "status": "preserved",
                "page": "appendix.md", "evidence": "Mandatory human check (QA-moved)."}]},
            {"approved": True, "changes": []},
        ])
        self.assertEqual(hc._finalize_pages(model, old, human, old, result), result)
        pages = {data["PAGE"]: data for _, data, _ in model.calls}
        self.assertEqual(pages["p.md"]["OBLIGATIONS"], [])
        self.assertEqual(pages["p.md"]["OUTGOING"][0]["destination"], "appendix.md")
        self.assertEqual(pages["appendix.md"]["OBLIGATIONS"][0]["after"], addition)
        self.assertNotIn("Original page context.", json.dumps(pages["appendix.md"]))

    def test_relocation_link_is_mandatory_after_every_rewrite(self):
        old = {"p.md": "# Main\nOriginal rule.\n"}
        addition = "Human night rule (QA-moved).\n"
        human = {"p.md": old["p.md"] + addition}
        result = {**old, "appendix.md": "# Appendix\n" + addition}
        mention = old["p.md"] + "Human night rule governs; see appendix.md.\n"
        linked = old["p.md"] + "Human night rule governs; see [preserved edit](appendix.md).\n"
        model = FinalModel(self.project, [], rewrites=[{}, {}])
        writers = []

        def ask(schema, system, data, **kwargs):
            model.calls.append((schema, data, kwargs))
            if schema is hc._FinalRewrite:
                writers.append(data)
                replacement = mention if len(writers) == 1 else linked
                return {"edits": [{"page": "p.md", "find": data["RESULT"]["p.md"], "replace": replacement}]}
            return {"approved": True, "changes": [
                {"id": item["id"], "status": "preserved", "page": data["PAGE"], "evidence": addition.strip()}
                for item in data["OBLIGATIONS"]]}

        with patch.object(model, "_ask", side_effect=ask):
            actual = hc._finalize_pages(model, old, human, old, result)
        self.assertEqual(actual, {**result, "p.md": linked})
        self.assertEqual(len(writers), 2)
        for data in writers:
            self.assertIn("Relocated edit H0001", str(data["MECHANICAL_ERRORS"]))
            self.assertIn("appendix.md", str(data["CURRENT_FINDINGS"]))
        self.assertEqual([kwargs["role"] for _, data, kwargs in model.calls if data["PAGE"] == "p.md"],
                         ["judge", "writer", "judge", "writer", "judge"])

    def test_relocation_link_must_be_visible_and_target_the_destination(self):
        old = {"p.md": "# Main\nOriginal rule.\n"}
        human = {"p.md": old["p.md"] + "Human rule (QA-moved).\n"}
        destination = "029-付録.md"
        for pointer, valid in [
            ("029-付録.md", False),
            ("[Other](wrong.md)", False),
            ("```text\n[Example](029-付録.md)\n```", False),
            ("`[Example](029-付録.md)`", False),
            ("<!-- [Hidden](029-付録.md) -->", False),
            ("Human rule governs; see [preserved edit](./029-%E4%BB%98%E9%8C%B2.md#night).", True),
        ]:
            with self.subTest(pointer=pointer):
                result = {"p.md": old["p.md"] + pointer + "\n", destination: human["p.md"]}
                model = FinalModel(self.project, [])

                def ask(schema, system, data, **kwargs):
                    return {"approved": True, "changes": [
                        {"id": item["id"], "status": "preserved", "page": data["PAGE"],
                         "evidence": "Human rule (QA-moved)."} for item in data["OBLIGATIONS"]]}

                with patch.object(model, "_ask", side_effect=ask):
                    if valid:
                        self.assertEqual(hc._finalize_pages(model, old, human, old, result), result)
                    else:
                        with self.assertRaisesRegex(ValueError, "Relocated edit H0001"):
                            hc._finalize_pages(model, old, human, old, result)

    def test_invalid_verdict_is_corrected_by_judge_without_rewriting_content(self):
        old, human = {"p.md": "Limit 10.\n"}, {"p.md": "Limit 12.\n"}
        invalid_rows = [
            [{"id": "H0001", "status": "deletion_preserved", "reason": "NEW deleted its old value."}],
            [{"id": "H0001", "status": "preserved", "page": "p.md", "evidence": "Absent quote"}],
            [],
            [{"id": "unknown", "status": "preserved", "page": "p.md", "evidence": "Limit 12."}],
            [{"id": "H0001", "status": "preserved", "page": "p.md", "evidence": "Limit 12."}] * 2,
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                calls = []

                class Port:
                    async def structured(port, schema, messages, **kwargs):
                        self.assertIs(schema, hc._FinalVerdict)
                        data = json.loads(messages[1].content.split('<DATA>\n')[1].rsplit('\n</DATA>', 1)[0])
                        calls.append(data)
                        return schema.model_validate({"approved": True, "changes": rows if len(calls) == 1 else [
                            {"id": "H0001", "status": "preserved", "page": "p.md", "evidence": "Limit 12."}]})

                model = hc.LlmHumanModel(SimpleNamespace(concurrency=1), self.project)
                model._port = Port()
                model.final_rewrite_attempts = 0
                self.assertEqual(hc._finalize_pages(model, old, human, old, human), human)
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[1]["VERDICT_ERRORS"])
                self.assertEqual(calls[1]["PREVIOUS_VERDICT"]["changes"],
                                 hc._FinalVerdict.model_validate({"changes": rows}).model_dump()["changes"])
                self.assertEqual(calls[0]["RESULT"], calls[1]["RESULT"])

    def test_persistent_invalid_verdict_blocks_before_content_writer(self):
        calls = []

        class Port:
            async def structured(port, schema, messages, **kwargs):
                calls.append(schema)
                return schema.model_validate({"approved": True, "changes": [{
                    "id": "H0001", "status": "deletion_preserved", "reason": "Source deleted its old value."}]})

        model = hc.LlmHumanModel(SimpleNamespace(concurrency=1), self.project)
        model._port = Port()
        with self.assertRaisesRegex(ValueError, "invalid judge verdict.*unevidenced deletion"):
            hc._finalize_pages(model, {"p.md": "Limit 10.\n"}, {"p.md": "Limit 12.\n"},
                               {"p.md": "Limit 10.\n"}, {"p.md": "Limit 12.\n"})
        self.assertEqual(calls, [hc._FinalVerdict, hc._FinalVerdict])

    def test_page_loop_uses_the_old_counterpart_after_a_page_rename(self):
        old, human, new = "Limit 10.\n", "Limit 12.\n", "Limit 15.\n"
        result = human + "（元文書の更新: Limit 15.）\n"
        model = FinalModel(self.project, verdicts=[{
            "approved": True, "changes": [{"id": "H0001", "status": "conflict_recorded",
                "page": "002-Page.md", "evidence": "Limit 12."}],
        }])
        actual = hc._finalize_pages(model, {"001-Page.md": old}, {"001-Page.md": human},
                                   {"002-Page.md": new}, {"002-Page.md": result})
        self.assertEqual(actual, {"002-Page.md": result})
        self.assertEqual(model.calls[0][1]["OLD"], {"002-Page.md": old})
        self.assertEqual(model.calls[0][1]["HUMAN"], {"002-Page.md": human})

    def test_missing_moved_edit_reaches_destination_mechanical_checks_and_writer(self):
        old = {"p.md": "# Main\nOriginal page context.\n"}
        addition = "## Night rule\nMandatory human check (QA-moved).\n"
        human = {"p.md": old["p.md"] + addition}
        new = {"q.md": "# Other\nUnrelated source.\n"}
        result = {**new, "appendix.md": "# Appendix\n"}
        model = FinalModel(self.project, verdicts=[
            {"approved": False, "changes": [{"id": "H0001", "status": "missing"}]},
            {"approved": True, "changes": [{"id": "H0001", "status": "preserved",
                "page": "appendix.md", "evidence": "Mandatory human check (QA-moved)."}]},
            {"approved": True, "changes": []},
        ], rewrites=[{"edits": [{"page": "appendix.md", "find": "# Appendix\n",
                                  "replace": "# Appendix\n" + addition}]}])
        actual = hc._finalize_pages(model, old, human, new, result)
        self.assertEqual(actual, {**new, "appendix.md": "# Appendix\n" + addition})
        self.assertIn("QA-moved", str(model.calls[0][1]["FINDINGS"]))
        self.assertEqual(model.calls[1][1]["PAGE"], "appendix.md")

    def test_page_writer_cannot_patch_another_page_and_failure_rolls_back_all_pages(self):
        self.seed({"p.md": "Limit 10.\n", "q.md": "Other page.\n"})
        self.store.accept("input.md", "doc/p.md", "p", "human", "Limit 10.\n", "Limit 12.\n")
        before = self.store._load("input.md")
        model = FinalModel(self.project, verdicts=[], rewrites=[{}])

        def ask(schema, system, data, **kwargs):
            model.calls.append((schema, data, kwargs))
            if schema is hc._FinalRewrite:
                return {"edits": [{"page": "q.md", "find": "Other page.", "replace": "Overwritten."}]}
            if data["PAGE"] == "q.md":
                return {"approved": True, "changes": []}
            return {"approved": False, "changes": [{"id": "H0001", "status": "missing"}]}

        with patch.object(model, "_ask", side_effect=ask), self.assertRaisesRegex(ValueError, "unknown page"):
            self.store.generate("input.md", {"p.md": "Limit 15.\n", "q.md": "Other page.\n"}, model=model)
        self.assertEqual(self.store._load("input.md"), before)
        for page, text in before[2].items():
            self.assertEqual((self.project.wiki_dir("") / page).read_text(), text)

    def test_final_structured_calls_do_not_inherit_the_fast_default_output_cap(self):
        from graph.fast.model import FastModelPort, _OutputCappedLlm

        config = SimpleNamespace(chat_model="test", chat_base_url="unused", temperature=0)
        llm = Mock()
        port = FastModelPort(config, llm=llm)
        with patch("graph.clients.chat.structured_ainvoke", new_callable=AsyncMock) as invoke:
            asyncio.run(port.structured(hc._FinalVerdict, []))
            self.assertIs(invoke.await_args.args[0], llm)
            self.assertIsNone(invoke.await_args.kwargs["max_output_tokens"])
            asyncio.run(port.structured(hc._FinalVerdict, [], max_output_tokens=1024))
            self.assertIsInstance(invoke.await_args.args[0], _OutputCappedLlm)
            self.assertEqual(invoke.await_args.args[0].limit, 1024)

    def test_page_judge_writer_and_rejudge_share_one_event_loop(self):
        loops, lock = {}, threading.Lock()

        class Port:
            async def structured(self, schema, messages, **kwargs):
                data = json.loads(messages[1].content.split("<DATA>\n", 1)[1].rsplit("\n</DATA>", 1)[0])
                page = data["PAGE"]
                with lock:
                    loops.setdefault(page, []).append(asyncio.get_running_loop())
                if schema is hc._FinalRewrite:
                    return schema(edits=[hc._FinalEdit(page=page, find="Limit 10.\n", replace="Limit 12.\n")])
                missing = page == "p.md" and "12" not in data["RESULT"][page]
                return schema.model_validate({"approved": not missing, "changes": [
                    {"id": item["id"], "status": "missing" if missing else "preserved",
                     "page": page, "evidence": "Limit 12."} for item in data["OBLIGATIONS"]]})

        model = hc.LlmHumanModel(SimpleNamespace(concurrency=2), self.project)
        model._port = Port()
        old = {"p.md": "Limit 10.\n", "q.md": "Other page.\n"}
        human = {**old, "p.md": "Limit 12.\n"}
        self.assertEqual(hc._finalize_pages(model, old, human, old, old), human)
        self.assertEqual({page: len(items) for page, items in loops.items()}, {"p.md": 3, "q.md": 1})
        self.assertTrue(all(len(set(items)) == 1 for items in loops.values()))
        self.assertIsNot(loops["p.md"][0], loops["q.md"][0])
        self.assertTrue(all(loop.is_closed() for items in loops.values() for loop in items))

    def test_every_final_round_combines_mechanical_and_llm_feedback(self):
        old = '<table>\n<tr><td>10</td><td>5</td></tr>\n<tr><td>C</td><td>D</td></tr>\n</table>\n'
        human, new = old.replace('>10<', '>12 (QA-human)<'), old.replace('>5<', '>6<')
        good = human.replace('>5<', '>6<')
        bad = new.replace('<td>D</td>', '').replace('>10<', '>10（元文書の更新: ---）<')
        first = good.replace('<td>D</td>', '')
        approved = {"approved": True, "changes": [{"id": "H0001", "status": "preserved",
                    "page": "p.md", "evidence": "12 (QA-human)"}]}
        model = FinalModel(self.project, verdicts=[
            {"approved": False, "changes": [], "defects": ["Wrong semantic location."]},
            approved, approved,
        ], rewrites=[
            {"edits": [{"page": "p.md", "find": bad, "replace": first}]},
            {"edits": [{"page": "p.md", "find": first, "replace": good}]},
        ])
        result = hc._finalize_pages(model, {"p.md": old}, {"p.md": human}, {"p.md": new}, {"p.md": bad})
        self.assertEqual(result, {"p.md": good})
        self.assertEqual([call[2]["role"] for call in model.calls], ["judge", "writer", "judge", "writer", "judge"])
        initial_judge, first_writer, second_judge, second_writer, final_judge = [call[1] for call in model.calls]
        self.assertIn("12", str(initial_judge["FINDINGS"]))
        for message in ("ragged", "source-update note"):
            self.assertIn(message, str(initial_judge["MECHANICAL_ERRORS"]))
            self.assertIn(message, str(first_writer["CURRENT_FINDINGS"]))
        self.assertIn("Wrong semantic location.", first_writer["CURRENT_FINDINGS"])
        self.assertIn("ragged", str(second_judge["MECHANICAL_ERRORS"]))
        self.assertIn("ragged", str(second_writer["CURRENT_FINDINGS"]))
        self.assertEqual(final_judge["MECHANICAL_ERRORS"], [])

    def test_final_judge_can_waive_cosmetic_suspicions(self):
        old = "共通、定義、説明。\n"
        human, new = old + "Human addition.\n", old + "Source addition.\n"
        result = "共通定義説明。\nHuman addition.\nSource addition.\n"
        model = FinalModel(self.project, verdicts=[{
            "approved": True, "changes": [{"id": "H0001", "status": "preserved",
                "page": "p.md", "evidence": "Human addition."}],
        }])
        self.assertEqual(hc._finalize_pages(model, {"p.md": old}, {"p.md": human}, {"p.md": new},
                                           {"p.md": result}), {"p.md": result})
        self.assertIn("、", str(model.calls[0][1]["FINDINGS"]))
        self.assertEqual(len(model.calls), 1)

    def test_final_checks_run_without_human_obligations_and_do_not_waive_damage(self):
        old = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>\n"
        bad = old.replace("<td>D</td>", "") + "```text\nUnclosed.\n"
        model = FinalModel(self.project, verdicts=[{"approved": True, "changes": []}])
        with self.assertRaisesRegex(ValueError, "ragged"):
            hc._finalize_pages(model, {"p.md": old}, {"p.md": old}, {"p.md": old}, {"p.md": bad})
        self.assertIn("unclosed code fence", str(model.calls[0][1]["MECHANICAL_ERRORS"]))

    def test_final_patch_error_is_reported_to_the_next_writer(self):
        old, human = "Limit 10.\n", "Limit 12.\n"
        verdict = {"approved": False, "changes": [{"id": "H0001", "status": "missing"}]}
        model = FinalModel(self.project, verdicts=[verdict, verdict, {
            "approved": True, "changes": [{"id": "H0001", "status": "preserved",
                "page": "p.md", "evidence": "Limit 12."}],
        }], rewrites=[
            {"edits": [{"page": "p.md", "find": "Absent target", "replace": human}]},
            {"edits": [{"page": "p.md", "find": old, "replace": human}]},
        ])
        self.assertEqual(hc._finalize_pages(model, {"p.md": old}, {"p.md": human}, {"p.md": old},
                                           {"p.md": old}), {"p.md": human})
        self.assertIn("occur exactly once", str(model.calls[3][1]["CURRENT_FINDINGS"]))

    def test_window_judge_sees_all_mechanical_errors_before_rejection(self):
        old = '<table>\n<tr><td>10</td><td>5</td></tr>\n<tr><td>C</td><td>D</td></tr>\n</table>\n'
        human, new = old.replace('>10<', '>12<'), old.replace('>5<', '>6<')
        bad = human.replace('>12<', '>12（元文書の更新: 6）<').replace('>5<', '>6<').replace('<td>D</td>', '')
        model = Model(bad)
        with self.assertRaisesRegex(ValueError, "ragged"):
            hc._attempt(model, old, human, new, [], "", Counter())
        self.assertEqual(len(model.reviews), 1)
        self.assertIn("ragged", str(model.reviews[0]["FINDINGS"]))
        self.assertIn("source-update note", str(model.reviews[0]["FINDINGS"]))

    def test_reset_captures_human_edits_before_deleting_remote_pages(self):
        from publisher import pipeline
        from publisher.ledger import Ledger, load_ledger, save_ledger

        old, human = "Original.\n", "Original. Human rule (QA-cx).\n"
        self.seed({"p.md": old})
        self.project.root = self.project.metadata.parent
        ledger = Ledger({}, {"doc": {}}, {"doc/p.md": {"page_id": "p", "revision_id": "old"}})
        save_ledger(self.project.metadata / "pipeline.json", ledger)
        events = []

        def pull(project, pages, unchanged):
            events.append("capture")
            self.store.accept("input.md", "doc/p.md", "p", "human", old, human)
            pages["doc/p.md"]["revision_id"] = "human"
            return ["doc/p.md"], [], set()

        def reset(**kwargs):
            events.append("delete")
            self.assertEqual(self.store._load("input.md")[2]["p.md"], human)
            return 1

        publisher = SimpleNamespace(pull_changes=Mock(side_effect=pull), reset=Mock(side_effect=reset))
        with patch.object(pipeline, "open_project", return_value=self.project), \
             patch.object(pipeline, "_publisher", return_value=publisher), \
             patch("publisher.index.delete_index_pages"):
            result = pipeline.reset_growi(SimpleNamespace())
        self.assertEqual(result["failures"], [])
        self.assertEqual(events, ["capture", "delete"])
        self.assertEqual(load_ledger(self.project.metadata / "pipeline.json").published_pages, {})
        self.assertEqual(self.store._load("input.md")[2]["p.md"], human)

    def test_reset_stops_before_any_deletion_when_capture_fails(self):
        from publisher import pipeline
        from publisher.ledger import Ledger, load_ledger, save_ledger

        self.seed({"p.md": "Original.\n"})
        self.project.root = self.project.metadata.parent
        pages = {"doc/p.md": {"page_id": "p", "revision_id": "old"}}
        save_ledger(self.project.metadata / "pipeline.json", Ledger({}, {"doc": {}}, pages))
        publisher = SimpleNamespace(
            pull_changes=Mock(return_value=([], ["doc/p.md: capture failed"], {"doc"})), reset=Mock())
        with patch.object(pipeline, "open_project", return_value=self.project), \
             patch.object(pipeline, "_publisher", return_value=publisher), \
             patch("publisher.index.delete_index_pages") as delete_index:
            result = pipeline.reset_growi(SimpleNamespace())
        self.assertTrue(result["failures"])
        publisher.reset.assert_not_called()
        delete_index.assert_not_called()
        self.assertEqual(load_ledger(self.project.metadata / "pipeline.json").published_pages, pages)

    def test_reset_rechecks_captured_revisions_before_deletion(self):
        from graph.growi.client import GrowiPage, GrowiPublisher, wrap_page

        page = GrowiPage(page_id="p", revision_id="late-human-edit", path="/wiki/doc/p.md",
                         body=wrap_page("Late human rule.\n", page_id="p"))
        client = SimpleNamespace(list_all_pages=AsyncMock(return_value=[page]),
                                 get_page=AsyncMock(return_value=page), delete_pages=AsyncMock())
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/wiki"))
        with self.assertRaisesRegex(RuntimeError, "changed before deletion"):
            publisher.reset(known_pages={"doc/p.md": {"page_id": "p", "revision_id": "captured"}})
        client.delete_pages.assert_not_called()

    def test_real_pull_preserves_a_pending_rule_through_reset(self):
        from graph.growi.client import GrowiPage, GrowiPublisher, wrap_page
        from publisher import pipeline
        from publisher.ledger import Ledger, save_ledger

        old, human = "Original.\n", "Original. Mandatory human rule (QA-cx).\n"
        self.seed({"p.md": old})
        self.project.root = self.project.metadata.parent
        hc.write_json_atomic(self.project.wiki_dir("") / "_planning" / "source.json", {"raw": "input.md"})
        row = {"page_id": "p", "revision_id": "old", "growi_path": "/wiki/doc/p.md", "marker_id": "p"}
        self.store.remember_page("doc/p.md", row, old, old, published=True)
        save_ledger(self.project.metadata / "pipeline.json", Ledger({}, {"doc": {}}, {"doc/p.md": row}))
        page = GrowiPage(page_id="p", revision_id="human", path="/wiki/doc/p.md",
                         body=wrap_page(human, page_id="p"))

        async def delete(pages):
            self.assertEqual(pages, {"p": "human"})
            self.assertEqual(self.store._load("input.md")[2]["p.md"], human)

        client = SimpleNamespace(get_page=AsyncMock(return_value=page),
            list_all_pages=AsyncMock(return_value=[page]), delete_pages=AsyncMock(side_effect=delete))
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/wiki"))
        with patch.object(pipeline, "open_project", return_value=self.project), \
             patch.object(pipeline, "_publisher", return_value=publisher), \
             patch.object(hc.HumanStore, "identity", return_value=("doc", {"source_id": "doc"})), \
             patch("publisher.index.delete_index_pages"):
            result = pipeline.reset_growi(SimpleNamespace())
        self.assertEqual(result["failures"], [])
        client.delete_pages.assert_awaited_once()
        self.assertEqual(len(list((self.store._dir("input.md") / "captures").glob("*.json"))), 1)
        self.assertEqual(self.store._load("input.md")[2]["p.md"], human)

    def test_final_mechanical_rejection_rolls_back_and_judge_receives_metadata(self):
        old = '<table>\n<tr><td>10</td><td>5</td></tr>\n<tr><td>C</td><td>D</td></tr>\n</table>\n'
        human, new = old.replace('>10<', '>12 (QA-human)<'), old.replace('>5<', '>6<')
        expected = human.replace('>5<', '>6<')
        self.seed({"p.md": old})
        self.store.accept("input.md", "doc/p.md", "p", "human", old, human)
        before = self.store._load("input.md")
        metadata = {"coverage.json": {"file_count": 1}, "metadata.json": {"files": ["p.md"]}}
        for name, data in metadata.items():
            hc.write_json_atomic(self.project.wiki_dir("") / "_planning" / name, data)
        model = FinalModel(self.project, verdicts=[
            {"approved": False, "changes": [], "defects": ["Clarify the section."]},
            {"approved": True, "changes": [{"id": "H0001", "status": "preserved",
                "page": "p.md", "evidence": "12 (QA-human)"}]},
        ], rewrites=[{"edits": [{"page": "p.md", "find": expected,
                                  "replace": expected.replace("<td>D</td>", "")}]}])
        with self.assertRaisesRegex(ValueError, "ragged"):
            self.store.generate("input.md", {"p.md": new}, model=model)
        self.assertEqual(self.store._load("input.md"), before)
        self.assertEqual((self.project.wiki_dir("") / "p.md").read_text(), human)
        self.assertEqual(model.calls[0][1]["DOCUMENT_METADATA"], metadata)

    def test_failed_final_document_judge_keeps_previous_pages(self):
        old, human, new = "Limit 10.\n", "Limit 12 (QA-human).\n", "Limit 15.\n"
        self.seed({"p.md": old})
        self.store.accept("input.md", "doc/p.md", "p", "human", old, human)
        before = self.store._load("input.md")[2]
        model = FinalModel(self.project, verdicts=[{
            "approved": False,
            "changes": [{"id": "H0001", "status": "uncertain", "reason": "cannot verify"}],
            "defects": ["The merge cannot be verified."],
        }])
        with self.assertRaisesRegex(ValueError, "final merge verification failed"):
            self.store.generate("input.md", {"p.md": new}, model=model)
        self.assertEqual(self.store._load("input.md")[2], before)
        self.assertEqual((self.project.wiki_dir("") / "p.md").read_text(), before["p.md"])

    def test_large_merge_window_reaches_model(self):
        prefix = "Unchanged context.\n" * 1800
        old, human, new = prefix + "Limit 10.\n", prefix + "Limit 15.\n", prefix + "Limit 20.\n"
        candidate = prefix + "Limit 15.\n（元文書の更新: 20）\n"
        model, stats = Model(candidate), Counter()
        self.assertEqual(hc._merge_window(model, old, human, new, [], stats)[0], candidate)
        self.assertEqual(model.calls, 1)
        self.assertGreater(len(old) + len(human) + len(new), 30000)

    def test_fewer_table_rows_reach_judge_without_another_rewrite(self):
        source = "| Item | Verdict |\n|---|---|\n| A | ○ |\n| | |\n"
        draft = "<table><tr><th>Item</th><th>Verdict</th></tr><tr><td>A</td><td>○</td></tr></table>\n"
        page = SimpleNamespace(number=1, filename="p.md", title="判定", owner_ranges=[(1, 4)])
        prompt = SimpleNamespace(number=1, filename="p.md", images=[], has_initial_candidate=False,
                                 owner_evidence=source, reference_evidence="", shell=repair._PageShell("判定", "", ""))
        session = writer._GenerationSession(page, prompt, [], Path("unused"), source)
        judge = SimpleNamespace(structured=AsyncMock(return_value=repair.RepairJudgeResult(
            approved=True, coverage_score=100, notes="Only a semantically empty row was removed.")))
        with patch.object(writer, "_writer", AsyncMock(return_value=("判定", draft))) as write:
            outcome = asyncio.run(writer._generate_page(
                session, lines=source.splitlines(), model=judge,
                config=SimpleNamespace(policy="fast", write_attempts=4), stop_check=None, on_progress=None))
        self.assertEqual((write.await_count, judge.structured.await_count, outcome.attempts), (1, 1, 1))
        self.assertEqual(outcome.body, draft)
        messages = judge.structured.call_args.args[1]
        self.assertIn("表の行数が少ない", messages[-1].content)
        self.assertIn(source, messages[-1].content)
        self.assertIn(draft, messages[-1].content)

    def test_failed_model_keeps_both_rows_and_bounds_the_conflict_note(self):
        old = '# Page\n\n<table>\n<tr><td rowspan="2">Group</td><td>10</td></tr>\n<tr><td>End</td></tr>\n</table>\n\n## Other\nUnchanged prose.\n'
        human = old.replace(">10<", ">15<").replace("<tr><td>End", "<tr><td>Human row</td></tr>\n<tr><td>End")
        new = old.replace(">10<", ">20<").replace("<tr><td>End", "<tr><td>Source row</td></tr>\n<tr><td>End")
        model = Model()
        result = self.rebase(old, human, new, model)
        self.assertEqual(model.calls, 2)
        self.assertIn('rowspan="4"', result)
        self.assertIn("<td>Human row</td>", result)
        self.assertIn("<td>Source row</td>", result)
        self.assertIn("<td>15</td>", result)
        self.assertEqual(result.count(hc.NOTE), 1)
        self.assertIn("（元文書の更新: 20）", result)
        self.assertEqual(result.count("Unchanged prose."), 1)
        self.assertLess(result.index("</table>"), result.index(hc.NOTE))
        for formatted in (new.replace("<tr>", "<tr>\n  ").replace("</tr>", "\n</tr>"),
                          new.replace("<td>", "<td>\n").replace("</td>", "\n</td>")):
            result = self.rebase(old, human, formatted, Model())
            self.assertTrue(all(value in result for value in ('rowspan="4"', "Human row", "Source row")))
            self.assertEqual(result.count(hc.NOTE), 1)

    def test_rowspan_interior_deletion_and_pre_adjusted_spans(self):
        old = '<table>\n<tr><td rowspan="3">Group</td><td>A</td></tr>\n<tr><td>B</td></tr>\n<tr><td>C</td></tr>\n</table>\n'
        human = old.replace("<tr><td>B</td></tr>\n", "")
        self.assertEqual(self.rebase(old, human, old), human)
        self.assertIn('rowspan="2"', hc._rowspans(old, human))
        human = old.replace("<tr><td>C</td></tr>\n", "")
        self.assertIn('rowspan="2"', hc._rowspans(old, human))
        human = old.replace('rowspan="3"', 'rowspan="4"').replace("<tr><td>B", "<tr><td>Human</td></tr>\n<tr><td>B")
        new = old.replace('rowspan="3"', 'rowspan="4"').replace("<tr><td>C", "<tr><td>Source</td></tr>\n<tr><td>C")
        self.assertIn('rowspan="5"', self.rebase(old, human, new))

    def test_rowspan_counts_rows_added_after_last_original_row(self):
        old = '<table>\n<tr><td rowspan="3">Group</td><td>A</td></tr>\n<tr><td>B</td></tr>\n<tr><td>C</td></tr>\n</table>\n'
        human = old.replace("</table>", "<tr><td>Human row</td></tr>\n</table>")
        new = old.replace('rowspan="3"', 'rowspan="4"').replace("</table>", "<tr><td>Source row</td></tr>\n</table>")
        result = self.rebase(old, human, new)
        self.assertIn('rowspan="5"', result)
        self.assertIn("Human row", result)
        self.assertIn("Source row", result)
        hc._check_tables(result, old + human + new)

    def test_dismissed_note_stays_gone_until_source_fact_changes(self):
        for table in (False, True):
            with self.subTest(table=table):
                wrap = (lambda s: "<table>\n<tr><td>" + s + "</td></tr>\n</table>\n") if table else (lambda s: s + "\n")
                old, human, new = wrap("Limit 10; interval 5."), wrap("Limit 15; interval 5."), wrap("Limit 20; interval 5.")
                self.seed({"p.md": old})
                self.store.accept("input.md", "doc/p.md", "p", "1", old, human)
                self.store.generate("input.md", {"p.md": new})
                current = self.store._load("input.md")[2]["p.md"]
                self.assertEqual(current.count(hc.NOTE), 1)
                self.store.accept("input.md", "doc/p.md", "p", "2", current, human)
                for interval in (6, 7):
                    new = wrap(f"Limit 20; interval {interval}.")
                    self.store.generate("input.md", {"p.md": new})
                    current = self.store._load("input.md")[2]["p.md"]
                    self.assertEqual(current, wrap(f"Limit 15; interval {interval}."))
                new = wrap("Limit 25; interval 7.")
                self.store.generate("input.md", {"p.md": new})
                self.assertEqual(self.store._load("input.md")[2]["p.md"].count(hc.NOTE), 1)
                new = wrap("Limit 15; interval 7.")
                self.store.generate("input.md", {"p.md": new})
                self.assertEqual(self.store._load("input.md")[2]["p.md"], new)
                model = Model()
                self.assertFalse(self.store.generate("input.md", {"p.md": new}, model=model).changed_pages)
                self.assertEqual(model.calls, 0)

    def test_source_only_changes_land_in_body_and_deletions_stay_deleted(self):
        result = self.rebase("Alpha 10.\nOld intro.\nDelete me.\n", "Alpha 15.\nOld intro.\nDelete me.\n", "Alpha 20.\nNew intro.\n")
        self.assertIn("Alpha 15.\nNew intro.\n", result)
        self.assertNotIn("Delete me", result)
        self.assertNotIn("Old intro", result)
        self.assertEqual(result.count(hc.NOTE), 1)
        old = '<table>\n<tr><td rowspan="2">Group</td><td>A</td></tr>\n<tr><td>B</td></tr>\n</table>\n'
        kept = hc._rowspans(old, hc._fallback(old, old.replace(">A<", ">Human<"), ""))
        hc._check_tables(kept, old)
        self.assertIn("Human", kept)
        self.assertNotIn("<td>B</td>", kept)

    def test_source_section_deletion_moves_contained_human_edit_to_appendix(self):
        old = "# Page\nIntro.\n## Removed\nAlpha.\nBeta.\nGamma.\nDelta.\n## End\nTail.\n"
        human = old.replace("Gamma.\n", "Gamma.\n\nHuman annotation.\n")
        new = "# Page\nSource summary.\n## End\nTail.\n"
        pages, entries = hc.rebase({"p.md": old}, {"p.md": human}, {"p.md": new})
        self.assertEqual(pages["p.md"], new)
        self.assertNotIn("Alpha.", pages["p.md"])
        self.assertEqual(len(entries), 1)
        self.assertIn("Human annotation.", entries[0])
        self.assertIn("この部分は削除されました", entries[0])

    def test_appendix_registration_reuses_navigation_without_inventing_source_coverage(self):
        pages = {"028-Last.md": "# Last\n\nBody.\n\n---\n\n親: [Root](001-Root.md) ｜ 前のページ: [Before](027-Before.md)\n",
                 "029-付録.md": "# 付録\n\nHuman memo.\n"}
        doc = {"appendix": "029-付録.md"}
        coverage_path = self.project.wiki_dir("") / "_planning" / "coverage.json"
        source = {"filename": "付録.md", "source_start": 1, "source_end": 4}
        hc.write_json_atomic(coverage_path, {"file_count": 1, "files": [source]})
        metadata = self.store._publication("input.md", doc, pages)
        self.assertIn("親: [Root](001-Root.md) ｜ 前のページ: [Before](027-Before.md) ｜ 次のページ: [付録](029-付録.md)", pages["028-Last.md"])
        self.assertTrue(pages["029-付録.md"].endswith("前のページ: [Last](028-Last.md)\n"))
        self.assertEqual(metadata["coverage.json"]["file_count"], 2)
        self.assertEqual(metadata["coverage.json"]["files"][0], source)
        self.assertEqual(metadata["coverage.json"]["files"][-1]["filename"], "付録.md")
        self.assertNotIn("source_start", metadata["coverage.json"]["files"][-1])
        self.assertEqual(metadata["manifest.json"]["files"][0]["source_ranges"], [])
        self.assertEqual(hc.read_json(coverage_path)["files"], [source])
        for filename, data in metadata.items():
            if filename != "navigation":
                hc.write_json_atomic(coverage_path.parent / filename, data)
        before = dict(pages)
        self.assertEqual(self.store._publication("input.md", doc, pages), metadata)
        self.assertEqual(pages, before)
        pages["029-付録.md"] += "\nAnother memo.\n"
        self.store._publication("input.md", doc, pages)
        self.assertIn("Human memo.", pages["029-付録.md"])
        self.assertLess(pages["029-付録.md"].index("Another memo."), pages["029-付録.md"].index("前のページ:"))

    def test_multiple_conflicts_on_one_line_make_one_complete_note(self):
        old = "The limit is 110 seconds (11).\n"
        human = "The limit is 125 seconds (12-human).\n"
        new = "The limit is 120 seconds (12-source).\n"
        result = hc._fallback(old, human, new)
        self.assertEqual(result.count(hc.NOTE), 1)
        self.assertIn(human, result)
        self.assertIn("The limit is 120 seconds (12-source).", result)

    def test_invalid_merged_page_falls_back_without_aborting_other_pages(self):
        old = {"p.md": "P.\n", "q.md": "Q.\n"}
        human = {"p.md": "P.\nHuman p.\n", "q.md": "Q.\nHuman q.\n"}
        new = {"p.md": "P.\nSource p.\n", "q.md": "Q.\nSource q.\n"}
        real_check = hc._check_tables

        def check(text, baseline, **kwargs):
            if "Human p." in text:
                raise ValueError("synthetic invalid page")
            return real_check(text, baseline, **kwargs)

        stats = Counter()
        with patch.object(hc, "_check_tables", side_effect=check):
            pages, entries = hc.rebase(old, human, new, stats=stats)
        self.assertEqual(pages["p.md"], new["p.md"])
        self.assertIn("Human p.", "".join(entries))
        self.assertIn("Human q.", pages["q.md"])
        self.assertIn("Source q.", pages["q.md"])
        self.assertEqual(stats["page_fallback"], 1)

    def test_model_cannot_reannounce_an_unchanged_decided_value(self):
        old, human, new = "Limit 20; interval 5.\n", "Limit 15; interval 5.\n", "Limit 20; interval 6.\n"
        result = "Limit 15; interval 6.\n（元文書の更新: 20）\n"
        with self.assertRaises(ValueError):
            hc._attempt(Model(result), old, human, new, [], "", Counter())
        pending = self.rebase(old, human + "（元文書の更新: 20）\n", new)
        self.assertEqual(pending.count(hc.NOTE), 1)

    def test_one_plain_note_per_deleted_list_entry(self):
        old, human, new = "- **Alpha** old\n- **Beta** old\n", "", "- **Alpha** new\n- **Beta** new\n"
        result = hc._fallback(old, human, new)
        self.assertEqual(result.count(hc.NOTE), 2)
        self.assertNotIn("**", result)
        self.assertEqual(hc._current_notes(result, new), result)
        candidate = "> **元文書の更新:**\n> - **Alpha** new\n> - **Beta** new\n"
        checked = hc._attempt(Model(candidate), old, human, new, [], "", Counter())[0]
        self.assertEqual(checked, result)
        self.assertEqual(hc._note("-20℃\n"), "（元文書の更新: -20°C）\n")

    def test_malformed_fallback_is_blocked_and_human_code_is_preserved(self):
        with self.assertRaises(ValueError):
            hc._check_tables("<table>\n<tr><td>A</td>\n</table>\n", "<table>\n<tr><td>A</td></tr>\n</table>\n")
        old = "# Page\n\nIntro.\n"
        pasted = "```html\n<table><tr><td>example\n（元文書の更新: literal example）\n```\n\n| A | B |\n|---|---|\n| Ａ | 1 |\n"
        self.assertIn(pasted, self.rebase(old, old + pasted, old.replace("Intro", "New intro")))
        self.assertEqual(hc._current_notes(pasted, ""), pasted)
        hc._check_tables("Inline `<table><tr><td>example` remains literal.\n", "")

    def test_table_cells_require_rows_and_feed_the_existing_repair_loop(self):
        for body in ("<td>A</td>", "<TH>A</TH>", "<tr><td>A</td></tr><td>B</td>"):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "cell outside a row"):
                hc._check_tables("<table>" + body + "</table>", "")
        inherited = "<table><td>Existing human fragment</td></table>"
        hc._check_tables(inherited, inherited)
        hc._check_tables("```html\n<table><td>Literal example</td></table>\n```\n", "")
        hc._check_tables("<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>B</td></tr></tbody></table>", "")
        old = "<table><tr><td>Limit 10</td></tr></table>\n"
        good = old.replace("10", "12")
        bad = good.replace("<tr>", "").replace("</tr>", "")
        verdict = {"approved": True, "changes": [{"id": "H0001", "status": "preserved",
                    "page": "p.md", "evidence": "Limit 12"}]}
        model = FinalModel(self.project, [verdict, verdict], rewrites=[{
            "edits": [{"page": "p.md", "find": bad, "replace": good}]}])
        self.assertEqual(hc._finalize_pages(model, {"p.md": old}, {"p.md": good}, {"p.md": old},
                                           {"p.md": bad}), {"p.md": good})
        self.assertIn("cell outside a row", str(model.calls[0][1]["MECHANICAL_ERRORS"]))
        self.assertIn("cell outside a row", str(model.calls[1][1]["CURRENT_FINDINGS"]))
        self.assertEqual(model.calls[2][1]["MECHANICAL_ERRORS"], [])

    def test_capture_does_not_validate_unrelated_pages_or_reject_human_layouts(self):
        old = "# Edited\n\nOriginal.\n"
        ragged = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>\n"
        malformed = "<table>\n<tr><td>Human HTML fragment\n</table>\n"
        for other, edited in ((ragged, old), (malformed, old), (old, ragged), (old, malformed)):
            with self.subTest(unrelated=other != old, edited_table=edited != old):
                self.seed({"p.md": edited, "q.md": other})
                human = edited + "Human memo.\n"
                self.store.accept("input.md", "doc/p.md", "p", "capture", edited, human)
                current = self.store._load("input.md")[2]
                self.assertEqual(current, {"p.md": human, "q.md": other})
                self.assertEqual(len(list((self.store._dir("input.md") / "captures").glob("*.json"))), 1)
        old = '<table>\n<tr><td rowspan="2">Group</td><td>A</td></tr>\n<tr><td>B</td></tr>\n</table>\n'
        human = old.replace("<tr><td>B", "<tr><td>New</td><td>Extra</td></tr>\n<tr><td>B")
        self.seed({"p.md": old})
        self.store.accept("input.md", "doc/p.md", "p", "row-insert", old, human)
        self.assertEqual(self.store._load("input.md")[2]["p.md"], human)
        self.store.generate("input.md", {"p.md": old + "Source memo.\n"})
        self.assertEqual(self.store._load("input.md")[2]["p.md"], human + "Source memo.\n")

    def test_existing_ragged_geometry_allows_fact_merge_but_new_damage_fails(self):
        old = "<table><tr><td>Limit 10</td><td>Interval 5</td></tr><tr><td>Note</td></tr></table>\n"
        human, new = old.replace("10", "15"), old.replace("5", "6")
        self.assertEqual(self.rebase(old, human, new), human.replace("Interval 5", "Interval 6"))
        valid = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>\n"
        with self.assertRaisesRegex(ValueError, "ragged"):
            hc._check_tables(valid.replace("<td>D</td>", ""), valid)

    def test_model_validation_accepts_table_fragments_and_inherited_geometry(self):
        old, human, new = "<td>10</td>\n", "<td>15</td>\n", "<td>20</td>\n"
        result = "<td>15</td>\n（元文書の更新: 20）\n"
        self.assertEqual(hc._attempt(Model(result), old, human, new, [], "", Counter())[0], result)

    def test_transport_rebase_preserves_human_rowspan_attributes(self):
        old = '<table>\n<tr><td rowspan="2">Group</td><td>A</td></tr>\n<tr><td>B</td></tr>\n</table>\n'
        human = old.replace("<tr><td>B", "<tr><td>New</td><td>Extra</td></tr>\n<tr><td>B")
        local = old.replace(">A<", ">`A`<")
        pages, entries = hc.rebase({"p": old}, {"p": human}, {"p": local}, notes=False)
        self.assertEqual(pages["p"], human.replace(">A<", ">`A`<"))
        self.assertEqual(entries, [])

    def test_twin_tables_conflict_is_on_the_correct_table(self):
        table = "<table>\n<tr><td>Verdict</td><td>○</td></tr>\n</table>\n"
        old = "## First\n" + table + "## Second\n" + table
        human = old.replace("○", "△", 1)
        new = old.replace("○", "×", 1)
        result = self.rebase(old, human, new, Model())
        self.assertIn("<td>△</td>", result.split("## Second")[0])
        self.assertIn(table, result.split("## Second")[1])
        self.assertEqual(result.count(hc.NOTE), 1)

    def test_links_survive_capture_generation_and_noop_but_reset_and_force_work(self):
        pages = {"p.md": "# Page\n\nAlpha entity is documented.\n", "q.md": "# Other\n\nUnedited.\n"}
        self.seed(pages)
        page = lambda name, text: linker.Page("doc/" + name, "doc", ".", name, self.project.wiki_dir("") / name, text, [])
        p, q = page("p.md", pages["p.md"]), page("q.md", pages["q.md"])
        settings = SimpleNamespace(wiki_linker_inline_max_neo=3)
        linker._apply_links(self.project, [p], [linker.PlannedLink(p, q, "Alpha entity", "entity", .99)], settings)
        linked = p.path.read_text()
        self.store.accept("input.md", "doc/q.md", "q", "1", pages["q.md"], pages["q.md"] + "Human memo.\n")
        self.assertIn(p.rel, linker._manifest(self.project)["pages"])
        self.store.generate("input.md", {**pages, "q.md": pages["q.md"] + "Source memo.\n"})
        linker._apply_links(self.project, [p], [], settings)
        self.assertEqual(p.path.read_text(), linked)
        self.assertEqual(linker._apply_links(self.project, [p], [], settings)["pages"], 0)
        cleaned, error = linker._undo_manifest_row(linked, linker._manifest(self.project)["pages"][p.rel])
        self.assertEqual((cleaned, error), (p.text, ""))
        self.assertEqual(linker._undo_edits("Human replaced the whole line.\n", linker._manifest(self.project)["pages"][p.rel]["edits"])[1], "")
        linker._apply_links(self.project, [p], [], settings, force=True)
        self.assertEqual(p.path.read_text(), p.text)
        self.assertNotIn(p.rel, linker._manifest(self.project)["pages"])
        linker._apply_links(self.project, [p], [linker.PlannedLink(p, q, "Alpha entity", "entity", .99)], settings)
        code = "```\n" + p.text + "```\n"
        p.path.write_text(code)
        linker._apply_links(self.project, [page("p.md", code)], [], settings)
        self.assertEqual(p.path.read_text(), code)


if __name__ == "__main__":
    unittest.main()
