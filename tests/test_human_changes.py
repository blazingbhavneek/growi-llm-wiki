"""Human edits as a page diff: rebase, the state layer, model calls, and publication safety.

No network: the model is a scripted fake.
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graph.growi.client import GrowiAPIError, GrowiPage, GrowiPublisher, publish_pages, wrap_page
from graph.workspace.project import Project
from graph.workspace import writer
from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic
from publisher import human_changes as hc
from publisher.human_changes import HumanStore, apply_generated, classify, merge, rebase, unlinked
from publisher.history import candidate, candidate_is_clean, commit_candidate, ensure_repository, last_good, promote
from publisher import pipeline


BASE = "# Page\n\nIntro.\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n\n## Other\n\nUnchanged.\n"
HUMAN = BASE.replace("40°C", "60°C")


class FakeModel:
    """Scripted classify / merge / verify; counts every call."""

    def __init__(self, merges=(), verdicts=(), classes=None):
        self.merges, self.verdicts, self.classes = list(merges), list(verdicts), classes
        self.calls = Counter()
        self.merge_args = []
        self.findings = []

    def merge(self, old_w, cur_w, new_w, guidance, failure):
        self.calls["merge"] += 1
        self.merge_args.append((old_w, cur_w, new_w, list(guidance), failure))
        if not self.merges:
            raise RuntimeError("no scripted merge")
        return self.merges.pop(0)

    def verify(self, old_w, cur_w, new_w, result, appendix, *, findings=None):
        self.calls["verify"] += 1
        self.findings.append(list(findings or []))
        if self.verdicts:
            return self.verdicts.pop(0)
        ok = not findings  # unscripted: a strict judge that rejects what the mechanical checks suspect
        return {"human_kept": ok, "source_kept": ok}

    def finalize(self, old, human, new, result, obligations, *, metadata=None):
        return result  # the per-page final judge has its own tests in test_human_final_merge.py

    def classify(self, pairs):
        self.calls["classify"] += 1
        if self.classes is None:
            raise RuntimeError("classification unavailable")
        return self.classes


class NoModel:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected model call: {name}")


def skip_final_judge(case):
    """Pipeline-level tests build the real LlmHumanModel; the per-page final judge is covered in test_human_final_merge.py."""
    patcher = patch.object(hc.LlmHumanModel, "finalize", lambda self, old, human, new, result, obligations, **kw: result)
    patcher.start()
    case.addCleanup(patcher.stop)


def one(old, human, new, **kwargs):
    pages, entries = rebase({"a.md": old}, {"a.md": human}, {"a.md": new}, **kwargs)
    return pages["a.md"], entries


class RebaseTest(unittest.TestCase):
    def test_source_notes_expire_only_when_their_payload_leaves_the_source(self):
        note = "Human 60.\n（元文書の更新: Source 55.）\n"
        self.assertEqual(hc._current_notes(note, "Source 55.\n"), note)
        self.assertEqual(hc._current_notes(note, "Source 50.\n"), "Human 60.\n")
        cell = "<td>Human（元文書の更新: Source 55.）</td>\n"
        self.assertEqual(hc._current_notes(cell, "Source 50.\n"), "<td>Human</td>\n")
        quote = "> **元文書の更新:**\n> Source 55.\n"
        self.assertEqual(hc._current_notes(quote, "Source 50.\n"), "")
        self.assertEqual(hc._current_notes("（元文書の更新: ---）\n", "---\n"), "")

    def test_clean_merge_handles_disjoint_edits_but_not_source_edits_inside_a_human_deletion(self):
        old = "# T\nA,\nkeep\nobsolete\nend\n"
        human = "# T\nA\nHuman extra\nkeep\nobsolete\nend\n"
        source = "# T\nA,\nkeep\nend\n"
        self.assertEqual(hc._clean_merge(old, human, source), "# T\nA\nHuman extra\nkeep\nend\n")
        self.assertIsNone(hc._clean_merge("a\nb\nc\n", "a\nc\n", "a\nB\nc\n"))

    def test_fallback_drops_stale_source_text_and_bounds_notes(self):
        self.assertEqual(
            hc._fallback("keep 1\nstale\n", "keep 2\nstale\n", "keep 3\n"),
            "keep 2\n（元文書の更新: 3）\n",
        )
        self.assertEqual(
            hc._fallback("[x](old)\n", "[x](old)\nHuman\n", "[x](new)\n"),
            "[x](new)\nHuman\n",  # disjoint edits: the source's link change lands beside the human line
        )
        table = hc._fallback(
            "<table>\n<tr><td>1</td></tr>\n</table>\n",
            "<table>\n<tr><td>2</td></tr>\n</table>\n",
            "<table>\n<tr><td>3</td></tr>\n<tr><td>4</td></tr>\n</table>\n",
        )
        # The source's new row lands in the table; only the clashing cell is quoted, after the table.
        self.assertEqual(table, "<table>\n<tr><td>2</td></tr>\n<tr><td>4</td></tr>\n</table>\n（元文書の更新: 3）\n")

        # The source replaced the whole table: the page rebuild keeps the human cell in the appendix.
        text, entries = one(
            "# Old\n\n<table>\n<tr>\n<td>Team</td>\n<td>4321</td>\n</tr>\n"
            "<tr><td>Deleted team</td><td>4188</td></tr>\n</table>\n",
            "# Human\n\n<table>\n<tr>\n<td>Team</td>\n<td>7777</td>\n</tr>\n</table>\n",
            "# New\n\n- Team: 5555\n",
        )
        self.assertIn("- Team: 5555", text)
        self.assertNotIn("Deleted team", text + "".join(entries))
        self.assertEqual(hc._note_in_table(text), 0)
        self.assertIn("7777", "".join(entries))

        deleted_row = hc._fallback(
            "<table>\n<tr><td>Keep</td></tr>\n<tr><td>Delete</td></tr>\n</table>\n",
            "<table>\n<tr><td>Keep</td></tr>\n</table>\n",
            "Source rewrite\n",
        )
        self.assertEqual(deleted_row, "Source rewrite\n")  # the source dropped the table: the row deletion has nothing to apply to
        self.assertEqual(hc._note_in_table(deleted_row), 0)

        fragment = hc._fallback(
            "old\n<table>\n<thead>\n<tr>\n",
            "human\n<table>\n<thead>\n<tr>\n",
            "new\n<table>\n<thead>\n<tr>\n",
        )
        self.assertTrue(fragment.endswith("（元文書の更新: new）\n"))  # a window cut inside a table is repaired when the page is assembled
        cell = "# T\n\n<table>\n<tr><td>{}</td></tr>\n</table>\n"
        page, _ = one(cell.format(1), cell.format(2), cell.format(3))
        self.assertEqual(hc._note_in_table(page), 0)
        self.assertLess(page.index("</table>"), page.index(hc.NOTE))

        misplaced = (
            "<table>\n<tr><td>human（元文書の更新: source）</td></tr>\n"
            "> **元文書の更新:**\n> source row\n</table>\n"
        )
        relocated = hc._notes_after_tables(misplaced)
        self.assertEqual(hc._note_in_table(relocated), 0)
        self.assertIn("<td>human</td>", relocated)
        self.assertLess(relocated.index("</table>"), relocated.index(hc.NOTE))

    def test_normalised_same_page_match_keeps_a_human_heading(self):
        old = "# T\n\n## X\n\nbody\n"
        human = old.replace("## X", "## Human")
        new = old.replace("## X", "### X")
        self.assertEqual(one(old, human, new, model=NoModel())[0], human)

    def test_page_remap_requires_a_unique_thirty_percent_overlap(self):
        old = {"004-old.md": "".join(f"line {i}\n" for i in range(10))}
        enough = {"007-new.md": "line 0\nline 1\nline 2\nnew\n"}
        too_little = {"007-new.md": "line 0\nline 1\nnew\n"}
        self.assertEqual(hc._page_remap(old, enough), {"004-old.md": "007-new.md"})
        self.assertEqual(hc._page_remap(old, too_little), {})
        tied = {**enough, "008-other.md": enough["007-new.md"]}
        self.assertEqual(hc._page_remap(old, tied), {})

    def test_nested_parentheses_in_link_targets_are_unwrapped_as_one_link(self):
        link = "[dialog](008-name(1-9)(1-2).md)"
        self.assertEqual(unlinked(link, {link}), "dialog")
        from graph.fast.inline_linker import _wrap_once
        from graph.wiki.page import MARKDOWN_LINK_RE

        self.assertIsNotNone(MARKDOWN_LINK_RE.fullmatch(link))
        wrapped, change = _wrap_once(link + "\n", "dialog", "other.md")
        self.assertEqual(wrapped, link + "\n")
        self.assertIsNone(change)
        text = "> dialog\n元ページ: dialog\n（元文書の更新: dialog）\nplain dialog\n"
        wrapped, change = _wrap_once(text, "dialog", "other.md")
        self.assertEqual(wrapped.count("[dialog](other.md)"), 1)
        self.assertTrue(wrapped.endswith("plain [dialog](other.md)\n"))
        self.assertIsNotNone(change)

    def test_no_human_change_gives_the_new_text_and_no_source_change_gives_the_human_text(self):
        import random

        random.seed(3)

        def text():
            return "".join(f"line {random.randint(0, 9)}\n" if random.random() < 0.8 else "\n"
                           for _ in range(random.randint(0, 12)))

        for _ in range(500):
            x = {"a.md": text(), "b.md": text()}
            y = {name: text() if random.random() < 0.6 else body for name, body in x.items()}
            z = {"a.md": text(), "b.md": text()}
            self.assertEqual(rebase(x, x, z, model=NoModel())[0], z)
            self.assertEqual(rebase(x, y, x, model=NoModel())[0], y)

    def test_rule_1_and_2_human_only_and_source_only(self):
        self.assertEqual(one(BASE, HUMAN, BASE, model=NoModel()), (HUMAN, []))
        source = BASE.replace("AUTO", "MANUAL")
        self.assertEqual(one(BASE, BASE, source, model=NoModel()), (source, []))

    def test_rule_3_same_change_is_kept_once_without_a_note_or_model(self):
        stats = Counter()
        text, entries = one(BASE, HUMAN, HUMAN.replace("Unchanged.", "More."), model=NoModel(), stats=stats)
        self.assertEqual(text.count("60°C"), 1)
        self.assertNotIn("元文書", text)
        self.assertEqual(entries, [])
        self.assertEqual(stats["window1"], 1)

    def test_rule_4_same_fact_differently_through_the_merge_call(self):
        merged = {"edits": [{"find": "Maximum is 55°C.\n", "replace": "Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n"}],
                  "appendix": ""}
        model = FakeModel([merged])
        text, entries = one(BASE, HUMAN, BASE.replace("40°C", "55°C"), model=model, guidance={"a.md": ["use ##"]})
        self.assertIn("Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n", text)
        self.assertIn("Mode is AUTO.", text)
        self.assertEqual(model.calls, Counter(merge=1, verify=1))
        self.assertEqual(model.merge_args[0][3], ["use ##"])
        self.assertEqual(entries, [])

    def test_rule_4_fallback_note_keeps_both_versions_visible(self):
        text, _ = one(BASE, HUMAN, BASE.replace("40°C", "55°C"))
        self.assertIn("Maximum is 60°C.\n（元文書の更新: 55°C）\nMode is AUTO.", text)
        long = "Maximum is 55°C. " + "x" * 220
        text, _ = one(BASE, HUMAN, BASE.replace("Maximum is 40°C.", long))
        self.assertIn("（元文書の更新: 55°C）\nMode is AUTO.", text)  # the note stays bounded to the conflicting span
        self.assertNotIn(hc.NOTE + ":**", text)

    def test_rule_5_different_facts_coexist(self):
        text, _ = one(BASE, HUMAN, BASE.replace("AUTO", "MANUAL"), model=NoModel())
        self.assertIn("60°C", text)
        self.assertIn("MANUAL", text)

    def test_exact_match_is_line_aligned_and_needs_uniqueness_in_old_and_new(self):
        old = "# T\n\nA\nOK\n\nB\nOK\n\nC\n"
        human = old.replace("A\nOK\n", "A\nOK (checked)\n")  # "OK" occurs twice: no exact match
        stats = Counter()
        text, _ = one(old, human, old.replace("C\n", "C2\n"), model=NoModel(), stats=stats)
        self.assertEqual(text, human.replace("C\n", "C2\n"))
        self.assertEqual(stats["exact"], 0)
        self.assertEqual(stats["window3"] + stats["window2"], 1)
        # A unique line that is also a substring of another line must not be matched inside it.
        old = "x\nfoo\nfoo bar\ny\n"
        text, _ = one(old, "x\nFOO\nfoo bar\ny\n", "x\nfoo\nfoo bar\ny\nz\n", model=NoModel())
        self.assertEqual(text, "x\nFOO\nfoo bar\ny\nz\n")

    def test_human_change_follows_a_section_the_source_moves_or_reorders(self):
        old = {"a.md": "# A\n\n## One\n\nalpha 1\n\n## Two\n\nbeta 2\n", "b.md": "# B\n\nbravo\n"}
        human = {"a.md": old["a.md"].replace("beta 2", "beta 20")}
        reordered = {"a.md": "# A\n\n## Two\n\nbeta 2\n\n## One\n\nalpha 1\n", "b.md": old["b.md"]}
        pages, entries = rebase(old, human, reordered, model=NoModel())
        self.assertEqual(pages["a.md"], reordered["a.md"].replace("beta 2", "beta 20"))
        moved = {"a.md": "# A\n\n## One\n\nalpha 1\n", "b.md": "# B\n\nbravo\n\n## Two\n\nbeta 2\n"}
        pages, entries = rebase(old, human, moved, model=NoModel())
        self.assertEqual(pages["b.md"], "# B\n\nbravo\n\n## Two\n\nbeta 20\n")
        self.assertEqual(entries, [])

    def test_a_section_added_mid_page_a_renamed_heading_and_reordering_keep_their_place(self):
        human = BASE.replace("## Other", "## Notes\n\nHuman note 77.\n\n## Other")
        human = human.replace("## Limits", "## Operating limits")
        self.assertEqual(one(BASE, human, BASE, model=NoModel())[0], human)
        self.assertEqual(one(BASE, human, BASE.replace("Unchanged.", "Source v2."), model=NoModel())[0],
                         human.replace("Unchanged.", "Source v2."))
        reordered = "# Page\n\nIntro.\n\n## Other\n\nUnchanged.\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n"
        self.assertEqual(one(BASE, reordered, BASE, model=NoModel())[0], reordered)

    def test_a_repeated_line_never_carries_a_change_to_another_section(self):
        old = "# A\n\n## S1\n\nMode is AUTO.\n\n## S2\n\nMode is AUTO.\n\n## S3\n\nend\n"
        human = old.replace("## S2\n\nMode is AUTO.", "## S2\n\nMode is MANUAL.")
        new = old.replace("## S2\n\nMode is AUTO.\n\n", "")  # the source deletes S2
        text, entries = one(old, human, new, model=FakeModel([{"edits": [], "appendix": "Mode is MANUAL.\n"}]))
        self.assertIn("## S1\n\nMode is AUTO.", text)
        self.assertNotIn("MANUAL", text)  # S2 is gone, and the human change never lands on S1's identical line
        self.assertEqual(len(entries), 1)  # the deleted block's human text is kept in the appendix
        self.assertIn("## S2\n\nMode is MANUAL.", entries[0])

    def test_a_blank_line_left_by_a_human_does_not_freeze_a_source_change(self):
        old, new = "# T\n\n- a 25\n- b\n", "# T\n\n- a 26\n- b\n"
        human = "# T\n\n- a 25\n\n- b\n"  # what an insert-then-delete in the editor leaves behind
        self.assertEqual(one(old, human, new, model=NoModel())[0], new)

    def test_a_section_added_before_a_heading_survives_a_rewording_without_a_note(self):
        old = "# T\n\n## A\n\nOld wording of a.\n\n## B\n\nb\n"
        human = old.replace("## B", "## Human section\n\nHuman text 77.\n\n## B")
        new = old.replace("Old wording of a.", "New wording of a.")
        self.assertEqual(one(old, human, new, model=NoModel())[0], new.replace("## B", "## Human section\n\nHuman text 77.\n\n## B"))

    def test_independent_windows_merge_in_parallel(self):
        import threading
        import time

        class Parallel(FakeModel):
            active = peak = 0
            lock = threading.Lock()

            def merge(self, *args):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                time.sleep(0.2)
                with self.lock:
                    self.active -= 1
                return {"edits": [{"find": "Maximum is 55°C.\n",
                                   "replace": "Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n"}], "appendix": ""}

        model, new = Parallel(), BASE.replace("40", "55")
        pages, _ = rebase({"a.md": BASE, "b.md": BASE}, {"a.md": HUMAN, "b.md": HUMAN}, {"a.md": new, "b.md": new},
                          model=model, concurrency=4)
        self.assertEqual(model.peak, 2)
        self.assertTrue(all("Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n" in text for text in pages.values()))

    def test_source_deletes_a_changed_section_or_page_into_the_appendix_once(self):
        stats = Counter()
        pages, entries = rebase({"001.md": BASE, "002.md": "# Two\n\ntwo\n"}, {"001.md": HUMAN},
                                {"001.md": BASE.split("## Limits")[0], "002.md": "# Two\n\ntwo\n"}, model=NoModel(), stats=stats)
        self.assertNotIn("60°C", pages["001.md"])
        self.assertEqual(len(entries), 1)
        self.assertIn("Maximum is 60°C.", entries[0])
        self.assertNotIn("Mode is AUTO", entries[0])
        self.assertIn("## Limits", entries[0])
        self.assertIn("元ページ: 001.md", entries[0])
        self.assertEqual(stats["window4"], 1)
        # A whole page that disappears.
        pages, entries = rebase({"001.md": BASE}, {"001.md": HUMAN}, {"002.md": "# New\n"}, model=NoModel())
        self.assertEqual(pages, {"002.md": "# New\n"})
        self.assertEqual(len(entries), 1)
        self.assertIn("Maximum is 60°C.", entries[0])

    def test_both_sides_delete_the_same_text(self):
        human = BASE.replace("Mode is AUTO.\n", "")
        text, entries = one(BASE, human, human, model=NoModel())
        self.assertEqual((text, entries), (human, []))
        # The human deleted text the source no longer has anywhere: nothing to keep.
        pages, entries = rebase({"a.md": BASE}, {"a.md": human}, {"a.md": "# Other\n\nx\n"}, model=NoModel())
        self.assertEqual(entries, [])
        self.assertNotIn("AUTO", pages["a.md"])

    def test_human_deletion_stays_deleted_and_the_new_source_value_becomes_a_note(self):
        human = BASE.replace("Maximum is 40°C.\n", "")
        text, _ = one(BASE, human, BASE.replace("40°C", "55°C"))
        self.assertEqual(text, BASE.replace("Maximum is 40°C.\n", "（元文書の更新: Maximum is 55°C.）\n"))

    def test_merge_output_is_checked_before_it_is_accepted(self):
        source = BASE.replace("40°C", "55°C")
        good_edit = {"find": "Maximum is 55°C.\n", "replace": "Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n"}
        cases = {
            "drops a human number": {"edits": [{"find": "Maximum is 55°C.\n", "replace": "Maximum is high.\n"}], "appendix": ""},
            "find is missing": {"edits": [{"find": "no such text", "replace": "x"}], "appendix": ""},
            "find is not unique": {"edits": [{"find": "°C", "replace": "K"}], "appendix": ""},
            "empty find": {"edits": [{"find": "", "replace": "x"}], "appendix": ""},
            "edits overlap": {"edits": [{"find": "Maximum is 55°C.", "replace": "60"}, {"find": "55°C.\nMode", "replace": "60"}], "appendix": ""},
        }
        for name, bad in cases.items():
            with self.subTest(name):
                model = FakeModel([bad, good_edit and {"edits": [good_edit], "appendix": ""}])
                text, _ = one(BASE, HUMAN, source, model=model)
                self.assertIn("Maximum is 60°C.\n（元文書の更新", text)
                self.assertEqual(model.calls["merge"], 2)
                self.assertIn(" ", model.merge_args[1][4])  # the second attempt is told why
        stats = Counter()
        model = FakeModel([{"edits": [good_edit], "appendix": ""}] * 2, [{"human_kept": False, "source_kept": True}] * 2)
        text, _ = one(BASE, HUMAN, source, model=model, stats=stats)
        self.assertEqual((model.calls["merge"], model.calls["verify"]), (2, 2))
        self.assertEqual(stats["fallback"], 1)
        self.assertIn("Maximum is 60°C.\n（元文書の更新: 55°C）\nMode is AUTO.", text)
        model = FakeModel([{"edits": [good_edit], "appendix": ""}] * 2, [{"human_kept": True, "source_kept": False}, {"human_kept": True, "source_kept": True}])
        one(BASE, HUMAN, source, model=model)
        self.assertEqual(model.calls["merge"], 2)

    def test_merge_cannot_send_human_text_to_the_appendix(self):
        source = "# Page\n\nRewritten completely.\n"
        model = FakeModel([{"edits": [], "appendix": "Model appendix.\n"}])
        text, entries = one(BASE, HUMAN, source, model=model)
        self.assertEqual(text, source)
        self.assertNotIn("Model appendix.", text + "".join(entries))  # the model's appendix is never used
        self.assertEqual(len(entries), 1)  # the source replaced the edited block: the deletion path keeps the human text
        self.assertIn("Maximum is 60°C.", entries[0])

    def test_bad_model_notes_and_stale_source_text_are_rejected(self):
        source = BASE.replace("40°C", "55°C")
        good = {"edits": [{
            "find": "Maximum is 55°C.\n",
            "replace": "Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n",
        }]}
        # Mechanical checks reach the judge as suspicions; restored OLD text that the human replaced is the judge's call.
        bad = [
            ({"edits": [{"find": "Maximum is 55°C.\n", "replace": "<table><tr><td>Maximum is 60°C.（元文書の更新: Maximum is 55°C.）</td></tr></table>\n"}]}, []),
            ({"edits": [{"find": "Maximum is 55°C.\n", "replace": "Maximum is 60°C.\n（元文書の更新: ---）\n"}]}, []),
            ({"edits": [{"find": "Maximum is 55°C.\n", "replace": "Maximum is 40°C.\nMaximum is 60°C.\n（元文書の更新: Maximum is 55°C.）\n"}]},
             [{"human_kept": True, "source_kept": False}]),
        ]
        for answer, verdicts in bad:
            with self.subTest(answer=answer):
                model = FakeModel([answer, good], verdicts)
                text, entries = one(BASE, HUMAN, source, model=model)
                self.assertIn("Maximum is 60°C.\n（元文書の更新: Maximum is 55°C.）", text)
                self.assertNotIn("<table>", text)
                self.assertNotIn("---）", text)
                self.assertNotIn("Maximum is 40°C.", text)
                self.assertEqual(entries, [])
                self.assertEqual(model.calls["merge"], 2)

    def test_unresolved_transport_rebase_keeps_human_text_without_a_note(self):
        text, entries = one(BASE, HUMAN, BASE.replace("40°C", "55°C"), notes=False)
        self.assertIn("Maximum is 60°C.", text)
        self.assertNotIn("元文書の更新", text)
        self.assertEqual(entries, [])

    def test_appendix_reasons_are_honest_and_table_fragments_are_wrapped(self):
        old = {"001-old.md": "# T\n\n<td>old</td>\n"}
        human = {"001-old.md": "# T\n\n<td>human</td>\n"}
        _pages, deleted = rebase(old, human, {"001-old.md": "# T\n"}, model=NoModel())
        self.assertIn("元の文書では、この部分は削除されました", deleted[0])
        self.assertIn("<table>\n<td>human</td>\n</table>", deleted[0])
        _pages, lost = rebase(old, human, {"002-new.md": "# Other\n\nunrelated\n"}, model=NoModel())
        self.assertIn("新しい版ではこの編集の位置を特定できませんでした", lost[0])

    def test_classification_only_collects_structure_instructions(self):
        model = FakeModel(classes=[{"kind": "structure", "instruction": "Use ## for steps"},
                                   {"kind": "content", "instruction": "ignored"}])
        self.assertEqual(classify(model, [("a", "b"), ("c", "d")]), ["Use ## for steps"])
        self.assertEqual(classify(FakeModel(), [("a", "b")]), [])
        self.assertEqual(classify(None, [("a", "b")]), [])


class AdapterTest(unittest.TestCase):
    """LlmHumanModel on a fake chat port: structured results arrive as instances or dicts."""

    def adapter(self, answer):
        from publisher.human_changes import LlmHumanModel

        seen = []

        class Port:
            async def structured(self, schema, messages, **kwargs):
                seen.append((schema.__name__, kwargs, [m.content for m in messages]))
                return answer(schema)

        model = LlmHumanModel(SimpleNamespace(), SimpleNamespace(metadata=Path("/nonexistent")))
        model._port = Port()
        return model, seen

    def test_calls_return_plain_dicts_and_keep_page_text_in_data_messages(self):
        from publisher import human_changes as hc

        model, seen = self.adapter(lambda schema: schema.model_validate(
            {"items": [{"kind": "structure", "instruction": "tidy"}]} if schema is hc._Classes else
            {"edits": [{"find": "a", "replace": "b"}], "appendix": ""} if schema is hc._Merged else
            {"human_kept": True, "source_kept": False}))
        self.assertEqual(model.classify([("B text", "A text")]), [{"kind": "structure", "instruction": "tidy"}])
        self.assertEqual(model.merge("o", "h", "n", ["g"], "why")["edits"], [{"find": "a", "replace": "b"}])
        self.assertEqual(model.verify("o", "h", "n", "r", ""), {"human_kept": True, "source_kept": False})
        self.assertEqual([name for name, _kw, _m in seen], ["_Classes", "_Merged", "_Verdict"])
        self.assertTrue(all(kw["temperature"] == 0.0 and "thinking" not in kw for _n, kw, _m in seen))
        self.assertIn("B text", seen[0][2][1])
        self.assertNotIn("B text", seen[0][2][0])  # page text is only in the delimited data message
        self.assertTrue(seen[0][2][1].startswith("<DATA>"))

    def test_a_dict_answer_is_validated_and_a_bad_one_falls_back_in_rebase(self):
        model, _seen = self.adapter(lambda schema: {"edits": "not a list"} if schema.__name__ == "_Merged" else {})
        text, _ = one(BASE, HUMAN, BASE.replace("40°C", "55°C"), model=model)
        self.assertIn("Maximum is 60°C.\n（元文書の更新: 55°C）", text)  # both attempts rejected: the bounded note


class StoreCase(unittest.TestCase):
    """A document with a project, as the writer leaves it after an export."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()
        self.rel = "doc.md"
        self.folder = self.project.wiki_dir(self.rel)
        self.folder.mkdir(parents=True)
        self.project.raw_file(self.rel).write_text("source\n", encoding="utf-8")
        writer.write_source_stamp(self.folder, self.project.raw_file(self.rel), self.rel, identity_seed="source-1")
        write_json_atomic(self.folder / "_planning" / "linker.json", {"status": "complete"})
        self.store = HumanStore(self.project)
        self.model = None
        self.generate({"001.md": BASE})

    def generate(self, pages):
        """What the writer does: rebuild the folder from the new pages, then apply_generated."""
        for page in self.folder.glob("*.md"):
            page.unlink()
        shutil.rmtree(self.folder / "_planning" / "pages", ignore_errors=True)
        for name, text in pages.items():
            write_text_atomic(self.folder / name, text)
        return apply_generated(self.project, self.rel, model=self.model)

    def pull(self, human, *, base=None, page="001.md", revision="r1"):
        base = self.text(page) if base is None else base
        return self.store.accept(self.rel, f"doc/{page}", "b1", revision, base, human, model=self.model)

    def text(self, name="001.md"):
        return (self.folder / name).read_text(encoding="utf-8")

    def current(self, name="001.md"):
        return (self.store._dir(self.rel) / "current" / name).read_text(encoding="utf-8")

    def names(self):
        return sorted(p.name for p in self.folder.glob("*.md"))


class StateTest(StoreCase):
    def test_no_state_is_written_until_the_first_human_edit(self):
        self.assertFalse((self.store.root / "doc").exists())
        self.assertEqual((self.folder / "_planning" / "pages" / "001.md").read_text(encoding="utf-8"), BASE)
        self.pull(HUMAN)
        directory = self.store._dir(self.rel)
        self.assertEqual((directory / "pure" / "001.md").read_text(encoding="utf-8"), BASE)
        self.assertEqual(self.current(), HUMAN)
        self.assertEqual(self.text(), HUMAN)
        self.assertEqual((self.folder / "_planning" / "pages" / "001.md").read_text(encoding="utf-8"), HUMAN)
        doc = read_json(directory / "doc.json")
        self.assertEqual((doc["raw_rel"], doc["appendix"], doc["guidance"]), (self.rel, None, {}))
        record = read_json(next((directory / "captures").glob("*.json")))
        self.assertEqual((record["page"], record["revision"]), ("001.md", "r1"))
        self.assertEqual(self.store.get(record["human_blob"]), HUMAN)
        self.store.audit()

    def test_human_change_survives_four_source_updates_and_a_restart(self):
        self.pull(HUMAN)
        for number in range(4):
            self.generate({"001.md": BASE.replace("Unchanged.", f"Source version {number}.")})
            self.store = HumanStore(self.project)
            self.assertEqual(self.text().count("60°C"), 1)
            self.assertIn(f"Source version {number}.", self.text())
        self.assertEqual(self.store.status()["counts"]["human_information"], 1)

    def test_full_regeneration_keeps_every_human_change(self):
        self.pull(HUMAN.replace("Intro.", "Intro by a human."))
        self.generate({"001.md": "# Page\n\nIntro.\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n\n## Other\n\nAll new words.\n"})
        self.assertIn("60°C", self.text())
        self.assertIn("Intro by a human.", self.text())
        self.assertIn("All new words.", self.text())

    def test_pull_while_the_local_generation_is_newer_than_the_published_one(self):
        newer = BASE.replace("Unchanged.", "Newer source.")
        self.generate({"001.md": newer})  # a build whose publication failed
        self.pull(HUMAN, base=BASE)  # the remote edit sits on the older published page
        self.assertEqual(self.text(), newer.replace("40°C", "60°C"))

    def test_rule_4_conflict_is_a_note_and_repeating_the_same_source_adds_none(self):
        self.pull(HUMAN)
        source = BASE.replace("40°C", "55°C")
        self.generate({"001.md": source})
        self.assertIn("Maximum is 60°C.\n（元文書の更新: 55°C）\n", self.text())
        self.generate({"001.md": source})
        self.assertEqual(self.text().count("元文書の更新"), 1)

    def test_a_changed_source_replaces_its_note_and_catching_up_removes_it(self):
        self.pull(HUMAN)
        self.generate({"001.md": BASE.replace("40°C", "55°C")})
        self.generate({"001.md": BASE.replace("40°C", "50°C")})
        self.assertEqual(self.text().count("元文書の更新"), 1)
        self.assertIn("（元文書の更新: 50°C）", self.text())
        self.assertNotIn("55°C", self.text())
        self.generate({"001.md": BASE.replace("40°C", "60°C")})
        self.assertNotIn("元文書の更新", self.text())
        self.assertEqual(self.text().count("60°C"), 1)

    def test_a_human_who_deletes_the_note_keeps_their_value_and_it_does_not_return(self):
        self.pull(HUMAN)
        source = BASE.replace("40°C", "55°C")
        self.generate({"001.md": source})
        self.pull(self.text().replace("（元文書の更新: 55°C）\n", ""), revision="r2")
        self.assertNotIn("元文書", self.text())
        self.generate({"001.md": source.replace("Unchanged.", "Next.")})
        self.assertNotIn("元文書", self.text())
        self.assertIn("60°C", self.text())

    def test_human_revert_follows_the_source_again_and_rule_2_applies_after_catching_up(self):
        self.pull(HUMAN)
        self.pull(BASE, revision="r2")
        self.generate({"001.md": BASE.replace("40°C", "55°C")})
        self.assertIn("55°C", self.text())
        self.assertNotIn("60°C", self.text())
        self.assertFalse(self.store.status()["counts"]["human_information"])
        # The source catches up to a human change, then changes the fact again.
        self.pull(HUMAN.replace("60", "65"), revision="r3")
        self.generate({"001.md": BASE.replace("40°C", "65°C")})
        self.assertEqual(self.text().count("65°C"), 1)
        self.assertNotIn("元文書", self.text())
        self.generate({"001.md": BASE.replace("40°C", "70°C")})
        self.assertIn("70°C", self.text())
        self.assertNotIn("65°C", self.text().replace("（元文書の更新", ""))

    def test_a_page_the_human_emptied_stays_blank_unpublished_and_deleted(self):
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n"})
        self.pull("", page="002.md")
        self.assertEqual(self.current("002.md"), "")
        self.assertEqual(self.names(), ["001.md"])
        self.assertFalse((self.folder / "_planning" / "pages" / "002.md").exists())
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n"})
        self.assertEqual(self.names(), ["001.md"])
        self.assertEqual(self.current("002.md"), "")
        self.assertFalse(self.store.status()["counts"]["human_information"])

    def test_the_source_deletes_a_changed_part_into_the_appendix_and_repeat_runs_add_nothing(self):
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"})
        self.pull(HUMAN)
        shrunk = {"001.md": BASE.split("## Limits")[0], "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"}
        self.generate(shrunk)
        self.assertEqual(self.names(), ["001.md", "002.md", "003.md", "004-付録.md"])
        appendix = self.text("004-付録.md")
        self.assertTrue(appendix.startswith("# 付録\n\n"))
        self.assertEqual(appendix.count("Maximum is 60°C."), 1)
        self.assertNotIn("Mode is AUTO", appendix)
        for _ in range(2):
            self.generate(shrunk)
        self.assertEqual(self.text("004-付録.md"), appendix)
        # The source restores the part: it appears normally and the entry stays.
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"})
        self.assertIn("Maximum is 40°C.", self.text("001.md"))
        self.assertEqual(self.text("004-付録.md").count("Maximum is 60°C."), 1)

    def test_the_appendix_keeps_its_name_and_human_edits_to_it_are_kept(self):
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n"})
        self.pull(HUMAN)
        self.generate({"001.md": "# Page\n\nIntro.\n", "002.md": "# Two\n\ntwo\n"})
        self.assertIn("003-付録.md", self.names())
        doc = read_json(self.store._dir(self.rel) / "doc.json")
        self.assertEqual(doc["appendix"], "003-付録.md")
        self.generate({"001.md": "# Page\n\nIntro.\n", "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"})
        self.assertEqual(self.names(), ["001.md", "002.md", "003-付録.md", "003.md"])
        self.assertEqual(self.text("003-付録.md").count("Maximum is 60°C."), 1)
        self.assertEqual(read_json(self.store._dir(self.rel) / "doc.json")["appendix"], "003-付録.md")
        # A human edits the Appendix like any page; the edit survives a generation.
        edited = self.text("003-付録.md").replace("Maximum is 60°C.", "Maximum is 61°C.")
        self.pull(edited, page="003-付録.md", revision="r2")
        self.assertIn("61°C", self.text("003-付録.md"))
        self.generate({"001.md": "# Page\n\nIntro.\n", "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"})
        self.assertEqual(self.text("003-付録.md").count("61°C"), 1)
        self.assertNotIn("60°C", self.text("003-付録.md"))
        # Deleting every entry leaves a blank page that is not published and stays blank.
        self.pull("", page="003-付録.md", revision="r3")
        self.assertEqual(self.names(), ["001.md", "002.md", "003.md"])
        self.generate({"001.md": "# Page\n\nIntro.\n", "002.md": "# Two\n\ntwo\n", "003.md": "# Three\n\nthree\n"})
        self.assertEqual(self.names(), ["001.md", "002.md", "003.md"])
        self.assertFalse(self.store.status()["counts"]["human_information"])

    def test_a_page_titled_appendix_by_the_generator_is_an_ordinary_page(self):
        self.generate({"001.md": BASE, "002-付録.md": "# 付録\n\ngenerated\n"})
        self.pull(HUMAN)
        self.generate({"001.md": BASE.replace("Unchanged.", "x"), "002-付録.md": "# 付録\n\ngenerated 2\n"})
        self.assertIn("60°C", self.text("001.md"))
        self.assertEqual(self.text("002-付録.md"), "# 付録\n\ngenerated 2\n")

    def test_structure_guidance_is_remembered_trimmed_and_does_not_change_merging(self):
        self.model = FakeModel(classes=[{"kind": "structure", "instruction": f"rule {i}"} for i in range(25)])
        self.pull(HUMAN)
        guidance = self.store.guidance(self.rel)["001.md"]
        self.assertEqual(guidance, [f"rule {i}" for i in range(5, 25)])
        self.pull(HUMAN, base=HUMAN, revision="r2")  # nothing changed: no new entry, no duplicates
        self.assertEqual(self.store.guidance(self.rel)["001.md"], guidance)
        self.assertEqual(self.text(), HUMAN)
        # A failed classification adds nothing and merges identically.
        self.model = FakeModel()
        self.pull(HUMAN.replace("Intro.", "Intro 2."), revision="r3")
        self.assertEqual(self.store.guidance(self.rel)["001.md"], guidance)
        self.assertIn("Intro 2.", self.text())

    def test_guidance_follows_a_renamed_page(self):
        self.generate({"001-old.md": BASE})
        self.model = FakeModel(classes=[{"kind": "structure", "instruction": "Keep compact tables"}])
        self.pull(HUMAN, page="001-old.md")
        self.model = None
        self.generate({"007-new.md": BASE.replace("Unchanged.", "New source.")})
        self.assertEqual(self.store.guidance(self.rel), {"007-new.md": ["Keep compact tables"]})
        self.assertIn("60°C", self.text("007-new.md"))

    def test_guidance_reaches_the_writer_prompts_and_leaves_them_unchanged_without_it(self):
        from graph.fast.repair import _writer_messages
        from graph.wiki import prompts

        args = dict(page_title="T", page_summary="S", index=1, count=1, source_start=1, source_end=2,
                    numbered_section="1: x", facts_text="", image_context="", output_language="日本語")
        plain = prompts.section_write_prompt(**args).render()
        with_guidance = prompts.section_write_prompt(**args, guidance=["Use ## for steps"]).render()
        self.assertEqual(prompts.section_write_prompt(**args, guidance=()).render(), plain)
        self.assertIn("Use ## for steps", with_guidance)
        self.assertNotIn("Use ## for steps", plain)
        intro = dict(page_title="T", page_summary="S", body="b", output_language="日本語")
        self.assertIn("Use ## for steps", prompts.intro_prompt(**intro, guidance=["Use ## for steps"]).render())
        self.assertEqual(prompts.intro_prompt(**intro).render(), prompts.intro_prompt(**intro, guidance=()).render())
        edit = dict(page_title="T", current_page="p", current_source="s", edits="e", image_context="", output_language="日本語")
        self.assertIn("Use ## for steps", prompts.incremental_page_edit_prompt(**edit, guidance=["Use ## for steps"]).render())
        self.assertEqual(prompts.incremental_page_edit_prompt(**edit).render(),
                         prompts.incremental_page_edit_prompt(**edit, guidance=()).render())
        page = SimpleNamespace(images=[], shell=SimpleNamespace(title="T"), masked_body="b", number=1, filename="001.md",
                               owner_evidence="o", reference_evidence="r", guidance=["Use ## for steps"])
        joined = lambda page: "".join(str(m.content) for m in _writer_messages(page, "T", "b", ["f"], attempt=1))
        self.assertIn("Use ## for steps", joined(page))
        page.guidance = []
        self.assertNotIn("人間の編集者", joined(page))

    def test_linker_output_is_removed_and_human_links_are_kept(self):
        linked = BASE.replace("Mode is AUTO.", "[Mode](002.md) is AUTO.")
        human = linked.replace("Maximum is 40°C.", "Maximum is 40°C (see [spec](https://example.test/spec)).")
        base, remote = self.store.separate(self.rel, "doc/001.md", linked, human)
        self.assertEqual(base, BASE)
        self.assertEqual(remote, BASE.replace("Maximum is 40°C.", "Maximum is 40°C (see [spec](https://example.test/spec))."))
        # A link-only or transport-only revision is not a human change.
        self.assertEqual(self.store.separate(self.rel, "doc/001.md", linked, linked.replace("[Mode](002.md)", "Mode")),
                         (BASE, BASE))
        self.assertEqual(self.store.separate(self.rel, "doc/001.md", BASE, BASE + "\n\n"), (BASE, BASE))
        self.assertEqual(unlinked("a [b](c) ![i](p.png) [d](e)", {"[b](c)"}), "a b ![i](p.png) [d](e)")

    def test_pull_removes_links_the_current_page_does_not_have(self):
        linked = BASE.replace("Mode is AUTO.", "[Mode](002.md) is AUTO.")
        write_text_atomic(self.folder / "001.md", linked)  # the linker rewrote the live page
        human = linked.replace("Maximum is 40°C.", "Maximum is 60°C.")
        base, remote = self.store.separate(self.rel, "doc/001.md", linked, human)
        self.store.accept(self.rel, "doc/001.md", "b1", "r1", base, remote)
        self.assertEqual(self.current(), HUMAN)
        self.assertEqual(self.text(), HUMAN)
        self.assertEqual(read_json(self.folder / "_planning" / "linker.json")["status"], "pending")

    def test_the_inline_link_manifest_keeps_rows_but_invalidates_the_rewritten_document(self):
        self.generate({"001.md": BASE, "002.md": "# Two\n\ntwo\n"})
        manifest = self.project.metadata / "cache" / "fast-inline-links" / "manifest.json"
        pages = {"doc/001.md": {"edits": []}, "doc/002.md": {"edits": []}, "other/001.md": {"edits": []}}
        write_json_atomic(manifest, {"pages": pages, "documents": {"doc": {"version": 1}, "other": {"version": 1}}})
        self.pull(HUMAN)
        row = read_json(manifest)
        self.assertEqual(sorted(row["pages"]), sorted(pages))  # link ownership stays for deterministic replay
        self.assertEqual(sorted(row["documents"]), ["other"])  # the rewritten document must be linked again

    def test_status_counts_without_page_text(self):
        self.model = FakeModel(classes=[{"kind": "structure", "instruction": "tidy"}])
        self.pull(HUMAN)
        status = self.store.status()
        self.assertEqual(status["counts"], {"blocked": 0, "documents": 1, "human_information": 1,
                                            "appendix_entries": 0, "guidance": 1})
        self.assertNotIn("60°C", json.dumps(status, ensure_ascii=False))


class IdentityAndDeleteTest(StoreCase):
    def journal(self, status="active"):
        key, stamp = self.store.identity(self.rel)
        write_json_atomic(self.store.root / "documents" / f"{key}.json", {
            "schema_version": 1, "key": key, "source_id": stamp["source_id"], "pages": {}, "captured_revisions": [],
            "edits": [{"edit_id": "hedit-1", "status": status}],
        })
        self.store._initialize()

    def test_a_document_with_a_live_legacy_journal_is_blocked(self):
        self.journal()
        with self.assertRaisesRegex(ValueError, "legacy human journal"):
            self.pull(HUMAN)
        with self.assertRaisesRegex(ValueError, "legacy human journal"):
            self.generate({"001.md": BASE})
        with self.assertRaisesRegex(ValueError, "legacy human journal"):
            self.store.assert_deletable(self.rel)
        self.assertFalse((self.store.root / "doc").exists())

    def test_finished_legacy_edits_do_not_block(self):
        self.journal("deleted")
        self.pull(HUMAN)
        self.assertEqual(self.text(), HUMAN)

    def _ledger(self, source_id):
        write_json_atomic(self.project.metadata / "pipeline.json",
                          {"schema_version": 4, "sources": {"doc.md": {"raw_rel": self.rel, "source_id": source_id, "id_seed": "source-1"}}})

    def test_a_different_source_at_the_same_path_inherits_nothing_and_is_blocked_by_human_text(self):
        self._ledger("src-1")
        self.pull(HUMAN)
        old = self.store._dir(self.rel)
        self._ledger("src-2")
        self.assertNotEqual(self.store._dir(self.rel), old)
        self.assertIsNone(self.store.state(self.rel))
        with self.assertRaisesRegex(ValueError, "different source"):
            self.generate({"001.md": BASE})
        with self.assertRaisesRegex(ValueError, "different source"):
            self.pull(HUMAN, revision="r9")
        # The same document with only a human deletion does not block a new source.
        self._ledger("src-1")
        self.pull(BASE.replace("Mode is AUTO.\n", ""), base=HUMAN, revision="r2")
        self.assertFalse(self.store.status()["counts"]["human_information"])
        self._ledger("src-3")
        self.generate({"001.md": BASE})
        self.assertEqual(self.text(), BASE)

    def test_a_moved_document_keeps_its_state_and_a_new_source_at_the_old_path_inherits_nothing(self):
        self._ledger("src-1")
        self.pull(HUMAN)
        key = self.store.identity(self.rel)[0]
        (Path(self.tmp.name) / "mount").mkdir()
        settings = SimpleNamespace(data_root=self.tmp.name, target_name="project",
                                   mount_path=str(Path(self.tmp.name) / "mount"), wiki_linker_enabled=False)
        publisher = SimpleNamespace(connection=SimpleNamespace(write_path="/docs"), move_document=lambda *args: {})
        job = SimpleNamespace(from_rel="doc.md", rel="moved.md", raw_rel="moved_md.md", target_sha256="", target_blob_oid="")
        with patch.object(pipeline, "_publisher", return_value=publisher), \
             patch("publisher.index.build_index", return_value={"failures": []}):
            self.assertEqual(pipeline.move_sources(settings, [job])["failures"], [])
        self.assertEqual(self.store.identity("moved_md.md")[0], key)
        self.assertEqual(self.store.state("moved_md.md")["raw_rel"], "moved_md.md")
        self.folder.mkdir()
        self.project.raw_file(self.rel).write_text("another source\n", encoding="utf-8")
        writer.write_source_stamp(self.folder, self.project.raw_file(self.rel), self.rel, identity_seed="src-2")
        self.generate({"001.md": BASE})  # not blocked by the moved document's human text
        self.assertIsNone(self.store.state(self.rel))

    def test_deleting_a_document_with_human_information_is_blocked_but_not_with_only_deletions(self):
        self.store.assert_deletable(self.rel)
        self.pull(HUMAN)
        with self.assertRaisesRegex(ValueError, "human information"):
            self.store.assert_deletable(self.rel)
        self.pull(BASE.replace("Mode is AUTO.\n", ""), base=HUMAN, revision="r2")
        self.pull(HUMAN.replace("Mode is AUTO.\n", "").replace("60", "40"), base=self.text(), revision="r3")
        self.store.assert_deletable(self.rel)

    def test_remove_sources_reports_a_blocked_delete_and_keeps_the_document(self):
        from publisher.ledger import Ledger

        self.pull(HUMAN)
        ledger = Ledger({"doc.md": {"raw_rel": self.rel}}, {}, {})
        done, _touched, failures = pipeline._remove_sources(self.project, ledger, {"doc.md": self.rel})
        self.assertEqual(done, [])
        self.assertTrue(any("human information" in failure for failure in failures))
        self.assertTrue(self.folder.exists())
        self.assertIn("doc.md", ledger.sources)


class RollbackReplayTest(unittest.TestCase):
    def test_replay_applies_candidate_captures_to_live_once_and_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = {}
            for name in ("live", "cand"):
                project = Project(Path(tmp) / name).ensure()
                folder = project.wiki_dir("doc.md")
                folder.mkdir(parents=True)
                project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
                writer.write_source_stamp(folder, project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
                write_text_atomic(folder / "001.md", BASE)
                apply_generated(project, "doc.md")
                projects[name] = project
            live, cand = projects["live"], projects["cand"]
            candidate_store = HumanStore(cand)
            candidate_store.accept("doc.md", "doc/001.md", "b1", "r1", BASE, HUMAN)
            second = HUMAN.replace("Intro.", "Intro 2.")
            candidate_store.accept("doc.md", "doc/001.md", "b1", "r2", HUMAN, second)
            # The candidate's own source generation must not be copied.
            write_text_atomic(cand.wiki_dir("doc.md") / "001.md", second.replace("Unchanged.", "Candidate source."))
            apply_generated(cand, "doc.md")
            store = HumanStore(live)
            store.replay_captured(cand, ["doc.md"])
            self.assertEqual((live.wiki_dir("doc.md") / "001.md").read_text(encoding="utf-8"), second)
            self.assertEqual(len(list((store._dir("doc.md") / "captures").glob("*.json"))), 2)
            store.replay_captured(cand, ["doc.md"])  # nothing is captured twice
            self.assertEqual((live.wiki_dir("doc.md") / "001.md").read_text(encoding="utf-8"), second)
            store.audit()


class PublicationSafetyTest(unittest.TestCase):
    def test_pull_command_dispatches_once_and_reports_failures(self):
        import main

        args = main.build_parser().parse_args(["pull", "--project", "example"])
        settings = object()
        with patch.object(main, "_settings", return_value=settings), patch.object(pipeline, "pull_growi_once", return_value={"done": [], "failures": []}) as pull:
            self.assertEqual(args.fn(args), 0)
        pull.assert_called_once_with(settings)

    def test_409_aborts_without_second_put_or_create(self):
        class Client:
            calls = 0
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="r0", path="/docs/001", body=wrap_page("old", page_id="b1"))
            async def update_page(self, *args):
                self.calls += 1
                raise GrowiAPIError(409, "PUT", "/page")
            async def create_page(self, *args):
                raise AssertionError("must not recreate on conflict")
        client = Client()
        with self.assertRaises(GrowiAPIError):
            asyncio.run(publish_pages(client, [{"path": "/docs/001", "local_path": "doc/001.md", "body": wrap_page("new", page_id="b1")}],
                                     mode="attach", write_path="/docs", expected_pages={"doc/001.md": {"page_id": "p", "revision_id": "r0", "marker_id": "b1"}}))
        self.assertEqual(client.calls, 1)

    def test_preflight_checks_entire_batch_before_any_write(self):
        class Client:
            async def get_page(self, **kwargs):
                if kwargs["page_id"] == "p2":
                    return GrowiPage(page_id="p2", revision_id="human", path="/docs/002", body=wrap_page("human", page_id="b2"))
                return GrowiPage(page_id="p1", revision_id="r0", path="/docs/001", body=wrap_page("old", page_id="b1"))
            async def update_page(self, *args):
                raise AssertionError("wrote before completing preflight")
            async def create_page(self, *args):
                raise AssertionError("created before completing preflight")
        items = [{"path": f"/docs/00{i}", "local_path": f"doc/00{i}.md", "body": wrap_page("new", page_id=f"b{i}")} for i in (1, 2)]
        rows = {f"doc/00{i}.md": {"page_id": f"p{i}", "revision_id": "r0", "marker_id": f"b{i}"} for i in (1, 2)}
        with self.assertRaisesRegex(RuntimeError, "changed before publication"):
            asyncio.run(publish_pages(Client(), items, mode="attach", write_path="/docs", expected_pages=rows))

    def test_publish_preserves_remote_only_code_and_comments_below_stamp(self):
        tail = "\nRemote note `AA29` <!-- human warning E-900 -->\n```markdown\n<!-- llm-wiki-bot-ref:example -->\n```\n"
        class Client:
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p1", revision_id="r0", path="/docs/001", body=wrap_page("Old.\n", page_id="b1") + tail)
            async def update_page(self, page_id, revision_id, body):
                return GrowiPage(page_id=page_id, revision_id="r1", path="/docs/001", body=body)
        pages = asyncio.run(publish_pages(Client(), [{"path": "/docs/001", "local_path": "doc/001.md", "body": wrap_page("New.\n", page_id="b1")}],
                                         mode="attach", write_path="/docs", expected_pages={"doc/001.md": {"page_id": "p1", "revision_id": "r0", "marker_id": "b1"}}))
        self.assertTrue(pages[0].body.endswith(tail))

    def test_publication_keeps_human_comments_and_drops_machine_comments(self):
        from graph.growi.client import _growi_markdown

        self.assertEqual(_growi_markdown('a <!-- human note --> b\n<!-- table-spec: {"x": 1} -->\n'),
                         "a <!-- human note --> b\n\n")

    def test_untracked_human_revision_is_not_adopted_as_bot_output(self):
        class Client:
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="human", path="/docs/001", body=wrap_page("human", page_id="b1"))
            async def update_page(self, *args):
                raise AssertionError("overwrote an unknown revision")
        with self.assertRaisesRegex(RuntimeError, "no inspected published baseline"):
            asyncio.run(publish_pages(Client(), [{"path": "/docs/001", "body": wrap_page("bot", page_id="b1")}],
                                     mode="attach", write_path="/docs"))

    def test_late_human_edit_blocks_page_deletion(self):
        class Client:
            async def list_all_pages(self, path):
                return [GrowiPage(page_id="p", revision_id="human", path="/docs/doc/001")]
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="human", path="/docs/doc/001", body=wrap_page("human", page_id="b1"))
            async def delete_pages(self, *args):
                raise AssertionError("deleted late human revision")
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
        with self.assertRaisesRegex(RuntimeError, "changed before deletion"):
            asyncio.run(publisher._trash_under("/docs/doc", keep=set(), expected_revisions={"p": "r0"}))

    def test_sync_captures_before_parse_and_publishes_after_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "mount"
            mount.mkdir()
            (mount / "doc.md").write_text("source", encoding="utf-8")
            settings = SimpleNamespace(data_root=str(Path(tmp) / "data"), target_name="project", mount_path=str(mount),
                                       ingest_mode="wiki", wiki_linker_enabled=False)
            calls = []
            def capture(*args, **kwargs):
                calls.append("capture")
                return [], [], set()
            def parse(*args, **kwargs):
                calls.append("parse")
                return "source"
            def generate(*args, **kwargs):
                calls.append("generate-and-overlay")
                return writer.WriteResult(target=Path(tmp), touched=[])
            def publish(*args, **kwargs):
                self.assertFalse(kwargs.get("captured"))  # publication pulls again: edits made during the build
                calls.append("publish")
                return []
            with (patch.object(pipeline, "_publisher", return_value=object()),
                  patch.object(pipeline, "_capture_remote", side_effect=capture),
                  patch.object(pipeline, "_parse", side_effect=parse),
                  patch.object(pipeline, "write_wiki_pages", side_effect=generate),
                  patch.object(pipeline, "_publish_sweep", side_effect=publish),
                  patch.object(pipeline, "_model", return_value=object()),
                  patch.object(pipeline, "Embedder", return_value=object()),
                  patch.object(pipeline, "_pending_link_rels", return_value=[])):
                result = pipeline.sync_once(settings)
            self.assertFalse(result["failures"])
            self.assertEqual(calls, ["capture", "parse", "generate-and-overlay", "publish"])


class TransactionSafetyTest(unittest.TestCase):
    def setUp(self):
        skip_final_judge(self)

    def test_store_is_part_of_candidate_clean_check_commit_and_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "live").ensure()
            base = ensure_repository(project)
            with candidate(project, "human-store") as staged:
                store = HumanStore(staged)
                digest = store.put("exact human fact")
                self.assertFalse(candidate_is_clean(staged, base))
                commit = commit_candidate(staged, "human fact", {})
                self.assertTrue(candidate_is_clean(staged, commit))
                promote(project, staged, commit)
                self.assertEqual(last_good(project), commit)
                self.assertEqual(HumanStore(project).get(digest), "exact human fact")

    def _failed_publication(self, *, late_human=False, missing_revision_record=False,
                            old_page_removed=False, late_delete=False):
        from graph.wiki.storage import sha256_text
        from publisher.ledger import Ledger, load_ledger, save_ledger

        with tempfile.TemporaryDirectory() as tmp:
            live = Project(Path(tmp) / "live", Path(tmp) / "mount").ensure()
            raw_rel = "doc_md.md"
            document = "doc.md"
            live.mount.mkdir()
            (live.mount / "doc.md").write_text("source\n", encoding="utf-8")
            live.raw_file(raw_rel).write_text("source\n", encoding="utf-8")
            folder = live.wiki_dir(raw_rel)
            folder.mkdir()
            writer.write_source_stamp(folder, live.raw_file(raw_rel), raw_rel, identity_seed="source-1")
            write_text_atomic(folder / "001.md", BASE)
            apply_generated(live, raw_rel)
            store = HumanStore(live)
            path = document + "/001.md"

            class Client:
                pages = {}
                updates = 0
                next_id = 1
                def copy(self, page):
                    return page.model_copy(deep=True) if page else None
                async def get_page(self, *, page_id=None, path=None):
                    if page_id:
                        return self.copy(self.pages.get(page_id))
                    return self.copy(next((p for p in self.pages.values() if p.path == path), None))
                async def create_page(self, path, body):
                    while f"p{self.next_id}" in self.pages:
                        self.next_id += 1
                    page = GrowiPage(page_id=f"p{self.next_id}", revision_id="created", path=path, body=body)
                    self.next_id += 1
                    self.pages[page.page_id] = page
                    return self.copy(page)
                async def update_page(self, page_id, revision_id, body):
                    page = self.pages[page_id]
                    if page.revision_id != revision_id:
                        raise GrowiAPIError(409, "PUT", "/page")
                    self.updates += 1
                    page.revision_id, page.body = f"bot-{self.updates}", body
                    return self.copy(page)
                async def list_all_pages(self, prefix):
                    return [self.copy(p) for p in self.pages.values() if p.path.startswith(prefix + "/")]
                async def delete_pages(self, revisions):
                    for page_id, revision in revisions.items():
                        if self.pages[page_id].revision_id != revision:
                            raise GrowiAPIError(409, "DELETE", "/page")
                    for page_id in revisions:
                        del self.pages[page_id]

            class FailOncePublisher(GrowiPublisher):
                fail = True
                def publish_documents(self, *args, **kwargs):
                    pages = super().publish_documents(*args, **kwargs)
                    if self.fail:
                        self.fail = False
                        raise RuntimeError("injected failure after remote writes")
                    return pages

            client = Client()
            publisher = FailOncePublisher(client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
            marker = publisher.page_marker_id(live, path)
            row = {"page_id": "p1", "revision_id": "r0", "marker_id": marker, "growi_path": "/docs/doc/001"}
            store.remember_page(path, row, BASE, BASE, published=True)
            client.pages["p1"] = GrowiPage(page_id="p1", revision_id="r0", path=row["growi_path"], body=wrap_page(BASE, page_id=marker))
            save_ledger(live.metadata / "pipeline.json", Ledger(
                {"doc.md": {"raw_rel": raw_rel, "source_id": "source-1", "id_seed": "source-1", "source_sha256": sha256_text("source\n")}},
                {document: {"raw_rel": raw_rel, "content_sha256": pipeline._content_hash(folder), "growi_path": "/docs/doc"}},
                {path: row},
            ))
            ensure_repository(live)
            client.pages["p1"].body = wrap_page(HUMAN, page_id=marker)
            client.pages["p1"].revision_id = "human-r1"
            known = {}
            def record(page):
                known.setdefault(page.page_id, set()).add(page.revision_id)
            def rebuild(**kwargs):
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                write_text_atomic(docs / ("002.md" if old_page_removed else "001.md"), BASE.replace("40°C", "55°C"))
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))
            live_settings = SimpleNamespace(data_root=tmp, target_name="live", mount_path=str(live.mount), ingest_mode="wiki", wiki_linker_enabled=False)
            with candidate(live, "failed-human-publish") as staged:
                write_text_atomic(staged.mount / "doc.md", "changed source\n")
                settings = SimpleNamespace(data_root=str(staged.root.parent), target_name=staged.root.name,
                                           mount_path=str(staged.mount), ingest_mode="wiki", wiki_linker_enabled=False)
                with (patch.object(pipeline, "_publisher", return_value=publisher),
                      patch.object(pipeline, "_model", return_value=object()),
                      patch.object(pipeline, "Embedder", return_value=object()),
                      patch.object(pipeline, "_parse", return_value="changed source\n"),
                      patch.object(writer, "build_wiki_output", side_effect=rebuild),
                      patch("publisher.index.build_index", return_value={"failures": []})):
                    result = pipeline.sync_once(settings, only=["doc.md"], on_revision=record)
                    self.assertTrue(result["failures"])
                    updated = next(page for page in client.pages.values() if page.path.endswith("/002" if old_page_removed else "/001"))
                    self.assertIn("55°C", updated.body)
                    self.assertIn("60°C", updated.body)  # the capture was applied on the new source
                    if missing_revision_record:
                        known["p1"] = set()
                    if late_delete:
                        del client.pages["p1"]
                        known.pop("p1", None)
                        staged_ledger = load_ledger(staged.metadata / "pipeline.json")
                        staged_ledger.published_pages.pop(path)
                        save_ledger(staged.metadata / "pipeline.json", staged_ledger)
                        remaining = {key: value.body for key, value in client.pages.items()}
                        with self.assertRaisesRegex(RuntimeError, "disappeared during publication"):
                            pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        self.assertEqual({key: value.body for key, value in client.pages.items()}, remaining)
                    elif late_human:
                        client.pages["p1"].body = wrap_page("Late human E-902.\n", page_id=marker)
                        client.pages["p1"].revision_id = "late-human"
                        updates = client.updates
                        with self.assertRaisesRegex(RuntimeError, "unknown GROWI revision"):
                            pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        self.assertEqual(client.updates, updates)
                        self.assertIn("Late human E-902.", client.pages["p1"].body)
                    else:
                        pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        restored = load_ledger(live.metadata / "pipeline.json").published_pages[path]
                        restored_page = client.pages[restored["page_id"]]
                        self.assertIn("60°C", restored_page.body)
                        self.assertNotIn("55°C", restored_page.body)
                        self.assertEqual(restored_page.body.count("60°C"), 1)
                        self.assertEqual(len(client.pages), 1)
                        self.assertTrue(candidate_is_clean(live, last_good(live)))
                        self.assertEqual(restored["revision_id"], restored_page.revision_id)

    def test_failed_publication_restores_old_source_with_the_captured_human_change(self):
        self._failed_publication()

    def test_unknown_human_revision_during_rollback_is_never_overwritten(self):
        self._failed_publication(late_human=True)

    def test_unrecorded_bot_revision_recovers_only_with_exact_prepared_body(self):
        self._failed_publication(missing_revision_record=True)

    def test_acknowledged_bot_page_deletion_can_be_restored(self):
        self._failed_publication(old_page_removed=True)

    def test_missing_base_only_page_without_bot_deletion_is_not_recreated(self):
        self._failed_publication(late_delete=True)


class TierIntegrationTest(unittest.TestCase):
    def setUp(self):
        skip_final_judge(self)

    def test_human_change_survives_tiers_one_two_three_and_zero(self):
        from tests import test_update_tiers as tiers
        with tempfile.TemporaryDirectory() as tmp:
            page = "# P1\n\nline 5\n\nOperator limit 40°C.\n"
            project, rel, state = tiers.make_project(tmp, [(1, tiers.COUNT, page)], tiers.OLD_SOURCE)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE, encoding="utf-8")
            folder = project.wiki_dir(rel)
            folder.mkdir(parents=True)
            writer.write_source_stamp(folder, project.raw_file(rel), rel)
            write_text_atomic(folder / "001.md", page)
            apply_generated(project, rel)
            store = HumanStore(project)
            path = folder.relative_to(project.wiki).as_posix() + "/001.md"
            store.accept(rel, path, "b1", "r1", page, page.replace("40°C", "60°C"))
            project.raw_file(rel).write_text(tiers.OLD_SOURCE.replace("line 5\n", "line 5 changed\n"), encoding="utf-8")
            model = tiers.PatchModel(patches=[{"edit_ids": [1], "before": "line 5", "after": "line 5 changed"}])
            first = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=model, embedder=None)
            self.assertEqual(first.tier, 1)
            self.assertNotIn("60°C", (state / "wiki" / "001.md").read_text())
            self.assertEqual((folder / "001.md").read_text().count("60°C"), 1)
            self.assertIn("line 5 changed", (folder / "001.md").read_text())
            zero = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None)
            self.assertEqual(zero.tier, 0)
            self.assertEqual((folder / "001.md").read_text().count("60°C"), 1)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE.replace("line 5\n", "line 5 second\n"), encoding="utf-8")
            def regenerate(**kwargs):
                write_text_atomic(state / "wiki" / "001.md", "# P1\n\nFresh source 55°C.\n")
                from graph.wiki.export import export_ingest_layout
                return SimpleNamespace(out_dir=export_ingest_layout(state, kwargs["out_dir"], document_name=rel))
            with patch.object(writer, "build_wiki_output", side_effect=regenerate), patch("graph.wiki.incremental.FULL_MIN_REGEN_SHARE", 1.0):
                second = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=tiers.PatchModel(fail=True), embedder=None)
            self.assertEqual(second.tier, 2)
            self.assertIn("60°C", (folder / "001.md").read_text())
            self.assertIn("55°C", (folder / "001.md").read_text())
            def rebuild(**kwargs):
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                write_text_atomic(docs / "001.md", "# P1\n\nFully rebuilt source.\n")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))
            with patch.object(writer, "build_wiki_output", side_effect=rebuild):
                third = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None, resume=False)
            self.assertEqual(third.tier, 3)
            # The rebuild deleted the block the human edited: the edit moves to the appendix page.
            self.assertEqual(sum(path.read_text().count("60°C") for path in folder.glob("*.md")), 1)
            self.assertIn("Fully rebuilt source.", (folder / "001.md").read_text())

    def test_guidance_reaches_the_builders_and_state_does_not_exist_for_untouched_documents(self):
        from tests import test_update_tiers as tiers
        with tempfile.TemporaryDirectory() as tmp:
            page = "# P1\n\nline 5\n"
            project, rel, _state = tiers.make_project(tmp, [(1, tiers.COUNT, page)], tiers.OLD_SOURCE)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE, encoding="utf-8")
            folder = project.wiki_dir(rel)
            folder.mkdir(parents=True)
            writer.write_source_stamp(folder, project.raw_file(rel), rel)
            write_text_atomic(folder / "001.md", page)
            apply_generated(project, rel)
            self.assertFalse((project.metadata / "human-sync" / "doc").exists())
            model = FakeModel(classes=[{"kind": "structure", "instruction": "Use a table"}])
            HumanStore(project).accept(rel, f"{folder.name}/001.md", "b1", "r1", page, page + "\n", model=model)
            seen = []
            def build(**kwargs):
                seen.append(kwargs["human_guidance"])
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                write_text_atomic(docs / "001.md", "# P1\n\nRebuilt.\n")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))
            project.raw_file(rel).write_text(tiers.OLD_SOURCE.replace("line 5\n", "line 5 second\n"), encoding="utf-8")
            with patch.object(writer, "build_wiki_output", side_effect=build):
                writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None, resume=False)
            self.assertEqual(seen, [{"001.md": ["Use a table"]}])


class PartialPublicationTest(unittest.TestCase):
    """A standalone publish that dies mid-batch must never look like human intent."""

    raw_rel = "doc_md.md"
    document = "doc.md"
    bot = BASE.replace("40°C", "55°C")

    def _harness(self, tmp, *, pages=("001.md", "002.md"), ledgered=("001.md", "002.md")):
        from graph.wiki.storage import sha256_text
        from publisher.ledger import Ledger, save_ledger

        live = Project(Path(tmp) / "live", Path(tmp) / "mount").ensure()
        live.mount.mkdir()
        (live.mount / "doc.md").write_text("source\n", encoding="utf-8")
        live.raw_file(self.raw_rel).write_text("source\n", encoding="utf-8")
        folder = live.wiki_dir(self.raw_rel)
        folder.mkdir()
        writer.write_source_stamp(folder, live.raw_file(self.raw_rel), self.raw_rel, identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        for name in pages:
            write_text_atomic(folder / name, BASE)
        apply_generated(live, self.raw_rel)
        store = HumanStore(live)

        class Client:
            def __init__(self):
                self.pages = {}
                self.updates = 0
                self.next_id = 3
                self.fail_update = ""
                self.lose_response = ""
                self.human_edit_on_put = ""
            def copy(self, page):
                return page.model_copy(deep=True) if page else None
            async def get_page(self, *, page_id=None, path=None):
                if page_id:
                    return self.copy(self.pages.get(page_id))
                return self.copy(next((p for p in self.pages.values() if p.path == path), None))
            async def create_page(self, path, body):
                page = GrowiPage(page_id=f"p{self.next_id}", revision_id="created", path=path, body=body)
                self.next_id += 1
                self.pages[page.page_id] = page
                if self.lose_response == "create":
                    raise ConnectionError("lost response after the server accepted the write")
                return self.copy(page)
            async def update_page(self, page_id, revision_id, body):
                page = self.pages[page_id]
                if self.human_edit_on_put == page_id:
                    # A human save lands after our preflight, so the PUT is rejected.
                    page.body = page.body.replace("Mode is AUTO.", "Mode is MANUAL.")
                    page.revision_id = "human-1"
                if page.revision_id != revision_id:
                    raise GrowiAPIError(409, "PUT", "/page")
                if self.fail_update == page_id:
                    raise GrowiAPIError(500, "PUT", "/page")
                self.updates += 1
                page.revision_id, page.body = f"bot-{self.updates}", body
                if self.lose_response == page_id:
                    raise ConnectionError("lost response after the server accepted the write")
                return self.copy(page)
            async def list_all_pages(self, prefix):
                return [self.copy(p) for p in self.pages.values() if p.path.startswith(prefix + "/")]
            async def delete_pages(self, revisions):
                for page_id, revision in revisions.items():
                    if self.pages[page_id].revision_id != revision:
                        raise GrowiAPIError(409, "DELETE", "/page")
                for page_id in revisions:
                    del self.pages[page_id]
            async def revision_bodies(self, page_id, limit=100):
                return [self.pages[page_id].body]  # a rejected write never became a revision

        client = Client()
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
        document = live.wiki_dir(self.raw_rel).relative_to(live.wiki).as_posix()
        doc_path = publisher.doc_path(live, self.raw_rel)
        rows = {}
        for index, page in enumerate(publisher._document_pages(live, self.raw_rel), start=1):
            path, page_path = page["local_path"], page["path"]
            if Path(path).name not in ledgered:
                continue
            marker = publisher.page_marker_id(live, path)
            row = {"page_id": f"p{index}", "revision_id": "r0", "marker_id": marker, "growi_path": page_path}
            store.remember_page(path, row, BASE, BASE, published=True)
            rows[path] = row
            client.pages[row["page_id"]] = GrowiPage(page_id=row["page_id"], revision_id="r0",
                                                     path=page_path, body=wrap_page(BASE, page_id=marker))
        save_ledger(live.metadata / "pipeline.json", Ledger(
            {self.document: {"raw_rel": self.raw_rel, "source_id": "source-1", "id_seed": "source-1",
                             "source_sha256": sha256_text("source\n")}},
            {document: {"raw_rel": self.raw_rel, "content_sha256": pipeline._content_hash(folder),
                        "growi_path": doc_path}},
            rows,
        ))
        ensure_repository(live)
        settings = SimpleNamespace(data_root=tmp, target_name="live", mount_path=str(live.mount),
                                   ingest_mode="wiki", wiki_linker_enabled=False)
        return live, folder, store, client, publisher, settings, rows

    def _ledger(self, live):
        from publisher.ledger import load_ledger

        return load_ledger(live.metadata / "pipeline.json")

    def _publish(self, publisher, settings):
        with (patch.object(pipeline, "_publisher", return_value=publisher),
              patch("publisher.index.build_index", return_value={"failures": []})):
            return pipeline.publish_only(settings)

    def _pull(self, publisher, settings):
        with patch.object(pipeline, "_publisher", return_value=publisher):
            return pipeline.pull_growi_once(settings)

    def _regenerate(self, live, folder, pages):
        """Mirror the real writer: export the new pages, then apply_generated."""
        for name, text in pages.items():
            write_text_atomic(folder / name, text)
        apply_generated(live, self.raw_rel)

    def test_moving_a_document_when_growi_moves_child_pages_with_the_document_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, _store, client, publisher, _settings, rows = self._harness(tmp)
            old_doc = publisher.doc_path(live, self.raw_rel)
            client.pages["root"] = GrowiPage(page_id="root", revision_id="r0", path=old_doc, body="")

            async def rename_page(page_id, revision_id, path, recursively=False):
                if any(page.path == path for page in client.pages.values()):
                    raise GrowiAPIError(409, "PUT", "/pages/rename")
                old = client.pages[page_id].path
                for page in client.pages.values():  # like GROWI: the children move too
                    if page.path == old or page.path.startswith(old + "/"):
                        page.path = path + page.path[len(old):]
                return client.copy(client.pages[page_id])

            client.rename_page = rename_page
            shutil.move(str(folder), str(live.wiki_dir("moved_md.md")))  # move_sources moves the folder first
            moved = publisher.move_document(live, self.raw_rel, "moved_md.md", rows)
            new_doc = publisher.doc_path(live, "moved_md.md")
            self.assertEqual(sorted(page.page_id for page in moved.values()), ["p1", "p2"])
            self.assertTrue(all(client.pages[p].path.startswith(new_doc + "/") for p in ("p1", "p2")))

    def test_unrecorded_published_page_is_not_captured_as_a_human_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.fail_update = "p1"  # 002 publishes, then 001 fails the batch.
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertNotIn("55°C", client.pages["p1"].body)
            marker = publisher.page_marker_id(live, f"{self.document}/002.md")
            prepared = store.page(marker)
            self.assertTrue(prepared["prepared_attempt_id"].startswith("hattempt-"))
            self.assertEqual(prepared["publication_confirmation"]["revision"], client.pages["p2"].revision_id)
            self.assertEqual(prepared["attempt_history"][-1]["status"], "confirmed")
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"], "r0")
            # A failed publish still checkpoints durable state, so recovery is not blocked.
            self.assertTrue(candidate_is_clean(live, last_good(live)))
            self.assertFalse(self._pull(publisher, settings)["failures"])
            self.assertIsNone(store.state(self.raw_rel))
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"],
                             client.pages["p2"].revision_id)
            settled = store.page(marker)
            self.assertNotIn("prepared_attempt_id", settled)
            self.assertEqual(settled["attempt_history"][-1]["status"], "recovered_exact")
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/001.md"]["revision_id"], "r0")
            client.fail_update = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p1"].body)
            self.assertIsNone(store.state(self.raw_rel))
            self.assertTrue(candidate_is_clean(live, last_good(live)))

    def test_lost_update_response_recovers_from_the_exact_prepared_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.lose_response = "p2"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertFalse(self._pull(publisher, settings)["failures"])
            self.assertIsNone(store.state(self.raw_rel))
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"],
                             client.pages["p2"].revision_id)
            client.lose_response = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertEqual(client.updates, 2)
            self.assertTrue(candidate_is_clean(live, last_good(live)))

    def test_created_page_with_lost_create_response_is_published_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp, ledgered=("001.md",))
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.lose_response = "create"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertNotIn(f"{self.document}/002.md", self._ledger(live).published_pages)
            client.lose_response = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            published = self._ledger(live).published_pages[f"{self.document}/002.md"]
            self.assertEqual(published["page_id"], "p3")
            self.assertEqual(client.pages["p3"].body.count("55°C"), 1)
            self.assertIsNone(store.state(self.raw_rel))

    def test_an_edit_saved_during_generation_is_captured_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, _folder, _store, client, publisher, settings, _rows = self._harness(tmp)
            (live.mount / "doc.md").write_text("changed source\n", encoding="utf-8")

            def rebuild(**kwargs):
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                for name in ("001.md", "002.md"):
                    write_text_atomic(docs / name, self.bot)
                page = client.pages["p1"]  # a human saves while the document is being generated
                page.body, page.revision_id = page.body.replace("Mode is AUTO.", "Mode is MANUAL."), "human-during-build"
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with (patch.object(pipeline, "_publisher", return_value=publisher),
                  patch.object(pipeline, "_model", return_value=object()),
                  patch.object(pipeline, "Embedder", return_value=object()),
                  patch.object(pipeline, "_parse", return_value="changed source\n"),
                  patch.object(writer, "build_wiki_output", side_effect=rebuild),
                  patch("publisher.index.build_index", return_value={"failures": []})):
                self.assertEqual(pipeline.sync_once(settings, only=["doc.md"])["failures"], [])
            self.assertIn("Mode is MANUAL.", client.pages["p1"].body)
            self.assertIn("55°C", client.pages["p1"].body)

    def test_a_lost_create_recorded_without_a_revision_is_recovered_before_preflight(self):
        from publisher.ledger import save_ledger

        with tempfile.TemporaryDirectory() as tmp:
            live, _folder, _store, client, publisher, settings, _rows = self._harness(tmp, ledgered=())
            ledger = self._ledger(live)
            ledger.published_documents.clear()  # a new document
            save_ledger(live.metadata / "pipeline.json", ledger)
            client.lose_response = "create"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            created = next(iter(client.pages.values()))
            path = next(p for p in (f"{self.document}/001.md", f"{self.document}/002.md")
                        if publisher.page_marker_id(live, p) in created.body)
            ledger = self._ledger(live)  # what inventory records for an exact prepared create
            ledger.published_pages[path] = {"growi_path": created.path, "page_id": created.page_id, "revision_id": "",
                                            "marker_id": publisher.page_marker_id(live, path)}
            save_ledger(live.metadata / "pipeline.json", ledger)
            client.lose_response = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertEqual(len(client.pages), 2)  # the lost create was adopted, not duplicated
            self.assertEqual(self._ledger(live).published_pages[path]["revision_id"], created.revision_id)

    def test_human_edit_on_an_unledgered_created_page_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp, ledgered=("001.md",))
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.lose_response = "create"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            client.pages["p3"].body = wrap_page(self.bot + "Reviewer note E-902.\n", page_id=marker)
            client.pages["p3"].revision_id = "late-human"
            self.assertTrue([problem for problem in self._publish(publisher, settings)["failures"]
                             if "no inspected published baseline" in problem])
            self.assertIn("Reviewer note E-902.", client.pages["p3"].body)
            self.assertEqual(store.page(marker)["publication_error"], "missing_published_snapshot")
            self.assertIn("Reviewer note E-902.", store.get(store.page(marker)["observed_remote_blob"]))

    def test_prepared_write_needs_exact_body_page_identity_and_a_moved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, _folder, store, _client, publisher, _settings, _rows = self._harness(tmp)
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            effective = self.bot.replace("Mode is AUTO.", "Mode is MANUAL.")
            store.save_page(dict(store.page(marker), prepared_path="/docs/doc/002", prepared_page_id="p2",
                                 prepared_revision="r0", prepared_remote_blob=store.put("exact prepared body"),
                                 prepared_local_blob=store.put(effective)))
            ours = GrowiPage(page_id="p2", revision_id="bot-1", path="/docs/doc/002", body="exact prepared body")
            self.assertTrue(store.prepared_match(marker, ours))
            self.assertEqual(store.prepared_match(marker, ours.model_copy(update={"body": "someone changed it"})), {})
            self.assertEqual(store.prepared_match(marker, ours.model_copy(update={"page_id": "p9"})), {})
            # A page still at the inspected revision never received our write.
            stalled = ours.model_copy(update={"revision_id": "r0"})
            self.assertEqual(store.prepared_match(marker, stalled), {})
            self.assertEqual(store.account_prepared(dict(store.page(marker)), stalled), {})
            self.assertEqual(store.account_prepared(dict(store.page(marker)), ours), {"exact": True})
            for key in ("prepared_path", "prepared_page_id", "prepared_revision",
                        "prepared_remote_blob", "prepared_local_blob"):
                self.assertNotIn(key, store.page(marker))
            store.save_page(dict(store.page(marker), prepared_path="/docs/doc/002", prepared_page_id="p2",
                                 prepared_revision="bot-1", prepared_remote_blob=store.put("exact prepared body"),
                                 prepared_local_blob=store.put(effective)))
            late = GrowiPage(page_id="p2", revision_id="late-human", path="/docs/doc/002", body="someone changed it")
            rebased = store.account_prepared(dict(store.page(marker)), late)
            self.assertFalse(rebased["exact"])
            self.assertEqual((rebased["remote"], rebased["local"]), ("exact prepared body", effective))

    def test_human_edit_after_a_partial_write_is_captured_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.fail_update = "p1"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            human = client.pages["p2"].body.replace("Mode is AUTO.", "Mode is MANUAL.")
            self.assertIn("55°C", human)
            client.pages["p2"].body = human
            client.pages["p2"].revision_id = "late-human"
            self.assertFalse(self._pull(publisher, settings)["failures"])
            # Only the human delta is intent: the bot's 40 to 55 change is generated state.
            self.assertEqual(store.state(self.raw_rel)["appendix"], None)
            page = (folder / "002.md").read_text(encoding="utf-8")
            self.assertIn("Mode is MANUAL.", page)
            self.assertEqual(page.count("55°C"), 1)
            self.assertEqual((folder / "001.md").read_text(encoding="utf-8"), self.bot)
            self.assertEqual(client.pages["p2"].body, human)
            client.fail_update = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)

    def test_human_edit_on_a_page_whose_publish_got_an_error_is_applied_on_the_newer_local_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, _store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.fail_update = "p2"  # 002 is answered with HTTP 500; the write never lands
            self.assertTrue(self._publish(publisher, settings)["failures"])
            client.pages["p2"].body = client.pages["p2"].body.replace("Mode is AUTO.", "Mode is MANUAL.")
            client.pages["p2"].revision_id = "late-human"
            self.assertFalse(self._pull(publisher, settings)["failures"])
            page = (folder / "002.md").read_text(encoding="utf-8")
            self.assertIn("Mode is MANUAL.", page)
            self.assertEqual(page.count("55°C"), 1)  # the newer local generation stays
            client.fail_update = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)
            self.assertIn("55°C", client.pages["p2"].body)

    def test_409_race_uses_the_old_baseline_not_the_write_that_never_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            client.human_edit_on_put = "p2"
            self.assertTrue([problem for problem in self._publish(publisher, settings)["failures"]
                             if problem.startswith("publish:")])
            self.assertEqual(store.page(marker)["publication_error"], "revision_race")
            self.assertNotIn("55°C", client.pages["p2"].body)
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)
            self.assertFalse(self._pull(publisher, settings)["failures"])
            # The rejected transport is not human intent: the old published baseline
            # stays authoritative for the human delta, applied on the newer local text.
            page = (folder / "002.md").read_text(encoding="utf-8")
            self.assertIn("55°C", page)
            self.assertIn("Mode is MANUAL.", page)
            self.assertNotIn("40°C", page)
            self.assertEqual(self._ledger(live).published_pages[path]["revision_id"], "human-1")
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertNotIn("publication_error", store.page(marker))

    def test_source_change_after_a_rebased_edit_does_not_conflict_or_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot})
            client.fail_update = "p1"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            client.pages["p2"].body = client.pages["p2"].body.replace("Mode is AUTO.", "Mode is MANUAL.")
            client.pages["p2"].revision_id = "late-human"
            self.assertFalse(self._pull(publisher, settings)["failures"])
            # The next build legitimately carries the published bot change.
            for page in folder.glob("*.md"):
                page.unlink()
            self._regenerate(live, folder, {"001.md": self.bot, "002.md": self.bot.replace("Unchanged.", "Revised.")})
            page = (folder / "002.md").read_text(encoding="utf-8")
            self.assertEqual(page.count("55°C"), 1)
            self.assertNotIn("40°C", page)
            self.assertNotIn("元文書", page)
            self.assertIn("Mode is MANUAL.", page)
            self.assertIn("Revised.", page)


class TransportSpellingMergeTest(unittest.TestCase):
    """GROWI holds inline code without backticks; adjacent spelling-only lines must not
    swallow a human token edit when the capture rebases it onto the local page."""

    def test_human_edit_inside_a_multi_line_spelling_hunk_is_captured(self):
        remote = "- 供給温度は通常 18 °C に設定します。\n- 戻り温度が 26 °C を超えた場合は確認する。\n"
        human = "- 供給温度は通常 18 °C に設定します（夏季は 16 °C）。\n- 戻り温度が 26 °C を超えた場合は確認する。\n"
        local = "- 供給温度は通常 `18 °C` に設定します。\n- 戻り温度が `26 °C` を超えた場合は確認する。\n"
        merged, status = merge(remote, human, local)
        self.assertEqual(status, "active")
        self.assertEqual(merged, "- 供給温度は通常 `18 °C` に設定します（夏季は 16 °C）。\n- 戻り温度が `26 °C` を超えた場合は確認する。\n")

    def test_deleting_a_paragraph_whose_spelling_differs_only_in_transport_is_not_a_conflict(self):
        # GROWI holds a blank line for the stripped comment and no backticks; the human deletes that section.
        remote = "# T\n\nkeep hxctl status\n\n## Memo\n\n\n\nrun hxctl diag now\n\n## End\n"
        local = "# T\n\nkeep `hxctl status`\n\n## Memo\n\n<!-- memo -->\n\nrun `hxctl diag` now\n\n## End\n"
        human = "# T\n\nkeep hxctl status\n\n## End\n"
        self.assertEqual(merge(remote, human, local), ("# T\n\nkeep `hxctl status`\n\n## End\n", "active"))

    def test_same_token_still_conflicts_inside_a_multi_line_hunk(self):
        base = "- 高温警報: 60 °C\n- 低圧警報: 0.15 MPa\n"
        human = "- 高温警報: 65 °C\n- 低圧警報: 0.15 MPa\n"
        source = "- 高温警報: 55 °C\n- 低圧警報: 0.20 MPa\n"
        self.assertEqual(merge(base, human, source)[1], "conflict")


if __name__ == "__main__":
    unittest.main()
