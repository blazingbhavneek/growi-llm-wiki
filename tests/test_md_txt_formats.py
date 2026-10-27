from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graph.formats import kind_of, structural_seed_plan
from graph.formats import md, pdf
from graph.wiki.markdown_blocks import build_block_index
from graph.workspace.convert import convert_mount
from graph.workspace.project import Project
from publisher.pipeline import _parse
from publisher.scanner import scan_mount


CONFIG = SimpleNamespace(
    structure_target_lines=40,
    structure_min_lines=5,
    pdf_use_headings=False,
)


class MarkdownAndTextFormatTests(unittest.TestCase):
    def test_txt_uses_pdf_llm_path(self) -> None:
        self.assertEqual(kind_of("notes_txt.md"), "pdf")
        self.assertIsNone(pdf.plan(["plain text"] * 50, config=CONFIG))
        self.assertIsNone(asyncio.run(structural_seed_plan(
            ["plain text"] * 50,
            kind=kind_of("notes_txt.md"),
            config=CONFIG,
            model=None,
        )))

    def test_structured_markdown_uses_heading_plan(self) -> None:
        lines = (
            ["# Manual", "intro"]
            + ["## Install"] + [f"install {n}" for n in range(30)]
            + ["## Operate"] + [f"operate {n}" for n in range(30)]
        )
        plan = asyncio.run(structural_seed_plan(
            lines, kind="md", config=CONFIG, model=None
        ))
        self.assertIsNotNone(plan)
        self.assertTrue(all(page.chapter and page.path for page in plan.pages))

    def test_headingless_markdown_uses_llm_path(self) -> None:
        self.assertIsNone(md.plan([f"line {n}" for n in range(100)], config=CONFIG))

    def test_atomic_markdown_blocks_have_no_internal_safe_cut(self) -> None:
        lines = [
            "before",
            '<TABLE class="wide">', "<tr>", "<td>value</td>", "</tr>", "</TABLE>",
            "> quote one", "> quote two",
            "```python", "print('ok')", "```",
            "| a | b |", "|---|---|", "| 1 | 2 |",
            "<image-unit>", "![diagram](asset.png)", "</image-unit>",
            "after",
        ]
        index = build_block_index(lines)
        for start, end in ((2, 6), (7, 8), (9, 11), (12, 14), (15, 17)):
            for cut in range(start + 1, end + 1):
                self.assertFalse(index.cut_is_safe(cut), (start, end, cut))

    def test_unclosed_html_table_does_not_fail(self) -> None:
        index = build_block_index(["before", "<table>", "still open"])
        self.assertTrue(index.cut_is_safe(3))

    def test_txt_is_scanned_and_read_without_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mount"
            mount.mkdir()
            source = mount / "notes.txt"
            source.write_text("plain text", encoding="utf-8")
            scan = scan_mount(mount)
            self.assertIn("notes.txt", scan.files)
            item = scan.files["notes.txt"]
            self.assertEqual(item.parser, "txt")
            with patch("publisher.pipeline.parse_document") as parser:
                self.assertEqual(_parse(item, source, SimpleNamespace()), "plain text")
                parser.assert_not_called()

    def test_vendored_dependency_folders_are_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "mount"
            for rel in ("docs/real.md", "docs/node_modules/left-pad/README.md",
                        "docs/node_modules/left-pad/docs/guide.md",
                        ".venv/Lib/site-packages/pkg/readme.md", "venv/share/notes.txt"):
                path = mount / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("text", encoding="utf-8")
            self.assertEqual(set(scan_mount(mount).files), {"docs/real.md"})

    def test_convert_copies_txt_without_parser_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(root / "data", root / "mount").ensure()
            project.mount.mkdir()
            (project.mount / "notes.txt").write_text("plain text", encoding="utf-8")
            result = convert_mount(project, parser_base_url="", settings=SimpleNamespace())
            self.assertEqual(result["converted"], ["notes.txt"])
            self.assertEqual((project.raw / "notes_txt.md").read_text(encoding="utf-8"), "plain text")


if __name__ == "__main__":
    unittest.main()
