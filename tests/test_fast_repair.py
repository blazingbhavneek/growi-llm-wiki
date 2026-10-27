from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.storage import read_json, sha256_text, write_json_atomic
from publisher.ledger import Ledger, save_ledger
from graph.fast.model import (
    FAST_WRITER_MAX_OUTPUT_TOKENS,
    FastModelPort,
    FastRoleGate,
    JUDGE_MAX_OUTPUT_TOKENS,
    model_pair,
)
from graph.fast.passes import bounded_map
from graph.fast.policy import FAST
from graph.fast.repair import (
    FastRepair,
    JUDGE_CALL_ATTEMPTS,
    MAX_REWRITE_ATTEMPTS,
    RepairJudgeResult,
    _assemble_page,
    _feedback,
    _judge_rank,
    _new_repair_session,
    _split_page,
    _write_session_checkpoint,
)
from graph.fast.wiki import append_low_coverage_source, recent_writer_context
from graph.fast.writer import _generate_page, _prepare_session, _value_errors, rewrite_pending_pages
from graph.workspace.project import Project
from graph.wiki import pipeline as wiki_pipeline
from graph.wiki.config import WikiConfig
from graph.wiki.images import extract_image_units
from runner.cli import build_parser


IMAGE = (
    '<image-unit><image-media><img src="data:image/png;base64,aGVsbG8=" />'
    '></image-media><image-description>图片中的坏说明</image-description></image-unit>'
)


def settings(root: Path, *, concurrency: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        policy="fast",
        data_root=str(root.parent),
        target_name=root.name,
        mount_path=str(root / "mount"),
        chat_base_url="http://example.invalid/v1",
        chat_api_key="test",
        chat_model="test-model",
        concurrency=concurrency,
        wiki_rewrite_concurrency=concurrency,
        wiki_output_language="Japanese (日本語)",
        wiki_section_target_lines=80,
        wiki_write_attempts=3,
        wiki_planner_concurrency=concurrency,
        wiki_request_timeout=30,
        skip_excel_and_small=False,
        structure_target_lines=250,
        structure_min_lines=40,
        pdf_use_headings=False,
        growi_url="",
    )


def make_document(
    root: Path,
    pages: list[tuple[str, str, tuple[int, int]]],
    source_lines: list[str],
) -> tuple[Project, str, Path]:
    project = Project(root, root / "mount").ensure()
    project.mount.mkdir(parents=True, exist_ok=True)
    raw_rel = "team/document_pdf.md"
    raw = project.raw_file(raw_rel)
    raw.parent.mkdir(parents=True, exist_ok=True)
    source = "\n".join(source_lines) + "\n"
    raw.write_text(source, encoding="utf-8")
    state_root = project.state_dir(raw_rel)
    (state_root / "source").mkdir(parents=True, exist_ok=True)
    (state_root / "state" / "pages").mkdir(parents=True, exist_ok=True)
    (state_root / "wiki").mkdir(parents=True, exist_ok=True)
    (state_root / "source" / "original.md").write_text(source, encoding="utf-8")
    source_hash = sha256_text(source)
    plan_pages = []
    manifest_pages = []
    for number, (title, body, owned) in enumerate(pages, start=1):
        filename = f"{number:03d}-page-{number}.md"
        path = state_root / "wiki" / filename
        path.write_text(body, encoding="utf-8")
        page = {
            "number": number,
            "title": title,
            "chapter": "test",
            "summary": "",
            "path": [title],
            "filename": filename,
            "owner_ranges": [list(owned)],
            "reference_ranges": [],
        }
        plan_pages.append(page)
        manifest_pages.append({**page, "status": "rewritten"})
        write_json_atomic(state_root / "state" / "pages" / f"{number:03d}.json", {
            "number": number,
            "title": title,
            "filename": filename,
            "status": "rewritten",
            "rewrite_version": "existing-writer",
            "source_ranges": [list(owned)],
            "reference_ranges": [],
            "provenance": {"source_document": {"sha256": source_hash}},
            "content_sha256": sha256_text(body),
        })
    plan = {
        "source": str(raw),
        "source_snapshot": str(state_root / "source" / "original.md"),
        "source_sha256": source_hash,
        "source_line_count": len(source_lines),
        "prompt_version": "test",
        "pages": plan_pages,
    }
    write_json_atomic(state_root / "state" / "plan.json", plan)
    write_json_atomic(state_root / "state" / "manifest.json", {
        "source": str(raw), "source_sha256": source_hash, "pages": manifest_pages,
    })
    write_json_atomic(state_root / "state" / "run.json", {"published": True})
    return project, raw_rel, state_root


class RepairingModel:
    def __init__(self) -> None:
        self.writer_messages: list[str] = []
        self.judge_messages: list[str] = []
        self.writer_calls = 0

    async def structured(self, _schema, messages, **_kwargs):
        body = messages[-1].content
        self.judge_messages.append(body)
        title = body.split("現在の候補タイトル: ", 1)[1].splitlines()[0]
        if title == "项目内部标题":
            return RepairJudgeResult(
                approved=False,
                coverage_score=80,
                title_defects=["中国語の内部タイトルである"],
                body_defects=["同じ文を繰り返している"],
            )
        return RepairJudgeResult(approved=True, coverage_score=100)

    async def text(self, messages, **_kwargs):
        self.writer_calls += 1
        self.writer_messages.append(messages[-1].content)
        return "# 正しい機能\n\n## 説明\n\n正常な説明。\n\n[[WIKI_IMAGE_001]]\n"


class FastRepairTest(unittest.TestCase):
    def test_fast_generation_value_gate_keeps_units_and_table_rows(self):
        source = "待機は１０ 秒。\n| 項目 | 値 |\n| --- | --- |\n| A | 1 |\n"
        self.assertEqual(_value_errors(source, "待機は10秒。\n<tr><td>A</td><td>1</td></tr>\n"), [
            "原文より表の行数が少ない。全ての表行とセルの意味を残すこと。"
        ])
        complete = (
            "待機は10秒。\n<table><thead><tr><th>項目</th><th>値</th></tr></thead>"
            "<tbody><tr><td>A</td><td>1</td></tr></tbody></table>\n"
        )
        self.assertEqual(_value_errors(source, complete), [])
        errors = _value_errors(source, "待機時間なし。\n")
        self.assertTrue(any("10秒" in error for error in errors))
        self.assertTrue(any("表の行数" in error for error in errors))

    def test_reassembled_footer_renders_as_rule_after_paragraph_or_table(self):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            self.skipTest("optional markdown-it renderer is not installed")

        footer = "---\n\n次のページ: [次](002.md)\n"
        for body in ("最後の説明。", "<table><tr><td>42</td></tr></table>"):
            with self.subTest(body=body):
                original = f"# ページ\n\n{body}\n{footer}"
                shell = _split_page(original)
                assembled = _assemble_page(shell.title, shell.body, shell.footer)
                rendered = MarkdownIt().render(assembled)
                self.assertIn(body + "\n\n---\n", assembled)
                self.assertEqual(rendered.count("<hr />"), 1)
                self.assertNotIn("<h2>", rendered)
                self.assertNotIn("<p>---</p>", rendered)
                self.assertEqual(_split_page(assembled), shell)

    def test_initially_approved_page_still_gets_footer_spacing_fixed(self):
        class CleanModel:
            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, *_args, **_kwargs):
                raise AssertionError("footer spacing must not require the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# ページ\n\n説明。\n---\n\n次のページ: [次](002.md)\n"
            project, raw_rel, state_root = make_document(
                root, [("ページ", original, (1, 1))], ["説明。"]
            )
            result = FastRepair(
                settings(root), project=project, model=CleanModel()
            ).repair_document(raw_rel, promote=False)
            repaired = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertEqual(result["clean"], 1)
            self.assertEqual(repaired, original.replace("説明。\n---", "説明。\n\n---"))
            state = read_json(state_root / "state" / "pages" / "001.json")
            self.assertEqual(state["content_sha256"], sha256_text(repaired))

    def test_shared_rubric_repairs_intent_and_readability_without_structural_gate(self):
        test = self

        class IntentModel:
            async def structured(self, _schema, messages, **_kwargs):
                rules = messages[0].content
                test.assertIn("質問・確認依頼・提案・仮定・未確定事項", rules)
                test.assertIn("Markdownパイプ表は本文品質の欠陥", rules)
                test.assertIn("導入の欠落", rules)
                test.assertIn("体裁だけの欠陥では下げない", rules)
                candidate = messages[-1].content.split("--- Wiki候補本文", 1)[1]
                if "確認事項：担当部署に、この理解が正しいか確認する。" in candidate:
                    return RepairJudgeResult(approved=True, coverage_score=100)
                return RepairJudgeResult(
                    approved=False,
                    coverage_score=90,
                    missing_important_information=["担当部署への確認依頼と未確定状態"],
                    body_defects=["導入の欠落、Markdownパイプ表、孤立した注記"],
                )

            async def text(self, messages, **_kwargs):
                rules = messages[0].content
                test.assertIn("H1の直後に", rules)
                test.assertIn("確定した仕様や決定事項へ変えない", rules)
                test.assertIn("表注記へ自然に統合", rules)
                test.assertIn("\\bigcirc、\\times", rules)
                test.assertIn("担当部署への確認依頼と未確定状態", messages[-1].content)
                return (
                    "# 設定値の確認\n\n設定値と担当部署への確認事項を示す。\n\n"
                    "<table><tr><th>項目</th><th>値</th></tr>"
                    "<tr><td>設定値</td><td>42</td></tr></table>\n\n"
                    "注記：設定値の単位は秒。\n\n"
                    "確認事項：担当部署に、この理解が正しいか確認する。\n"
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [("設定値の確認", "# 設定値の確認\n\n## 設定\n\n"
                  "| 設定値 | 42 |\n| --- | --- |\n\nこの理解が正しい。\n秒\n", (1, 3))],
                ["設定値は42。", "単位は秒。", "担当部署様、この理解で相違ございませんでしょうか？"],
            )
            result = FastRepair(
                settings(root), project=project, model=IntentModel()
            ).repair_document(raw_rel, promote=False)
            self.assertEqual(result["repaired"], 1)
            repaired = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertIn("確認事項：担当部署に、この理解が正しいか確認する。", repaired)
            self.assertIn("注記：設定値の単位は秒。", repaired)
            self.assertIn("<table>", repaired)

    def test_empty_source_is_reported_before_repair_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root, [("ページ", "# ページ\n", (1, 1))], []
            )
            for path in (project.raw_file(raw_rel), state_root / "source" / "original.md"):
                path.write_text("", encoding="utf-8")
            repair = FastRepair(settings(root), project=project, model=object())
            with self.assertRaisesRegex(RuntimeError, "source is empty; check conversion/extraction"):
                repair.repair_document(raw_rel, promote=False)
            self.assertEqual(
                (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8"),
                "# ページ\n",
            )

    def test_empty_page_source_is_reported_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = wiki_pipeline.SeedPage(1, "ページ", "章", "", [(1, 1)], "001.md", "page-1")
            with self.assertRaisesRegex(wiki_pipeline.PipelineError, "source is empty"):
                _prepare_session(
                    page, pages=[page], lines=[" "], units=[], config=WikiConfig(policy="fast"),
                    work_root=root / "work", wiki_root=root / "wiki", state_root=root / "state",
                    source_path=root / "source.md",
                )

    def test_fast_writer_keeps_only_images_opaque(self):
        lines = [
            "| 項目 | 値 |",
            "| --- | --- |",
            "| A | 1 |",
            "```text",
            "fenced source",
            "```",
            IMAGE,
        ]

        units = FAST.writer_units(lines, extract_image_units(lines))

        self.assertEqual(len(units), 1)
        self.assertIn("image-unit", units[0].raw)

    def test_low_coverage_keeps_raw_source_in_hidden_editor_fallback(self):
        raw = "# 1. 原文見出し\n\n重要な値は42。"
        rendered = append_low_coverage_source("読みやすい本文。", raw, 79)

        self.assertIn("情報保持スコアが80未満", rendered)
        self.assertIn('<span hidden style="display:none"', rendered)
        self.assertIn("重要な値は42。", rendered)
        self.assertIn("# 1. 原文見出し", FAST.section(rendered))
        masked, restore = FAST.mask_for_linking(rendered)
        self.assertNotIn("重要な値は42。", masked)
        self.assertEqual(restore(masked), rendered)
        self.assertEqual(
            append_low_coverage_source("読みやすい本文。", "重要な値は42。", 80),
            "読みやすい本文。",
        )

    def test_recent_writer_context_has_three_summaries_and_last_50_lines(self):
        pages = [
            SimpleNamespace(number=number, title=f"ページ{number}", summary=f"要約{number}")
            for number in range(1, 6)
        ]

        context = recent_writer_context(
            page=pages[-1],
            pages=pages,
            previous_source="\n".join(f"source-line-{number}" for number in range(1, 61)),
        )

        self.assertNotIn("ページ1 — 要約1", context)
        self.assertIn("ページ2 — 要約2", context)
        self.assertIn("ページ4 — 要約4", context)
        self.assertNotIn("source-line-10\n", context)
        self.assertIn("source-line-11\n", context)
        self.assertIn("source-line-60", context)
        self.assertIn("直前50行・最大12,000文字", context)

        long_context = recent_writer_context(
            page=pages[-1],
            pages=pages,
            previous_source="\n".join(
                f"long-line-{number}-" + ("x" * 300) for number in range(1, 61)
            ),
        )
        preceding = long_context.split(
            "現在のページにない事実をコピーしない。\n", 1
        )[1].rstrip("\n")
        self.assertLessEqual(len(preceding), 12_000)
        self.assertIn("long-line-60-", preceding)

    def test_fast_generation_uses_page_repair_writer_and_judge(self):
        class GenerationModel:
            def __init__(self):
                self.writer_messages = []
                self.judge_messages = []

            async def text(self, messages, **_kwargs):
                self.writer_messages.append(messages[-1].content)
                return (
                    "# 読みやすいページ\n\n整理された本文。\n\n---\n\n"
                    "前のページ: [モデル作成リンク](wrong.md)\n"
                )

            async def structured(self, schema, messages, **_kwargs):
                self.assert_schema = schema
                self.judge_messages.append(messages[-1].content)
                return RepairJudgeResult(
                    approved=False,
                    coverage_score=70,
                    missing_important_information=["重要な値42"],
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [f"直前の流れ{number}" for number in range(1, 51)] + [
                "現在ページの最初。",
                "重要な値は42。",
                "現在ページの最後。",
            ]
            previous = wiki_pipeline.SeedPage(
                1, "前ページ", "章", "前ページの要約", [(1, 50)],
                "001.md", "page-1",
            )
            page = wiki_pipeline.SeedPage(
                2, "仮タイトル", "章", "現在ページ", [(51, 53)],
                "002.md", "page-2",
            )
            config = WikiConfig(
                policy="fast", write_attempts=2, human_guidance={}
            )
            session = _prepare_session(
                page,
                pages=[previous, page],
                lines=lines,
                units=[],
                config=config,
                work_root=root / "work",
                wiki_root=root / "wiki",
                state_root=root / "state",
                source_path=root / "source.md",
            )
            model = GenerationModel()
            outcome = asyncio.run(_generate_page(
                session,
                lines=lines,
                model=model,
                config=config,
                stop_check=None,
                on_progress=None,
            ))

        self.assertIs(model.assert_schema, RepairJudgeResult)
        # write_attempts=2 cannot shrink the budget below the repair budget.
        self.assertEqual(len(model.writer_messages), MAX_REWRITE_ATTEMPTS)
        self.assertEqual(len(model.judge_messages), MAX_REWRITE_ATTEMPTS)
        self.assertIn("直前の流れ1", model.writer_messages[0])
        self.assertIn("直前の流れ50", model.writer_messages[0])
        self.assertIn("51: 現在ページの最初。", model.writer_messages[0])
        self.assertIn("53: 現在ページの最後。", model.writer_messages[0])
        self.assertNotIn("直前の流れ1", model.judge_messages[0])
        # Every draft was rejected, so the exact source is published instead of the best draft.
        self.assertTrue(outcome.verbatim)
        self.assertIsNone(outcome.verdict)
        self.assertIn("no candidate was approved", outcome.errors[-1])
        self.assertIn("重要な値42", outcome.errors[-1])
        self.assertNotIn("整理された本文。", outcome.body)
        self.assertNotIn("モデル作成リンク", outcome.body)
        self.assertIn("重要な値は42。", outcome.body)
        self.assertNotIn('data-llm-wiki-low-coverage="true"', outcome.body)

    def test_fast_generation_publishes_the_approved_draft_over_a_higher_scored_rejection(self):
        class RecoveringModel:
            drafts = 0

            async def text(self, _messages, **_kwargs):
                self.drafts += 1
                return f"# 仮タイトル\n\n草稿{self.drafts}。重要な値は42。\n"

            async def structured(self, _schema, messages, **_kwargs):
                if "草稿1" in messages[-1].content:
                    return RepairJudgeResult(
                        approved=False, coverage_score=95, body_defects=["文が重複している"]
                    )
                return RepairJudgeResult(approved=True, coverage_score=85)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ["重要な値は42。"]
            page = wiki_pipeline.SeedPage(1, "仮タイトル", "章", "", [(1, 1)], "001.md", "page-1")
            config = WikiConfig(policy="fast", human_guidance={})
            session = _prepare_session(
                page, pages=[page], lines=lines, units=[], config=config,
                work_root=root / "work", wiki_root=root / "wiki", state_root=root / "state",
                source_path=root / "source.md",
            )
            model = RecoveringModel()
            outcome = asyncio.run(_generate_page(
                session, lines=lines, model=model, config=config, stop_check=None, on_progress=None,
            ))

        self.assertEqual(model.drafts, 2)
        self.assertFalse(outcome.verbatim)
        self.assertIn("草稿2", outcome.body)
        self.assertEqual(outcome.verdict.coverage_score, 85)

    def test_fast_generation_does_not_retry_a_model_title_defect(self):
        class TitleDefectModel:
            def __init__(self):
                self.calls = 0

            async def text(self, _messages, **_kwargs):
                self.calls += 1
                return "# Model title\n\nWait 10秒.\n"

            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(
                    approved=False,
                    coverage_score=100,
                    title_defects=["change the title"],
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = wiki_pipeline.SeedPage(
                1, "Planned title", "", "", [(1, 1)], "001.md", "page-1"
            )
            config = WikiConfig(policy="fast", write_attempts=2)
            session = _prepare_session(
                page,
                pages=[page],
                lines=["Wait 10秒."],
                units=[],
                config=config,
                work_root=root / "work",
                wiki_root=root / "wiki",
                state_root=root / "state",
                source_path=root / "source.md",
            )
            model = TitleDefectModel()
            outcome = asyncio.run(_generate_page(
                session,
                lines=["Wait 10秒."],
                model=model,
                config=config,
                stop_check=None,
                on_progress=None,
            ))

        self.assertEqual(model.calls, 1)
        self.assertEqual(outcome.title, "Planned title")
        self.assertEqual(outcome.verdict.title_defects, [])
        self.assertTrue(outcome.verdict.approved)

    def test_fast_generation_replaces_model_navigation_with_one_current_footer(self):
        class NavigationModel:
            async def text(self, messages, **_kwargs):
                prompt = messages[-1].content
                title = "最終ページ一" if "ページ番号: 1" in prompt else "最終ページ二"
                return (
                    f"# {title}\n\n本文。\n\n---\n\n"
                    "次のページ: [モデルの誤リンク](wrong.md)\n"
                )

            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages = [
                wiki_pipeline.SeedPage(
                    1, "仮ページ一", "章", "", [(1, 1)], "001.md", "page-1"
                ),
                wiki_pipeline.SeedPage(
                    2, "仮ページ二", "章", "", [(2, 2)], "002.md", "page-2"
                ),
            ]
            results = []
            asyncio.run(rewrite_pending_pages(
                pending=pages,
                results=results,
                pages=pages,
                lines=["本文一。", "本文二。"],
                units=[],
                model=NavigationModel(),
                config=WikiConfig(
                    policy="fast", write_attempts=1, rewrite_concurrency=2
                ),
                work_root=root / "work",
                wiki_root=root / "wiki",
                state_root=root / "state",
                source_path=root / "source.md",
                source_snapshot_path=root / "snapshot.md",
                source_sha256="source-hash",
                source_line_count=2,
                rewrite_version="test-fast-v7",
                stop_check=None,
                on_progress=None,
                parents=None,
            ))

            first = (root / "wiki" / "001.md").read_text(encoding="utf-8")
            second = (root / "wiki" / "002.md").read_text(encoding="utf-8")

        self.assertNotIn("モデルの誤リンク", first + second)
        self.assertEqual(first.count("次のページ:"), 1)
        self.assertIn("本文。\n\n---\n", first)
        self.assertIn("次のページ: [仮ページ二](002.md)", first)
        self.assertEqual(second.count("前のページ:"), 1)
        self.assertIn("前のページ: [仮ページ一](001.md)", second)

    def test_fast_generation_keeps_the_planned_title_in_plan_and_index(self):
        from graph.wiki.schemas import CompiledSeedPlan
        from graph.wiki.wire import SeedRange

        class TitleModel:
            async def text(self, _messages, **_kwargs):
                return "# 最終タイトル\n\n重要な値は42。\n"

            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("重要な値は42。\n", encoding="utf-8")
            config = WikiConfig(
                policy="fast",
                run_dir=str(root / "run"),
                plan_cache_dir=str(root / "cache"),
                rewrite_concurrency=1,
                write_attempts=1,
            )
            source_text = source.read_text(encoding="utf-8")
            source_lines = wiki_pipeline.split_source_lines(
                wiki_pipeline.normalize_source(source_text)
            )
            seed = CompiledSeedPlan(pages=[SeedRange(
                title="仮タイトル",
                summary="要約",
                source_start=1,
                source_end=1,
                path=["仮タイトル"],
            )])
            wiki_pipeline._store_plan_cache(
                wiki_pipeline._plan_cache_path(source_text, config),
                source_text,
                source_lines,
                config,
                seed,
            )

            run_root = asyncio.run(wiki_pipeline.run_pipeline(
                source, config=config, model=TitleModel()
            ))
            plan_page = read_json(run_root / "state" / "plan.json")["pages"][0]
            index = (run_root / "wiki" / "index.md").read_text(encoding="utf-8")

        self.assertEqual(plan_page["title"], "仮タイトル")
        self.assertEqual(plan_page["path"], ["仮タイトル"])
        self.assertIn("[仮タイトル]", index)

    def test_cli_repair_is_fast_operation_with_limit(self):
        args = build_parser().parse_args(["sync", "--fast", "--repair", "--limit", "7", "a.pdf"])
        self.assertTrue(args.fast_repair)
        self.assertEqual(args.limit, 7)
        self.assertEqual(args.items, ["a.pdf"])
        resumed = build_parser().parse_args(
            ["sync", "--fast", "--repair", "--force", "--continue"]
        )
        self.assertTrue(resumed.force)
        self.assertTrue(resumed.continue_run)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--fast", "--repair", "--link"])

    def test_a_page_that_fails_the_pull_holds_back_only_its_own_document(self):
        from runner import cli

        repaired = []

        class Repair:
            concurrency = 1
            def __init__(self, *args, **kwargs):
                pass
            def targets(self, items=None):
                return ["a_md.md", "b_md.md"]
            def repair_document(self, raw_rel, **kwargs):
                repaired.append(raw_rel)
                return {"failures": [], "promoted": False, "examined": 1}

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mount").mkdir()
            settings = SimpleNamespace(data_root=tmp, target_name="p", mount_path=str(Path(tmp) / "mount"),
                                       growi_url="http://growi")
            args = build_parser().parse_args(["sync", "--fast", "--repair"])
            for failures, expected in ((["a.md/001.md: remote_moved"], ["b_md.md"]),
                                       (["pull: ConnectionError: down"], [])):
                repaired.clear()
                with patch.object(cli, "_settings", return_value=settings), \
                     patch("graph.fast.repair.FastRepair", Repair), \
                     patch("publisher.pipeline.pull_growi_once", return_value={"done": [], "failures": failures}):
                    self.assertEqual(cli._cmd_fast_repair(args), 1)  # the failure is still reported
                self.assertEqual(repaired, expected)

    def test_title_body_and_links_are_repaired_while_images_stay_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            page_one = (
                "# 项目内部标题\n\n同じ異常文章です。\n同じ異常文章です。\n同じ異常文章です。\n\n"
                + IMAGE
                + "\n\n---\n\n次のページ: [第二ページ](002-page-2.md)\n"
            )
            page_two = (
                "# 第二ページ\n\n[古いタイトル](001-page-1.md)を参照する。\n\n"
                "---\n\n前のページ: [古いタイトル](001-page-1.md)\n"
            )
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("项目内部标题", page_one, (1, 3)),
                    ("第二ページ", page_two, (4, 5)),
                ],
                ["# 正しい機能", "正常な説明。", IMAGE, "# 第二ページ", "参照説明。"],
            )
            model = RepairingModel()
            repair = FastRepair(settings(root), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 1)
            repaired = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertTrue(repaired.startswith("# 正しい機能\n"))
            self.assertIn(IMAGE, repaired)
            self.assertIn("次のページ: [第二ページ](002-page-2.md)", repaired)
            sibling = (state_root / "wiki" / "002-page-2.md").read_text(encoding="utf-8")
            self.assertIn("[正しい機能](001-page-1.md)", sibling)
            self.assertNotIn("aGVsbG8=", "\n".join(model.judge_messages + model.writer_messages))
            self.assertNotIn("图片中的坏说明", "\n".join(model.judge_messages + model.writer_messages))
            self.assertIn("[[SOURCE_IMAGE_", model.judge_messages[0])
            self.assertIn("[[WIKI_IMAGE_001]]", model.judge_messages[0])
            self.assertIn("タイトル: 中国語の内部タイトルである", model.writer_messages[0])
            state = read_json(state_root / "state" / "pages" / "001.json")
            self.assertEqual(state["rewrite_version"], "existing-writer")
            self.assertEqual(state["repair_status"], "repaired")
            repaired_plan = read_json(state_root / "state" / "plan.json")["pages"][0]
            self.assertEqual(repaired_plan["title"], "正しい機能")
            self.assertEqual(repaired_plan["path"], ["正しい機能"])

    def test_clean_page_is_not_reformatted(self):
        class CleanModel:
            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# ページ\n\n\n説明。\n\n"
            project, raw_rel, state_root = make_document(
                root, [("ページ", original, (1, 1))], ["説明。"]
            )
            result = FastRepair(
                settings(root), project=project, model=CleanModel()
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["clean"], 1)
            self.assertEqual(
                (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8"),
                original,
            )

    def test_continue_routes_unjudged_to_judge_and_rejected_to_writer(self):
        class ResumeModel:
            def __init__(self):
                self.roles = []

            async def structured(self, _schema, messages, **_kwargs):
                prompt = messages[-1].content
                page = 2 if "ページ番号: 2" in prompt else 1
                self.roles.append(("judge", page))
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                self.roles.append(("writer", 1))
                return "# ページ1\n\n正常な説明。\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["正常な説明。", "正常な説明。"],
            )
            setup = FastRepair(settings(root), project=project, model=ResumeModel())
            loaded_root, _plan, _manifest, pages, _titles = setup._load_document(
                raw_rel, force=False, limit=None
            )

            rejected = _new_repair_session(pages[0])
            verdict = RepairJudgeResult(
                approved=False,
                coverage_score=80,
                body_defects=["修復が必要"],
            )
            rejected.feedback = _feedback(verdict)
            rejected.feedback_history = list(rejected.feedback)
            rejected.best_feedback = list(rejected.feedback)
            rejected.best_verdict = verdict
            rejected.best_rank = _judge_rank(
                verdict, attempt=0
            )
            _write_session_checkpoint(
                loaded_root,
                setup.version,
                rejected,
                stage="writer",
                model_name=setup.model_name,
                model_provider=setup.model_provider,
            )
            _write_session_checkpoint(
                loaded_root,
                setup.version,
                _new_repair_session(pages[1]),
                stage="initial-judge",
                model_name=setup.model_name,
                model_provider=setup.model_provider,
            )

            checkpoints = [
                (state_root / "state" / "pages" / f"{number:03d}.json")
                .relative_to(project.root)
                .as_posix()
                for number in (1, 2)
            ]
            model = ResumeModel()
            result = FastRepair(
                settings(root),
                project=project,
                model=model,
                resume_page_states=checkpoints,
            ).repair_document(raw_rel, force=True, promote=False)

            self.assertEqual(model.roles[0], ("judge", 2))
            self.assertEqual(model.roles[1], ("writer", 1))
            self.assertEqual(model.roles[2], ("judge", 1))
            self.assertEqual(result["clean"], 1)
            self.assertEqual(result["repaired"], 1)

    def test_each_judge_rejection_is_returned_to_the_same_page_task(self):
        class FourRoundModel:
            def __init__(self):
                self.judge_calls = 0
                self.writer_messages: list[str] = []

            async def structured(self, _schema, _messages, **_kwargs):
                self.judge_calls += 1
                if self.judge_calls < 5:
                    return RepairJudgeResult(
                        approved=False,
                        coverage_score=90,
                        body_defects=[f"欠陥{self.judge_calls}"],
                    )
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, messages, **_kwargs):
                self.writer_messages.append(messages[-1].content)
                return f"# ページ\n\n正常な説明。候補{len(self.writer_messages)}\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root,
                [("ページ", "# ページ\n\n壊れた説明。\n", (1, 1))],
                ["正常な説明。"],
            )
            model = FourRoundModel()
            result = FastRepair(
                settings(root), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 1)
            self.assertEqual(model.judge_calls, 5)
            self.assertEqual(len(model.writer_messages), MAX_REWRITE_ATTEMPTS)
            for attempt, message in enumerate(model.writer_messages, start=1):
                self.assertIn(f"本文: 欠陥{attempt}", message)
            self.assertIn("--- 直前のWiki候補", model.writer_messages[1])
            self.assertIn("正常な説明。候補1", model.writer_messages[1])
            self.assertIn("それ以前の指摘", model.writer_messages[1])
            self.assertIn("本文: 欠陥1", model.writer_messages[1])

    def test_unavailable_candidate_judge_keeps_the_original_without_another_writer(self):
        class JudgeDownModel:
            def __init__(self):
                self.judge_calls = 0
                self.writer_calls = 0

            async def structured(self, _schema, _messages, **_kwargs):
                self.judge_calls += 1
                if self.judge_calls == 1:
                    return RepairJudgeResult(
                        approved=False, coverage_score=90, body_defects=["修復が必要"]
                    )
                raise TimeoutError("judge down")

            async def text(self, _messages, **_kwargs):
                self.writer_calls += 1
                return "# ページ\n\n書き直した説明。\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [("ページ", "# ページ\n\n壊れた説明。\n", (1, 1))],
                ["正常な説明。"],
            )
            model = JudgeDownModel()
            result = FastRepair(
                settings(root), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            self.assertEqual(
                (model.writer_calls, model.judge_calls), (1, 1 + JUDGE_CALL_ATTEMPTS)
            )
            published = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertIn("壊れた説明。", published)
            self.assertNotIn("書き直した説明。", published)

    def test_malformed_writer_candidate_is_retried_and_sibling_pages_continue(self):
        class MalformedWriterModel:
            def __init__(self):
                self.writer_calls = 0

            async def structured(self, _schema, messages, **_kwargs):
                prompt = messages[-1].content
                if "ページ番号: 1" in prompt:
                    return RepairJudgeResult(
                        approved=False,
                        coverage_score=90,
                        body_defects=["修復が必要"],
                    )
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, messages, **_kwargs):
                self.writer_calls += 1
                if "ページ番号: 1" in messages[-1].content:
                    return "# ページ1\n\n候補本文。\n\n# 余分なH1\n"
                raise AssertionError("clean sibling pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["壊れた説明。", "正常な説明。"],
            )
            model = MalformedWriterModel()
            result = FastRepair(
                settings(root, concurrency=2), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            self.assertEqual(result["clean"], 1)
            self.assertEqual(model.writer_calls, MAX_REWRITE_ATTEMPTS)

    def test_unexpected_page_error_is_checkpointed_and_reported_without_stopping_siblings(self):
        class CleanModel:
            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["壊れた説明。", "正常な説明。"],
            )
            repair = FastRepair(
                settings(root, concurrency=2), project=project, model=CleanModel()
            )
            from graph.fast import repair as repair_module

            original = repair_module._judge_messages

            def fail_one_page(page, title, body, **kwargs):
                if page.number == 1:
                    raise RuntimeError("injected page-local judge failure")
                return original(page, title, body, **kwargs)

            with patch("graph.fast.repair._judge_messages", side_effect=fail_one_page):
                result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            self.assertEqual(result["clean"], 1)
            self.assertEqual(len(result["failures"]), 1)
            self.assertIn("injected page-local judge failure", result["failures"][0])
            self.assertEqual(
                read_json(state_root / "state" / "pages" / "001.json")["repair_status"],
                "review",
            )

    def test_rejected_rewrites_keep_the_original_however_they_scored(self):
        class ScoredModel:
            def __init__(self):
                self.judge_calls = 0
                self.writer_calls = 0
                self.scores = [40, 70, 94, 80, 85]

            async def structured(self, _schema, _messages, **_kwargs):
                score = self.scores[self.judge_calls]
                self.judge_calls += 1
                return RepairJudgeResult(
                    approved=False,
                    coverage_score=score,
                    body_defects=[f"残る欠陥{self.judge_calls}"],
                )

            async def text(self, _messages, **_kwargs):
                self.writer_calls += 1
                return f"# ページ\n\n必要な仕様。候補{self.writer_calls}\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# ページ\n\n壊れた説明。\n"
            project, raw_rel, state_root = make_document(
                root, [("ページ", original, (1, 1))], ["必要な仕様。"]
            )
            result = FastRepair(
                settings(root), project=project, model=ScoredModel()
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            kept = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertEqual(kept, original)
            state = read_json(state_root / "state" / "pages" / "001.json")
            self.assertEqual(state["repair_status"], "review")
            self.assertEqual(state["repair_selected_attempt"], 0)
            self.assertEqual(state["repair_selection"], "original")
            self.assertEqual(state["judge_score"], 40)  # the kept page's own verdict

    def test_four_failed_rewrites_keep_original_and_create_review(self):
        class RejectingModel:
            def __init__(self):
                self.writer_calls = 0

            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(
                    approved=False, coverage_score=50, body_defects=["壊れている"]
                )

            async def text(self, _messages, **_kwargs):
                self.writer_calls += 1
                return "# 修復候補\n\n画像タグを落とした本文。\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# 壊れたページ\n\n本文。\n\n" + IMAGE + "\n"
            project, raw_rel, state_root = make_document(
                root, [("壊れたページ", original, (1, 2))], ["# 原文", IMAGE]
            )
            model = RejectingModel()
            repair = FastRepair(settings(root), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(model.writer_calls, MAX_REWRITE_ATTEMPTS)
            self.assertEqual(result["review"], 1)
            self.assertEqual((state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8"), original)
            self.assertIn("001-page-1.md", (state_root / "wiki" / "_review.md").read_text(encoding="utf-8"))
            self.assertEqual(
                read_json(state_root / "state" / "pages" / "001.json")["repair_status"],
                "review",
            )

    def test_configured_concurrency_runs_multiple_page_tasks(self):
        class SlowCleanModel:
            def __init__(self):
                self.active = 0
                self.maximum = 0

            async def structured(self, _schema, _messages, **_kwargs):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.03)
                self.active -= 1
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            pages = []
            source = []
            for number in range(1, 5):
                source.append(f"## 節{number}")
                pages.append((f"ページ{number}", f"# ページ{number}\n\n## 節{number}\n", (number, number)))
            project, raw_rel, _state_root = make_document(root, pages, source)
            model = SlowCleanModel()
            repair = FastRepair(settings(root, concurrency=3), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["clean"], 4)
            self.assertEqual(model.maximum, 3)

    def test_repair_batches_writer_rounds_between_judge_rounds(self):
        class RoundModel:
            def __init__(self):
                self.active = 0
                self.maximum = 0
                self.phase_role = None
                self.phase_count = 0
                self.phase_sizes = []
                self.judge_calls = 0

            async def _enter(self, role):
                if self.active == 0:
                    self.phase_role = role
                    self.phase_count = 0
                elif self.phase_role != role:
                    raise AssertionError("writer and judge overlapped")
                self.active += 1
                self.phase_count += 1
                self.maximum = max(self.maximum, self.active)

            async def _leave(self):
                self.active -= 1
                if self.active == 0:
                    self.phase_sizes.append((self.phase_role, self.phase_count))
                    self.phase_role = None

            async def structured(self, _schema, messages, **_kwargs):
                await self._enter("judge")
                try:
                    await asyncio.sleep(0.005)
                    self.judge_calls += 1
                    if "現在候補と同一" in messages[-1].content:
                        return RepairJudgeResult(
                            approved=False,
                            coverage_score=80,
                            body_defects=["修復が必要"],
                        )
                    return RepairJudgeResult(approved=True, coverage_score=100)
                finally:
                    await self._leave()

            async def text(self, _messages, **_kwargs):
                await self._enter("writer")
                try:
                    await asyncio.sleep(0.005)
                    return "# 修復済み\n\n正常な説明。\n"
                finally:
                    await self._leave()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            pages = [
                (f"ページ{number}", f"# ページ{number}\n\n正常な説明。\n", (number, number))
                for number in range(1, 4)
            ]
            project, raw_rel, _state_root = make_document(
                root, pages, ["正常な説明。"] * 3
            )
            model = RoundModel()
            result = FastRepair(
                settings(root, concurrency=2), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 3)
            self.assertEqual(model.maximum, 2)
            self.assertEqual(
                sum(size for role, size in model.phase_sizes if role == "writer"),
                3,
            )

    def test_bounded_pass_refills_each_finished_slot(self):
        async def run():
            first_five_started = asyncio.Event()
            release_first = asyncio.Event()
            release_slow = asyncio.Event()
            sixth_started = asyncio.Event()
            started = 0
            active = 0
            maximum = 0

            async def operation(number):
                nonlocal started, active, maximum
                started += 1
                active += 1
                maximum = max(maximum, active)
                if started == 5:
                    first_five_started.set()
                try:
                    if number == 0:
                        await release_first.wait()
                    elif number < 5:
                        await release_slow.wait()
                    else:
                        sixth_started.set()
                finally:
                    active -= 1

            task = asyncio.create_task(bounded_map(list(range(6)), 5, operation))
            await first_five_started.wait()
            release_first.set()
            await asyncio.wait_for(sixth_started.wait(), timeout=0.2)
            self.assertFalse(release_slow.is_set())
            release_slow.set()
            await task
            return maximum

        self.assertEqual(asyncio.run(run()), 5)

    def test_source_hash_mismatch_refuses_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root, [("ページ", "# ページ\n\n本文。\n", (1, 1))], ["本文。"]
            )
            project.raw_file(raw_rel).write_text("changed\n", encoding="utf-8")
            repair = FastRepair(settings(root), project=project, model=RepairingModel())
            with self.assertRaisesRegex(RuntimeError, "normal sync"):
                repair.repair_document(raw_rel, promote=False)

    def test_fast_model_pair_has_separate_roles_and_shared_gate(self):
        config = WikiConfig(
            chat_base_url="http://judge/v1",
            chat_model="large-judge",
            writer_base_url="http://writer/v1",
            writer_model="small-writer",
        )
        with patch("graph.clients.chat.make_llm", side_effect=[object(), object()]) as make:
            pair = model_pair(config)

        self.assertEqual(pair.writer.name, "small-writer")
        self.assertEqual(pair.judge.name, "large-judge")
        self.assertIs(pair.writer.gate, pair.judge.gate)
        self.assertEqual(
            [call.kwargs["model"] for call in make.call_args_list],
            ["small-writer", "large-judge"],
        )
        self.assertEqual(make.call_args_list[0].kwargs["temperature"], 0.7)

    def test_repair_targets_smallest_documents_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"

            def state_dir(raw_rel):
                return state_root / raw_rel.replace("/", "__")

            project = SimpleNamespace(metadata=root, state_dir=state_dir)
            save_ledger(root / "pipeline.json", Ledger(
                sources={
                    "large": {"raw_rel": "large.md"},
                    "small": {"raw_rel": "small.md"},
                    "medium": {"raw_rel": "medium.md"},
                },
                published_documents={},
            ))
            for raw_rel, count in (("large.md", 9), ("small.md", 1), ("medium.md", 4)):
                write_json_atomic(state_dir(raw_rel) / "state" / "plan.json", {
                    "pages": [{} for _ in range(count)]
                })

            repair = FastRepair.__new__(FastRepair)
            repair.project = project

            self.assertEqual(
                repair.targets(), ["small.md", "medium.md", "large.md"]
            )

    def test_unset_writer_follows_judge(self):
        config = WikiConfig(
            chat_base_url="http://chat/v1",
            chat_model="chat",
            judge_base_url="http://judge/v1",
            judge_model="large-judge",
        )
        with patch("graph.clients.chat.make_llm", side_effect=[object(), object()]) as make:
            pair = model_pair(config)

        self.assertEqual(pair.writer.name, "large-judge")
        self.assertEqual(pair.judge.name, "large-judge")
        self.assertEqual(
            [call.kwargs["base_url"] for call in make.call_args_list],
            ["http://judge/v1", "http://judge/v1"],
        )

    def test_fast_writer_caps_total_completion_without_changing_judge(self):
        seen: list[dict[str, object]] = []

        class Llm:
            def bind(self, **kwargs):
                seen.append(kwargs)
                return self

            async def ainvoke(self, _messages):
                return SimpleNamespace(content="# title\n\nA complete page body.")

        writer = FastModelPort(WikiConfig(), llm=Llm(), role="writer")
        judge = FastModelPort(WikiConfig(), llm=Llm(), role="judge")
        asyncio.run(writer.text([], max_output_tokens=FAST_WRITER_MAX_OUTPUT_TOKENS))
        asyncio.run(judge.text([], max_output_tokens=16000))

        self.assertEqual(seen[0]["max_tokens"], WikiConfig().write_max_output_tokens)
        self.assertNotIn("thinking_token_budget", seen[0].get("extra_body", {}))
        self.assertNotIn("thinking_token_budget", seen[1].get("extra_body", {}))
        self.assertNotIn("max_tokens", seen[1])

    def test_model_call_gate_respects_concurrency(self):
        async def run():
            gate = FastRoleGate(max_concurrency=3)
            active = 0
            maximum = 0

            async def operation():
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.02)
                active -= 1

            await asyncio.gather(*(gate.run(operation) for _ in range(8)))
            return maximum

        self.assertEqual(asyncio.run(run()), 3)

    def test_model_call_gate_keeps_roles_isolated_until_batch_drains(self):
        async def run():
            gate = FastRoleGate(max_concurrency=2)
            both_started = asyncio.Event()
            release_fast = asyncio.Event()
            release_slow = asyncio.Event()
            judge_started = asyncio.Event()
            order = []
            active_writers = 0

            async def writer_operation(label, release):
                nonlocal active_writers
                active_writers += 1
                order.append(label)
                if active_writers == 2:
                    both_started.set()
                try:
                    await release.wait()
                finally:
                    active_writers -= 1

            async def judge_operation():
                self.assertEqual(active_writers, 0)
                order.append("judge")
                judge_started.set()

            fast = asyncio.create_task(gate.run(
                lambda: writer_operation("fast", release_fast), role="writer"
            ))
            slow = asyncio.create_task(gate.run(
                lambda: writer_operation("slow", release_slow), role="writer"
            ))
            await both_started.wait()
            judge = asyncio.create_task(gate.run(judge_operation, role="judge"))
            release_fast.set()
            await asyncio.sleep(0.01)
            self.assertFalse(judge_started.is_set())
            release_slow.set()
            await asyncio.gather(fast, slow, judge)
            return order

        order = asyncio.run(run())
        self.assertEqual(set(order[:2]), {"fast", "slow"})
        self.assertEqual(order[-1], "judge")

    def test_fast_judge_has_16k_output_ceiling(self):
        seen = []

        class Structured:
            async def ainvoke(self, _messages):
                return RepairJudgeResult(approved=True, coverage_score=100)

        class JudgeLlm:
            request_timeout = None

            def bind(self, **kwargs):
                seen.append(("bind", kwargs))
                return self

            def with_structured_output(self, _schema, **kwargs):
                seen.append(("structured", kwargs))
                return Structured()

        config = WikiConfig(
            policy="fast",
            chat_base_url="http://judge-cap/v1",
            writer_base_url="http://writer-cap/v1",
        )
        with patch(
            "graph.clients.chat.make_llm", side_effect=[object(), JudgeLlm()]
        ):
            pair = model_pair(config)
        # A caller-supplied limit is clamped to the ceiling; callers that pass none (the human-sync
        # final judge) are not capped, see test_human_final_merge.
        result = asyncio.run(pair.judge.structured(RepairJudgeResult, [], max_output_tokens=50_000))

        self.assertTrue(result.approved)
        self.assertTrue(seen)
        self.assertTrue(
            all(call[1]["max_tokens"] == JUDGE_MAX_OUTPUT_TOKENS for call in seen)
        )


if __name__ == "__main__":
    unittest.main()
