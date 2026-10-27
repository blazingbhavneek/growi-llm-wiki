from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import graph.workspace.parser_client as parser_client
from graph.wiki.images import neutralize_image_descriptions, reuse_image_descriptions
from graph.workspace.parser_client import LLM_WIKI_PARSE_PATH, parse_document


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Settings:
    chat_base_url = "http://llm/v1"
    chat_api_key = "k"
    chat_model = "m"


def _image(payload: str, description: str = "") -> str:
    return (
        '<image-unit>\n  <image-media><img src="data:image/png;base64,'
        f'{payload}" alt="diagram"></image-media>\n'
        f"  <image-description>{description}</image-description>\n"
        "</image-unit>"
    )


class ParserClientTests(unittest.TestCase):
    def test_xlsm_upload_repairs_only_legacy_vml_and_preserves_source(self) -> None:
        stream = io.BytesIO()
        vml = b"<xml><font>before<br>after</font></xml>"
        with zipfile.ZipFile(stream, "w") as archive:
            archive.comment = b"keep comment"
            archive.writestr("xl/drawings/vmlDrawing1.vml", vml)
            archive.writestr("xl/vbaProject.bin", b"unchanged macro bytes")
            archive.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        original = stream.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsm"
            path.write_bytes(original)
            with parser_client._open_parser_upload(path) as upload, zipfile.ZipFile(upload) as archive:
                self.assertEqual(archive.read("xl/drawings/vmlDrawing1.vml"), vml.replace(b"<br>", b"<br/>"))
                self.assertEqual(archive.read("xl/vbaProject.bin"), b"unchanged macro bytes")
                self.assertEqual(archive.read("xl/worksheets/sheet1.xml"), b"<worksheet/>")
                self.assertEqual(archive.comment, b"keep comment")
            self.assertEqual(path.read_bytes(), original)

    def test_valid_xlsm_upload_is_byte_identical(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("xl/drawings/vmlDrawing1.vml", b"<xml><br>valid paired tag</br></xml>")
        original = stream.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsm"
            path.write_bytes(original)
            with parser_client._open_parser_upload(path) as upload:
                self.assertEqual(upload.read(), original)

    def test_uses_the_llm_wiki_route(self) -> None:
        self.assertEqual(LLM_WIKI_PARSE_PATH, "/parse/llm-wiki")

        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active["A1"] = 1
        workbook.save(stream)
        workbook.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x", "pages": ["## foo"]}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser/agent/doc-parser",
                    settings=_Settings(),
                )

            self.assertEqual(result, "# x")
        self.assertEqual(
            post.call_args.args[0],
            "http://parser/agent/doc-parser/parse/llm-wiki",
        )
        self.assertEqual(post.call_args.kwargs["headers"], {})
        self.assertEqual(post.call_args.kwargs["params"]["describe_images"], "true")

    def test_pdf_defaults_to_docling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x"}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser",
                    settings=_Settings(),
                )

        self.assertEqual(result, "# x")
        self.assertEqual(post.call_args.kwargs["params"]["pdf_client"], "docling")

    def test_rejects_malformed_pages(self) -> None:
        stream = io.BytesIO()
        Workbook().save(stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x", "pages": "not-a-list"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "pages must be a list"):
                    parse_document(
                        path,
                        base_url="http://parser",
                        settings=_Settings(),
                    )

    def test_update_reuses_unchanged_image_description_without_vision_call(self) -> None:
        previous = _image("YWJj", "old description")
        current = _image("YWJj")
        stream = io.BytesIO()
        Workbook().save(stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": current}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser",
                    settings=_Settings(),
                    previous_markdown=previous,
                )

        self.assertIn("<image-description>old description</image-description>", result)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["params"]["describe_images"], "true")

    def test_reuse_keeps_only_first_description_when_parser_deduplicates(self) -> None:
        previous = _image("YWJj", "first") + "\n" + _image("YWJj")
        current = _image("YWJj") + "\n" + _image("YWJj")
        calls: list[str] = []
        result = reuse_image_descriptions(
            previous,
            current,
            lambda data_url, alt: calls.append(data_url) or "unexpected",
            repeat_descriptions=False,
        )
        self.assertEqual(result, previous)
        self.assertEqual(calls, [])

    def test_reuse_repeats_descriptions_by_default(self) -> None:
        previous = _image("YWJj", "first") + "\n" + _image("YWJj")
        current = _image("YWJj") + "\n" + _image("YWJj")
        result = reuse_image_descriptions(previous, current, lambda _data_url, _alt: "unexpected")
        self.assertEqual(result, _image("YWJj", "first") + "\n" + _image("YWJj", "first"))

    def test_update_uses_parser_for_new_image_descriptions(self) -> None:
        previous = _image("YWJj", "keep me")
        current = previous.replace("keep me", "") + "\n" + _image("ZGVm", "parser description")
        stream = io.BytesIO()
        Workbook().save(stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": current}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser",
                    settings=_Settings(),
                    previous_markdown=previous,
                )

        self.assertEqual(post.call_count, 1)
        self.assertIn("<image-description>keep me</image-description>", result)
        self.assertIn("<image-description>parser description</image-description>", result)
        self.assertEqual(post.call_args.kwargs["params"]["describe_images"], "true")
        self.assertEqual(post.call_args.kwargs["headers"], {})

    def test_image_delete_needs_no_description_call(self) -> None:
        calls: list[str] = []
        result = reuse_image_descriptions(
            "before\n" + _image("YWJj", "old") + "\nafter",
            "before\nafter",
            lambda data_url, alt: calls.append(data_url) or "unexpected",
        )
        self.assertEqual(result, "before\nafter")
        self.assertEqual(calls, [])

    def test_description_wording_is_not_part_of_source_diff(self) -> None:
        old = _image("YWJj", "first wording")
        new = _image("YWJj", "completely different wording")
        self.assertEqual(
            neutralize_image_descriptions(old),
            neutralize_image_descriptions(new),
        )


class _NoDescribeSettings(_Settings):
    parser_describe_images = False


class ParserClientDescribeSwitchTests(unittest.TestCase):
    def _workbook_bytes(self) -> bytes:
        stream = io.BytesIO()
        Workbook().save(stream)
        return stream.getvalue()

    def test_disabled_sends_false_on_fresh_convert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(self._workbook_bytes())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x"}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser",
                    settings=_NoDescribeSettings(),
                )
            self.assertEqual(result, "# x")
            self.assertEqual(post.call_args.kwargs["params"]["describe_images"], "false")

    def test_disabled_update_reuses_cache_without_vision_call(self) -> None:
        previous = _image("YWJj", "keep me")
        current = previous.replace("keep me", "") + "\n" + _image("ZGVm")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(self._workbook_bytes())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": current}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser",
                    settings=_NoDescribeSettings(),
                    previous_markdown=previous,
                )
            # Only the parser call happens; no chat/completions vision call.
            self.assertEqual(post.call_count, 1)
            self.assertIn("<image-description>keep me</image-description>", result)
            self.assertIn("<image-description></image-description>", result)

    def test_explicit_kwarg_overrides_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(self._workbook_bytes())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x"}),
            ) as post:
                parse_document(
                    path,
                    base_url="http://parser",
                    settings=_NoDescribeSettings(),
                    describe_images=True,
                )
            self.assertEqual(post.call_args.kwargs["params"]["describe_images"], "true")


if __name__ == "__main__":
    unittest.main()
