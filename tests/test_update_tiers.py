"""Fast tests for tiered wiki updates (no parser, LLM, embedder, or GROWI)."""

from __future__ import annotations

import base64
import json
import random
import tempfile
import time
import unittest
from difflib import SequenceMatcher
from pathlib import Path

from graph.wiki import incremental as inc
from graph.wiki.pipeline import SeedPage, _load_seed_plan, _plan_pages, _resume_rewritten_page
from graph.wiki.storage import read_json, sha256_text
from publisher import pipeline, queue


def make_run(root: Path, source: str, page_ranges: list[tuple[int, int]], *, titles=None, paths=None,
             references=None, prompt_version: str = "test") -> Path:
    """A minimal run state: plan.json, page sidecars and page files."""
    run = root / "run"
    (run / "state" / "pages").mkdir(parents=True)
    (run / "wiki").mkdir()
    (run / "source").mkdir()
    (run / "source" / "original.md").write_text(source, encoding="utf-8")
    pages = []
    for number, (first, last) in enumerate(page_ranges, 1):
        name = f"{number:03d}-page.md"
        title = (titles or {}).get(number, f"Page {number}")
        pages.append({
            "number": number, "filename": name, "title": title, "chapter": "",
            "path": (paths or {}).get(number, []), "owner_ranges": [[first, last]],
            "reference_ranges": (references or {}).get(number, []),
        })
        body = f"# {title}\n\nbody {number}\n"
        (run / "wiki" / name).write_text(body, encoding="utf-8")
        (run / "state" / "pages" / f"{number:03d}.json").write_text(
            json.dumps({"filename": name, "content_sha256": sha256_text(body)}), encoding="utf-8")
    (run / "state" / "plan.json").write_text(json.dumps({
        "source_line_count": len(inc.source_lines(source)), "source_sha256": sha256_text(source),
        "prompt_version": prompt_version, "pages": pages,
    }), encoding="utf-8")
    return run


def resumable(run: Path, new_text: str) -> bool:
    plan = read_json(run / "state" / "plan.json")
    return _load_seed_plan(
        run / "state" / "plan.json", source_sha256=sha256_text(new_text),
        source_line_count=len(inc.source_lines(new_text)), prompt_version=plan["prompt_version"],
    ) is not None


class IncrementalDecisionTest(unittest.TestCase):
    def test_required_resume_accepts_stale_prompt_versions_but_validates_content(self) -> None:
        source = "one\ntwo\n"
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 2)], prompt_version="old-plan")
            plan_path = run / "state" / "plan.json"
            args = dict(
                source_sha256=sha256_text(source),
                source_line_count=2,
                prompt_version="new-plan",
            )
            self.assertIsNone(_load_seed_plan(plan_path, **args))
            self.assertIsNotNone(_load_seed_plan(plan_path, accept_stale_version=True, **args))

            page = SeedPage(1, "Page 1", "", "", [(1, 2)], "001-page.md", "page-001")
            output = run / "wiki" / page.filename
            state = run / "state" / "pages" / "001.json"
            stored = read_json(state)
            stored["rewrite_version"] = "old-rewrite"
            state.write_text(json.dumps(stored), encoding="utf-8")
            self.assertIsNone(_resume_rewritten_page(
                output, state, page, rewrite_version="new-rewrite", source_line_count=2
            ))
            self.assertIsNotNone(_resume_rewritten_page(
                output, state, page, rewrite_version="new-rewrite", source_line_count=2,
                accept_stale_version=True,
            ))
            output.write_text("tampered\n", encoding="utf-8")
            self.assertIsNone(_resume_rewritten_page(
                output, state, page, rewrite_version="new-rewrite", source_line_count=2,
                accept_stale_version=True,
            ))

    def test_replan_keeps_previous_names_by_title_stem(self) -> None:
        from graph.wiki.schemas import CompiledSeedPlan
        from graph.wiki.wire import SeedRange

        plan = CompiledSeedPlan(pages=[
            SeedRange(title="Alpha", source_start=1, source_end=1),
            SeedRange(title="Inserted", source_start=2, source_end=2),
            SeedRange(title="Beta", source_start=3, source_end=3),
        ])
        pages = _plan_pages(plan, keep=["001-Alpha.md", "002-Beta.md"])
        self.assertEqual(
            [page.filename for page in pages],
            ["001-Alpha.md", "002-Inserted.md", "002-Beta.md"],
        )

    def test_image_spellings_share_byte_identity_and_wrapper_only_diff_is_tier_zero(self) -> None:
        from graph.common.images import canonicalize_images
        from graph.wiki.images import (
            count_equivalent_units,
            extract_image_units,
            image_identity_counts,
        )

        payload = base64.b64encode(b"same image bytes").decode("ascii")
        old_image = f'<embed src="data:image/png;base64,{payload}" style="width:1in" />'
        new_image = (
            '<image-unit>\n'
            f'  <image-media><img alt="changed metadata" src="data:image/png;base64,{payload}"></image-media>\n'
            '  <image-description>generated wording is not image identity</image-description>\n'
            '</image-unit>'
        )
        old = f"# Page\n\n<td>{old_image}</td>\n\ntail"
        new = f"# Page\n\n<td>{new_image}</td>\n\ntail"

        self.assertEqual(
            [line for line in canonicalize_images(old).splitlines() if line],
            [line for line in canonicalize_images(new).splitlines() if line],
        )
        old_unit = extract_image_units(old.splitlines())[0]
        new_unit = extract_image_units(new.splitlines())[0]
        self.assertEqual(old_unit.unit_sha256, new_unit.unit_sha256)
        self.assertEqual(count_equivalent_units(new, old_unit), 1)
        self.assertEqual(count_equivalent_units(new + "\n" + old_image, old_unit), 2)
        self.assertEqual(image_identity_counts(new + "\n" + old_image)[old_unit.unit_sha256], 2)
        legacy_ref = '<embed src="media/media/image10.png" style="width:1in" />'
        wrapped_ref = (
            '<image-unit><image-media><img src="./media/media/image10.png" alt="x">'
            '</image-media><image-description>y</image-description></image-unit>'
        )
        self.assertEqual(
            extract_image_units([legacy_ref])[0].unit_sha256,
            extract_image_units([wrapped_ref])[0].unit_sha256,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), old, [(1, len(inc.source_lines(old)))])
            decision = inc.decide_update(run, old, new, kind="docx")
            self.assertEqual((decision.tier, decision.reason), (0, "image-representation"))
            self.assertTrue(decision.hunks)  # retained solely to remap physical lines
            inc.apply_update(run, decision, new)
            plan = read_json(run / "state" / "plan.json")
            self.assertEqual(plan["source_line_count"], len(inc.source_lines(new)))
            self.assertEqual(plan["pages"][0]["owner_ranges"], [[1, len(inc.source_lines(new))]])
            self.assertTrue(resumable(run, new))

    def test_changed_image_bytes_remain_a_real_change(self) -> None:
        first = base64.b64encode(b"first image").decode("ascii")
        second = base64.b64encode(b"second image").decode("ascii")
        old = f'# Page\n\n<img src="data:image/png;base64,{first}">\n\ntail'
        new = f'# Page\n\n<image-unit><image-media><img src="data:image/png;base64,{second}"></image-media></image-unit>\n\ntail'
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), old, [(1, len(inc.source_lines(old)))])
            decision = inc.decide_update(run, old, new, kind="docx")
        self.assertNotEqual(decision.reason, "unchanged")
        self.assertNotEqual(decision.reason, "image-representation")

    def test_line_hunks_matches_difflib_on_small_inputs(self) -> None:
        cases = [
            (["a", "b", "c"], ["a", "x", "b", "c"]),
            (["a", "b", "c"], ["a", "c"]),
            (["a"], ["b"]),
            ([], ["a"]),
            (["a"], []),
            (["a", "b"], ["a", "b", "c"]),
            (["a", "b"], ["z", "a", "b"]),
            (["x" * 5000, "b"], ["y" * 5000, "b"]),
        ]
        for old, new in cases:
            expected = [
                (i + 1, j - i, k + 1, m - k)
                for tag, i, j, k, m in SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
                if tag != "equal"
            ]
            self.assertEqual(inc.line_hunks(old, new), expected)

    def test_line_hunks_keeps_edits_in_repeated_regions_local(self) -> None:
        block = [f"shared paragraph {i % 37}" if i % 3 else "" for i in range(600)]
        old = ["# A"] + [f"a {i}" for i in range(200)] + block + ["# C"] + [f"c {i}" for i in range(300)] + block + ["# D"] + [f"d {i}" for i in range(100)]
        for seed in range(5):
            new = list(old)
            second = len(old) - 101 - 600
            random.seed(seed)
            edited = random.sample([second + i for i in range(600) if old[second + i]], 30)
            for i in edited:
                new[i] += " (edited)"
            hunks = inc.line_hunks(old, new)
            self.assertEqual(sum(h[1] for h in hunks), 30)
            self.assertTrue(all(h[1] == h[3] and h[0] == h[2] for h in hunks), hunks[:5])

    def test_boundary_map_rules(self) -> None:
        bm = inc.BoundaryMap([(11, 0, 11, 3)], 20, 23)
        self.assertEqual((bm.map_range(1, 10), bm.map_range(11, 20)), ([1, 13], [14, 23]))
        bm = inc.BoundaryMap([(1, 0, 1, 2)], 20, 22)
        self.assertEqual((bm.map_range(1, 10), bm.map_range(11, 20)), ([1, 12], [13, 22]))
        bm = inc.BoundaryMap([(21, 0, 21, 2)], 20, 22)
        self.assertEqual(bm.map_range(11, 20), [11, 22])
        self.assertEqual(inc.split_at_cuts([(9, 4, 9, 5)], [10]), [(9, 2, 9, 0), (11, 2, 9, 5)])
        bm = inc.BoundaryMap(inc.split_at_cuts([(9, 4, 9, 5)], [10]), 20, 21)
        self.assertEqual((bm.map_range(1, 10), bm.map_range(11, 20)), ([1, 8], [9, 21]))

    def test_unchanged_text_is_tier_0(self) -> None:
        source = "\n".join(f"line {i}" for i in range(8))
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 4), (5, 8)])
            decision = inc.decide_update(run, source, source, kind="md")
            self.assertEqual(decision.tier, 0)
            inc.apply_update(run, decision, source)
            self.assertTrue(resumable(run, source))

    def test_one_line_edit_is_tier_1_and_plan_stays_resumable(self) -> None:
        source = "\n".join(f"line {i}" if i % 3 else "" for i in range(120))
        new = source.replace("line 55", "line 55 updated")
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 40), (41, 80), (81, 120)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.tier, 1)
            self.assertEqual(decision.patch, {"002-page.md": {0}})
            self.assertFalse(decision.regenerate)
            inc.apply_update(run, decision, new)
            self.assertTrue(resumable(run, new))

    def test_edit_plus_append_at_end_keeps_full_coverage(self) -> None:
        source = "\n".join(f"line {i}" for i in range(40))
        new = source.replace("line 4", "line 4 updated", 1) + "\nappended one\nappended two"
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 20), (21, 40)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.tier, 1)
            self.assertEqual(set(decision.patch), {"001-page.md", "002-page.md"})
            inc.apply_update(run, decision, new)
            plan = read_json(run / "state" / "plan.json")
            self.assertEqual(plan["pages"][-1]["owner_ranges"][-1][1], len(inc.source_lines(new)))
            self.assertTrue(resumable(run, new))

    def test_rewriting_most_of_one_page_regenerates_only_that_page(self) -> None:
        old_lines = [f"line {i}" if i % 3 else "" for i in range(120)]
        new_lines = list(old_lines)
        for i in range(40, 80):
            if new_lines[i].strip():
                new_lines[i] = f"replacement {i}"
        source, new = "\n".join(old_lines), "\n".join(new_lines)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 40), (41, 80), (81, 120)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.tier, 2)
            self.assertEqual(decision.regenerate, {"002-page.md"})
            inc.apply_update(run, decision, new)
            self.assertFalse((run / "wiki" / "002-page.md").exists())
            self.assertFalse((run / "state" / "pages" / "002.json").exists())
            self.assertTrue((run / "wiki" / "001-page.md").exists())
            self.assertTrue((run / "wiki" / "003-page.md").exists())
            self.assertTrue(resumable(run, new))

    def test_emptying_a_page_is_tier_3(self) -> None:
        lines = [f"line {i}" for i in range(120)]
        source = "\n".join(lines)
        new = "\n".join(lines[:40] + lines[80:])
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 40), (41, 80), (81, 120)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.tier, 3)
            self.assertIn(decision.reason, {"page-emptied", "unmappable"})

    def test_page_growth_is_tier_3(self) -> None:
        source = "\n".join(f"line {i}" for i in range(120))
        insertion = "\n".join(f"added {i}" for i in range(600))
        new = source.replace("line 60", "line 60\n" + insertion)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 40), (41, 80), (81, 120)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual((decision.tier, decision.reason), (3, "page-too-large"))

    def test_most_pages_regenerated_is_tier_3(self) -> None:
        lines = [f"line {i}" for i in range(80)]
        new_lines = list(lines)
        for page in range(3):
            for index in range(page * 20, page * 20 + 16):
                new_lines[index] = f"replacement {index}"
        source, new = "\n".join(lines), "\n".join(new_lines)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 20), (21, 40), (41, 60), (61, 80)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual((decision.tier, decision.reason), (3, "most-pages-changed"))

    def test_reference_only_page_gets_a_patch_and_stale_research(self) -> None:
        source = "\n".join(f"line {i}" for i in range(80))
        new = source.replace("line 44", "line 44 updated")
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 40), (41, 80)], references={1: [[45, 46]]})
            research = run / "work" / "research-001"
            research.mkdir(parents=True)
            (research / "references.json").write_text(json.dumps({"useful_facts": [{
                "source_start": 45, "source_end": 46, "target_line": 1,
            }]}), encoding="utf-8")
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertIn("001-page.md", decision.patch)
            self.assertNotIn("001-page.md", decision.owned)
            inc.apply_update(run, decision, new)
            self.assertFalse(research.exists())

    def test_owner_edit_reuses_and_remaps_reference_research(self) -> None:
        source = "one\ntwo\nthree\nfour\nfive\nsix"
        new = "one\ninserted\ntwo\nthree\nfour\nfive\nsix"
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 3), (4, 6)], references={1: [[4, 5]]})
            cache = run / "work" / "research-001" / "references.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"useful_facts": [{
                "source_start": 4, "source_end": 5, "target_line": 1,
            }]}), encoding="utf-8")
            section = run / "work" / "page-001" / "section-01-attempt-01.md"
            section.parent.mkdir(parents=True)
            section.write_text("cached", encoding="utf-8")
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.tier, 2)  # 1 changed line of 3 is above the 20% churn threshold.
            inc.apply_update(run, decision, new)
            self.assertTrue(section.exists())
            fact = read_json(cache)["useful_facts"][0]
            self.assertEqual((fact["source_start"], fact["source_end"]), (5, 6))

    def test_heading_rename_retitles_without_rebuild(self) -> None:
        from types import SimpleNamespace

        planner = SimpleNamespace(structure_target_lines=250, structure_min_lines=40, page_target_lines=100, pdf_use_headings=False)
        source = "# Chapter 1\n## A\n" + "\n".join(f"first {i}" for i in range(130)) + "\n# Chapter 2\n" + "\n".join(f"second {i}" for i in range(130))
        shape = inc._structural_shape(inc.source_lines(source), kind="docx", planner=planner)
        self.assertIsNotNone(shape)
        ranges = [(item["range"][0], item["range"][1]) for item in shape]
        titles = {i: item["title"] for i, item in enumerate(shape, 1)}
        paths = {i: item["path"] for i, item in enumerate(shape, 1)}
        new = source.replace("# Chapter 2", "# Chapter 2 renamed")
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, ranges, titles=titles, paths=paths)
            pages = read_json(run / "state" / "plan.json")["pages"]
            second = next(page for page in pages if "Chapter 2" in page["title"])
            first = pages[0]
            second_title = second["title"]
            (run / "wiki" / first["filename"]).write_text(
                f"# {first['title']}\n\n[{second_title}]({second['filename']})\n", encoding="utf-8")
            decision = inc.decide_update(run, source, new, kind="docx")
            self.assertEqual(decision.tier, 1)
            self.assertIn(second["filename"], decision.retitle)
            inc.apply_update(run, decision, new)
            renamed = str(decision.retitle[second["filename"]]["title"])
            self.assertTrue((run / "wiki" / second["filename"]).read_text(encoding="utf-8").startswith(f"# {renamed}\n"))
            self.assertIn(f"[{renamed}]({second['filename']})", (run / "wiki" / first["filename"]).read_text(encoding="utf-8"))
            self.assertEqual(read_json(run / "state" / "plan.json")["pages"][second["number"] - 1]["title"], renamed)

    def test_new_chapter_is_structure_changed(self) -> None:
        from types import SimpleNamespace

        planner = SimpleNamespace(structure_target_lines=250, structure_min_lines=40, page_target_lines=100, pdf_use_headings=False)
        source = "# Chapter 1\n" + "\n".join(f"first {i}" for i in range(130)) + "\n# Chapter 2\n" + "\n".join(f"second {i}" for i in range(130))
        shape = inc._structural_shape(inc.source_lines(source), kind="docx", planner=planner)
        ranges = [(item["range"][0], item["range"][1]) for item in shape]
        titles = {i: item["title"] for i, item in enumerate(shape, 1)}
        paths = {i: item["path"] for i, item in enumerate(shape, 1)}
        new = source.replace("# Chapter 2", "# Chapter 1B\n" + "\n".join(f"middle {i}" for i in range(80)) + "\n# Chapter 2")
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, ranges, titles=titles, paths=paths)
            decision = inc.decide_update(run, source, new, kind="docx")
            self.assertEqual((decision.tier, decision.reason), (3, "structure-changed"))

    def test_llm_plan_never_uses_the_structure_check(self) -> None:
        source = "intro\n" + "\n".join(f"body {i}" for i in range(100))
        new = source.replace("intro", "intro\n# Heading\nnew text")
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, len(inc.source_lines(source)))], titles={1: "Section one"})
            decision = inc.decide_update(run, source, new, kind="docx")
            self.assertEqual(decision.tier, 1)
            self.assertNotEqual(decision.reason, "structure-changed")

    def test_crlf_and_form_feed_do_not_shift_pages(self) -> None:
        source = "first\x0c inside\nsecond\nthird\u2028 inside\n" + "\n".join(f"tail {i}" for i in range(8))
        new = source.replace("tail 7", "tail 7 updated")
        self.assertEqual(len(inc.source_lines(source)), 11)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), source, [(1, 2), (3, 11)])
            decision = inc.decide_update(run, source, new, kind="md")
            self.assertEqual(decision.patch, {"002-page.md": {0}})


from types import SimpleNamespace
from unittest.mock import patch

from graph.workspace import writer
from graph.workspace.project import Project
from graph.wiki.wire import IncrementalPageEditResult


def make_project(tmp: str, pages_text: list[tuple[int, int, str]], old_source: str, rel: str = "test_md.md"):
    root = Path(tmp)
    project = Project(root / "project", root / "mount").ensure()
    state = project.state_dir(rel)
    (state / "source").mkdir(parents=True)
    (state / "source" / "original.md").write_text(old_source, encoding="utf-8")
    (state / "state" / "pages").mkdir(parents=True)
    (state / "wiki").mkdir()
    pages = []
    for number, (first, last, text) in enumerate(pages_text, 1):
        name = f"{number:03d}.md"
        pages.append({"number": number, "filename": name, "title": f"P{number}", "chapter": "", "path": [],
                      "owner_ranges": [[first, last]], "reference_ranges": []})
        (state / "wiki" / name).write_text(text, encoding="utf-8")
        (state / "state" / "pages" / f"{number:03d}.json").write_text(
            json.dumps({"filename": name, "content_sha256": sha256_text(text)}), encoding="utf-8")
    (state / "state" / "plan.json").write_text(json.dumps({
        "source_line_count": len(inc.source_lines(old_source)), "prompt_version": "v", "pages": pages,
    }), encoding="utf-8")
    return project, rel, state


SETTINGS = SimpleNamespace(ingest_mode="wiki", wiki_linker_enabled=True, wiki_linker_mode="legacy", wiki_write_attempts=2)
BODY = [f"line {i}" for i in range(100)]
OLD_SOURCE = "\n".join(["# Title", ""] + BODY + ["", "本機能が主に動作する計算機・装置を示す。", "", "## End", "", "end"]) + "\n"
COUNT = len(OLD_SOURCE.splitlines())


class PatchModel:
    """Patch calls get the fixed patches; judge calls get ``verdicts`` in order (then approval)."""

    def __init__(self, patches=None, unchanged=None, fail=False, verdicts=()):
        self.calls = 0
        self.patch_prompts, self.judge_prompts = [], []
        self.patches, self.unchanged, self.fail = patches or [], unchanged or [], fail
        self.verdicts = list(verdicts)

    async def structured(self, schema, messages, **_kwargs):
        prompt = "\n".join(str(message.content) for message in messages)
        if schema is not IncrementalPageEditResult:
            self.judge_prompts.append(prompt)
            verdict = self.verdicts.pop(0) if self.verdicts else {}
            if isinstance(verdict, Exception):
                raise verdict
            return schema.model_validate(verdict)
        self.calls += 1
        self.patch_prompts.append(prompt)
        if self.fail:
            return schema.model_validate({"patches": [{"edit_ids": [99], "before": "x", "after": "y"}]})
        return schema.model_validate({"patches": self.patches, "unchanged_edit_ids": self.unchanged})


class WriterTierTest(unittest.TestCase):
    def _write_raw(self, project: Project, rel: str, text: str) -> None:
        path = project.raw_file(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _regen_page(self, **kwargs):
        from graph.wiki.export import export_ingest_layout

        state = Path(kwargs["state_dir"])
        (state / "wiki" / "001.md").write_text("# P1\n\nregenerated\n", encoding="utf-8")
        return SimpleNamespace(out_dir=export_ingest_layout(
            state, kwargs["out_dir"], document_name=kwargs["document_name"]
        ))

    def test_patch_then_unchanged_rerun(self) -> None:
        old_page = "# P1\n\nline 5\n\n本機能が主に動作する計算機および装置の構成について述べる。\n"
        patches = [{
            "edit_ids": [1],
            "before": "本機能が主に動作する計算機および装置の構成について述べる。",
            "after": "",
        }]
        model = PatchModel(patches=patches)
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, _state = make_project(tmp, [(1, COUNT, old_page)], OLD_SOURCE)
            new = OLD_SOURCE.replace("本機能が主に動作する計算機・装置を示す。\n", "")
            self._write_raw(project, rel, new)
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual(result.tier, 1)
            self.assertEqual(result.changed_pages, ["test.md/001.md"])
            self.assertNotIn("計算機および装置の構成", (project.wiki_dir(rel) / "001.md").read_text(encoding="utf-8"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                unchanged = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((unchanged.tier, unchanged.changed_pages), (0, []))
            self.assertEqual(model.calls, 1)

    def test_model_may_answer_unchanged(self) -> None:
        model = PatchModel(unchanged=[1])
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, _state = make_project(tmp, [(1, COUNT, "# P1\n\n" + "\n".join(BODY))], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 **bold**\n"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.changed_pages), (1, []))
            self.assertEqual(len(model.judge_prompts), 1)  # "nothing to change" is judged too

    def test_judge_rejection_is_fed_back_to_the_next_patch(self) -> None:
        model = PatchModel(
            patches=[{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}],
            verdicts=[{"coverage_score": 70, "missing_important_information": [
                {"description": "変更後の値", "source_start": 8, "source_end": 8}]}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, COUNT, "# P1\n\nline 5\n")], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.changed_pages), (1, ["test.md/001.md"]))
            self.assertEqual(model.calls, 2)
            self.assertIn("原文 8-8行: 変更後の値", model.patch_prompts[1])
            self.assertIn("8: line 5 changed", model.judge_prompts[0])
            self.assertIn("line 5 changed", model.judge_prompts[0].split("--- Wiki候補 ---")[1])
            self.assertTrue((state / "work" / "patch" / "page-001" / "judge-02.json").exists())

    def test_patch_the_judge_never_approves_is_regenerated(self) -> None:
        page_one, page_two = "# P1\n\n" + "\n".join(BODY[:50]), "# P2\n\n" + "\n".join(BODY[50:])
        model = PatchModel(
            patches=[{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}],
            verdicts=[{"coverage_score": 90, "defects": ["原文にない値を追加した"]}] * 5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, 50, page_one), (51, COUNT, page_two)], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output", side_effect=self._regen_page):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.reason), (2, "patch-escalated"))
            self.assertEqual(result.regenerated_pages, ["test.md/001.md"])
            self.assertEqual(model.calls, 5)
            self.assertIn("品質: 原文にない値を追加した", model.patch_prompts[-1])

    def test_unanswered_judge_rejudges_the_same_patch(self) -> None:
        down = RuntimeError("judge down")
        model = PatchModel(
            patches=[{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}],
            verdicts=[down, down],  # both calls of the first verdict fail, the next one approves
        )
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, _state = make_project(tmp, [(1, COUNT, "# P1\n\nline 5\n")], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.changed_pages), (1, ["test.md/001.md"]))
            self.assertEqual((model.calls, len(model.judge_prompts)), (1, 3))

    def test_quoting_page_is_judged_with_the_new_quoted_lines(self) -> None:
        class QuotingModel(PatchModel):
            async def structured(self, schema, messages, **kwargs):
                if schema is IncrementalPageEditResult and "所有しておらず" in messages[-1].content:
                    return schema.model_validate({"unchanged_edit_ids": [1]})
                return await super().structured(schema, messages, **kwargs)

        page_one, page_two = "# P1\n\n" + "\n".join(BODY[:50]), "# P2\n\n" + "\n".join(BODY[50:])
        model = QuotingModel(patches=[{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}])
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, 50, page_one), (51, COUNT, page_two)], OLD_SOURCE)
            plan = read_json(state / "state" / "plan.json")
            plan["pages"][1]["reference_ranges"] = [[8, 8]]
            (state / "state" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.changed_pages), (1, ["test.md/001.md"]))
            prompts = {prompt.split("ページ: ", 1)[1].split("\n", 1)[0]: prompt for prompt in model.judge_prompts}
            self.assertIn("--- 他ページから引用してよい原文（反映は任意） ---\n8: line 5 changed", prompts["P2"])
            self.assertNotIn("他ページから引用してよい原文", prompts["P1"])

    def test_fast_patch_is_judged_against_the_page_before_it(self) -> None:
        fast = SimpleNamespace(**{**vars(SETTINGS), "policy": "fast"})
        old_page = "# P1\n\nline 5\n\n---\n\n次のページ: [P2](002.md)\n"
        model = PatchModel(
            patches=[{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}],
            # The patch may not change the title, so a title-only finding does not block it.
            verdicts=[{"approved": False, "coverage_score": 95, "title_defects": ["一般的すぎる"]}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, COUNT, old_page)], OLD_SOURCE)
            (state / "run.json").write_text(json.dumps({"policy": "fast"}), encoding="utf-8")
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked")):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=fast, llm=model, embedder=None)
            self.assertEqual((result.tier, result.changed_pages), (1, ["test.md/001.md"]))
            self.assertEqual((model.calls, len(model.judge_prompts)), (1, 1))
            judged = model.judge_prompts[0]
            self.assertIn("8: line 5 changed", judged)
            self.assertIn("（退行比較専用。事実の根拠ではない） ---\n# P1\n\nline 5\n\n", judged)
            candidate = judged.split("--- Wiki候補本文（ナビゲーション除外済み） ---", 1)[1]
            self.assertIn("line 5 changed", candidate)
            self.assertNotIn("次のページ", candidate)

    def test_failed_patch_escalates_to_page_regeneration(self) -> None:
        page_one, page_two = "# P1\n\n" + "\n".join(BODY[:50]), "# P2\n\n" + "\n".join(BODY[50:])
        model = PatchModel(fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, 50, page_one), (51, COUNT, page_two)], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))

            def rebuild(**kwargs):
                self.assertTrue(kwargs["require_resume"])
                self.assertFalse((state / "wiki" / "001.md").exists())
                return self._regen_page(**kwargs)

            with patch.object(writer, "build_wiki_output", side_effect=rebuild):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.reason), (2, "patch-escalated"))
            self.assertEqual(result.regenerated_pages, ["test.md/001.md"])
            self.assertEqual(result.changed_pages, result.regenerated_pages)

    def test_edit_and_append_patch_both_pages(self) -> None:
        source = "\n".join(f"line {i}" for i in range(120))
        page_one = "# P1\n\n" + "\n".join(f"line {i}" for i in range(60))
        page_two = "# P2\n\n" + "\n".join(f"line {i}" for i in range(60, 120))

        class EditingModel:
            async def structured(self, schema, messages, **_kwargs):
                prompt = "\n".join(str(message.content) for message in messages)
                if "line 5 changed" in prompt:
                    patch_data = [{"edit_ids": [1], "before": "line 5\n", "after": "line 5 changed\n"}]
                else:
                    patch_data = [{"edit_ids": [2], "before": "line 119", "after": "line 119\nappended"}]
                return schema.model_validate({"patches": patch_data})

        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, 60, page_one), (61, 120, page_two)], source)
            new = source.replace("line 5\n", "line 5 changed\n") + "\nappended"
            self._write_raw(project, rel, new)
            result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=EditingModel(), embedder=None)
            self.assertEqual(result.tier, 1)
            self.assertEqual(set(result.changed_pages), {"test.md/001.md", "test.md/002.md"})
            plan = read_json(state / "state" / "plan.json")
            self.assertEqual(plan["pages"][-1]["owner_ranges"][-1][1], len(inc.source_lines(new)))

    def test_resume_unavailable_falls_back_to_full(self) -> None:
        page_one, page_two = "# P1\n\n" + "\n".join(BODY[:50]), "# P2\n\n" + "\n".join(BODY[50:])
        model = PatchModel(fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, _state = make_project(tmp, [(1, 50, page_one), (51, COUNT, page_two)], OLD_SOURCE)
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            calls = []

            def rebuild(**kwargs):
                calls.append(kwargs)
                if kwargs.get("require_resume"):
                    from graph.wiki.pipeline import ResumeUnavailable
                    raise ResumeUnavailable("test")
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# full\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=rebuild):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
            self.assertEqual((result.tier, result.reason, result.rebuild), (3, "resume-failed", "full"))
            self.assertEqual([call.get("resume") for call in calls], [True, False])

    def test_interrupted_initial_build_keeps_checkpoints_when_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(root / "project", root / "mount").ensure()
            rel = "test_docx.md"
            self._write_raw(project, rel, OLD_SOURCE)
            state = project.state_dir(rel)
            (state / "source").mkdir(parents=True)
            (state / "source" / "original.md").write_text(OLD_SOURCE, encoding="utf-8")
            checkpoint = state / "work" / "observations" / "checkpoints" / "window" / "result.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text('{"kept": true}\n', encoding="utf-8")
            calls = []

            def resume_build(**kwargs):
                calls.append(kwargs)
                self.assertTrue(kwargs["resume"])
                self.assertTrue(checkpoint.exists())
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# resumed\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=resume_build):
                result = writer.write_wiki_pages(
                    project, rel, mode="wiki", settings=SETTINGS,
                    llm=None, embedder=None, resume=True,
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual((result.tier, result.reason, result.rebuild), (3, "no-previous-state", "full"))
            self.assertTrue(checkpoint.exists())
            self.assertEqual((project.wiki_dir(rel) / "001.md").read_text(encoding="utf-8"), "# resumed\n")

    def test_unchanged_incomplete_run_resumes_stored_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(
                tmp, [(1, COUNT, "# P1\n\n" + "\n".join(BODY))], OLD_SOURCE,
            )
            self._write_raw(project, rel, OLD_SOURCE)
            plan = state / "state" / "plan.json"
            checkpoint = state / "work" / "planning" / "result.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text('{"kept": true}\n', encoding="utf-8")
            (state / "wiki" / "001.md").unlink()

            def resume_build(**kwargs):
                self.assertTrue(kwargs["resume"])
                self.assertTrue(plan.exists())
                self.assertTrue(checkpoint.exists())
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# resumed\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=resume_build):
                result = writer.write_wiki_pages(
                    project, rel, mode="wiki", settings=SETTINGS,
                    llm=None, embedder=None, resume=True,
                )

            self.assertEqual(
                (result.tier, result.reason, result.rebuild),
                (3, "interrupted-initial-build", "full"),
            )
            self.assertTrue(plan.exists())
            self.assertTrue(checkpoint.exists())

    def test_changed_source_with_incomplete_run_rebuilds_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(
                tmp, [(1, COUNT, "# P1\n\n" + "\n".join(BODY))], OLD_SOURCE,
            )
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5", "line 5 changed"))
            checkpoint = state / "work" / "planning" / "result.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text('{"stale": true}\n', encoding="utf-8")
            (state / "wiki" / "001.md").unlink()

            def fresh_build(**kwargs):
                self.assertFalse(kwargs["resume"])
                self.assertFalse(checkpoint.exists())
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# rebuilt\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=fresh_build):
                result = writer.write_wiki_pages(
                    project, rel, mode="wiki", settings=SETTINGS,
                    llm=None, embedder=None, resume=True,
                )

            self.assertEqual(
                (result.tier, result.reason, result.rebuild),
                (3, "incomplete-previous-state", "full"),
            )

    def test_initial_build_does_not_resume_checkpoints_for_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(root / "project", root / "mount").ensure()
            rel = "test_docx.md"
            self._write_raw(project, rel, OLD_SOURCE)
            state = project.state_dir(rel)
            (state / "source").mkdir(parents=True)
            (state / "source" / "original.md").write_text("different source\n", encoding="utf-8")
            checkpoint = state / "work" / "observations" / "checkpoints" / "stale.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text('{}\n', encoding="utf-8")

            def fresh_build(**kwargs):
                self.assertFalse(kwargs["resume"])
                self.assertFalse(checkpoint.exists())
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# fresh\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=fresh_build):
                writer.write_wiki_pages(
                    project, rel, mode="wiki", settings=SETTINGS,
                    llm=None, embedder=None, resume=True,
                )

    def test_legacy_human_state_blocks_the_document_instead_of_rebuilding_it_away(self) -> None:
        page_one, page_two = "# P1\n\n" + "\n".join(BODY[:50]), "# P2\n\n" + "\n".join(BODY[50:])
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, state = make_project(tmp, [(1, 50, page_one), (51, COUNT, page_two)], OLD_SOURCE)
            sidecar = state / "state" / "pages" / "001.json"
            metadata = read_json(sidecar)
            metadata["human_edited"] = True
            sidecar.write_text(json.dumps(metadata), encoding="utf-8")
            self._write_raw(project, rel, OLD_SOURCE.replace("line 5\n", "line 5 changed\n"))
            with patch.object(writer, "build_wiki_output") as build:
                with self.assertRaisesRegex(ValueError, "legacy human state"):
                    writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=PatchModel(fail=True), embedder=None)
            build.assert_not_called()
            self.assertTrue(state.exists())

    def test_tabular_is_always_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, rel, _state = make_project(tmp, [], "", rel="book_xlsx.md")
            self._write_raw(project, rel, "cell")
            calls = []

            def build(**kwargs):
                calls.append(kwargs)
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                (docs / "001.md").write_text("# Table\n", encoding="utf-8")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))

            with patch.object(writer, "build_wiki_output", side_effect=build):
                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=None, embedder=None)
            self.assertFalse(calls[0]["resume"])
            self.assertEqual(result.reason, "format-full-only")


class PublisherTierTest(unittest.TestCase):
    def test_legacy_suffixes_are_scanned_and_mapped(self) -> None:
        from graph.formats import kind_of
        from publisher.scanner import scan_mount

        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            for name in ("a.doc", "b.xls", "c.ppt"):
                (mount / name).write_bytes(b"sample")
            scan = scan_mount(mount)

        self.assertEqual(set(scan.files), {"a.doc", "b.xls"})
        self.assertEqual(kind_of("a_doc.md"), "docx")
        self.assertEqual(kind_of("t/b_xls.md"), "xlsx")
        self.assertEqual(kind_of("a_docx.md"), "docx")
        self.assertEqual(kind_of("notes.md"), "md")

    def test_parse_gate(self) -> None:
        from publisher.pipeline import _check_parse_size

        with self.assertRaisesRegex(RuntimeError, "sync --force"):
            _check_parse_size("a.docx", "x" * 5000, "x" * 1000)
        _check_parse_size("a.docx", "x" * 5000, "x" * 2000)
        _check_parse_size("a.docx", "x" * 1000, "")
        image = (
            '<image-unit>\n  <image-media><img src="data:image/png;base64,'
            + "A" * 50000
            + '" alt=""></image-media>\n  <image-description>d</image-description>\n</image-unit>'
        )
        _check_parse_size("a.docx", image + "x" * 3000, "x" * 3000)

    def test_classifier_ignores_heading_styles_and_uses_ratio(self) -> None:
        old = [f"line {i}" for i in range(100)]
        new = list(old)
        new[20] += " edited"
        with patch.object(queue, "_docx_text", side_effect=[old, new]):
            self.assertEqual(queue._classification(b"", b"", ".docx")["kind"], "small")
        new = [f"new {i}" for i in range(50)] + old[50:]
        with patch.object(queue, "_docx_text", side_effect=[old, new]):
            self.assertEqual(queue._classification(b"", b"", ".docx")["kind"], "large")
        self.assertEqual(queue._classification(b"", b"", ".pdf")["kind"], "unknown")

    def test_supersession_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "project", Path(tmp) / "mount").ensure()
            job = queue.Job("a.pdf", "a_pdf.md", "update", "slow", 1, "t")

            def reset(classification="unknown"):
                with queue._connect(project) as conn:
                    conn.execute("DELETE FROM jobs")
                    conn.execute(
                        "INSERT INTO jobs(rel,raw_rel,operation,lane,version,status,token,available_at,created_at,updated_at,classification)"
                        " VALUES('a.pdf','a_pdf.md','update','slow',1,'running','t',0,0,0,?)",
                        (classification,),
                    )

            for kind, expected in (("unknown", "continue"), ("small", "continue"), ("large", "cancel"), ("forced", "cancel")):
                reset()
                with queue._connect(project) as conn:
                    queue._enqueue(conn, "a.pdf", "a_pdf.md", "update", time.time(), 0, classification=kind)
                self.assertEqual(queue.supersession(project, [job]), expected)
            reset()
            with queue._connect(project) as conn:
                queue._enqueue(conn, "a.pdf", "a_pdf.md", "delete", time.time(), 0)
            self.assertEqual(queue.supersession(project, [job]), "cancel")

    def test_forced_scan_marks_jobs_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("one", encoding="utf-8")
            settings = SimpleNamespace(data_root=str(root / "data"), target_name="t", mount_path=str(mount))
            project = Project(Path(settings.data_root) / "t", mount).ensure()
            queue.scan(settings, settle_seconds=0)
            queue.scan(settings, settle_seconds=0, force=True)
            self.assertEqual(queue.status(project)[0]["classification"], "forced")
