"""HTTP client for the separate doc-parser service."""

from __future__ import annotations

import codecs
import io
import json
import logging
import mimetypes
import re
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from graph.wiki.images import reuse_image_descriptions

from .xlsm import apply_manifest, build_manifest, repair_legacy_vml

log = logging.getLogger(__name__)


def safe_endpoint(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))

# The graph pipeline always requests the llm-wiki profile so its historical
# image-unit, description, and XLSM lineage behavior is preserved. The generic
# /parse route produces ordinary Markdown and must never be used here.
LLM_WIKI_PARSE_PATH = "/parse/llm-wiki"
DEFAULT_PDF_CLIENT = "docling"

_IMAGE_DESCRIPTION_RE = re.compile(
    r"(?P<open><image-description\b[^>]*>)(?P<body>.*?)(?P<close></image-description>)",
    re.IGNORECASE | re.DOTALL,
)


def _repair_image_description_fences(markdown: str) -> str:
    """Keep parser image descriptions from breaking the source Markdown scan."""

    from graph.common.markdown import parse_markdown_fence_marker, scan_markdown_fences

    def repair(match: re.Match[str]) -> str:
        body = match.group("body")
        lines = body.splitlines()
        scan = scan_markdown_fences(lines)
        if scan.unclosed is not None:
            marker = scan.unclosed.marker_char * scan.unclosed.marker_length
            body = body.rstrip("\r\n") + "\n" + marker + "\n"
        elif body and not body.endswith(("\n", "\r")) and lines:
            _char, _length, rest = parse_markdown_fence_marker(lines[-1]) or ("", 0, "")
            if _char and not rest.strip():
                # The parser sometimes appends </image-description> directly to
                # an otherwise valid closing fence line.
                body += "\n"
        return match.group("open") + body + match.group("close")

    return _IMAGE_DESCRIPTION_RE.sub(repair, markdown)


class UnsupportedDocument(RuntimeError):
    pass


# TEMPORARY: set False to disable ALL preflight skips and run-only rejection caching.
# Nothing is permanently excluded; the next sync can try the files again.
TEMPORARY_PARSER_PREFLIGHT = True


class ParserSkip(UnsupportedDocument):
    """A confirmed preflight rejection, or HTTP 415 from every parser endpoint."""


_parser_skips: dict[tuple[str, str], str] | None = None


@contextmanager
def parser_skip_scope():
    """Share rejections with parse-ahead only for this CLI command."""
    global _parser_skips
    previous = _parser_skips
    _parser_skips = {}
    try:
        yield
    finally:
        _parser_skips = previous


def parser_skip_cache() -> dict[tuple[str, str], str] | None:
    return _parser_skips if TEMPORARY_PARSER_PREFLIGHT else None


TEXT_SOURCE_SUFFIXES = {
    ".csv", ".htm", ".html", ".ini", ".json", ".log", ".md", ".rst",
    ".sql", ".svg", ".tex", ".tsv", ".txt", ".vtt", ".xml", ".yaml", ".yml",
}


def _legacy_text_score(text: str) -> tuple[int, int, int]:
    """Prefer the legacy-Japanese decoding that produces normal Japanese text."""

    japanese = sum(
        1
        for char in text
        if "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
    )
    halfwidth = sum("\uff61" <= char <= "\uff9f" for char in text)
    controls = sum(
        1 for char in text if ord(char) < 32 and char not in "\r\n\t"
    )
    return japanese, -halfwidth, -controls


def decode_text_bytes(raw: bytes, *, source: Path | None = None) -> str:
    """Decode text sources and return Unicode without changing source bytes."""

    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates: list[str] = []
    for encoding in ("euc_jp", "cp932", "iso2022_jp"):
        try:
            candidates.append(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    if not candidates:
        label = str(source) if source is not None else "<bytes>"
        raise UnicodeError(f"unsupported text encoding: {label}")
    return max(candidates, key=_legacy_text_score)


def read_text_source(path: Path) -> str:
    text = decode_text_bytes(path.read_bytes(), source=path)
    if TEMPORARY_PARSER_PREFLIGHT and not text.strip():
        raise ParserSkip("スキップ: テキストが空です")
    return text


def preflight_document(path: Path) -> None:
    """TEMPORARY: reject only confirmed empty/password-protected sources."""
    if not TEMPORARY_PARSER_PREFLIGHT:
        return
    if path.stat().st_size == 0:
        raise ParserSkip("スキップ: ファイルが空です（0バイト）")
    encrypted = False
    try:
        if path.suffix.lower() in {".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".pptx"}:
            import msoffcrypto
            import olefile

            with path.open("rb") as handle:
                if olefile.isOleFile(handle):
                    with olefile.OleFileIO(handle) as archive:
                        encrypted = archive.exists("EncryptionInfo") and archive.exists("EncryptedPackage")
                    if not encrypted:
                        handle.seek(0)
                        encrypted = msoffcrypto.OfficeFile(handle).is_encrypted()
        elif path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            with path.open("rb") as handle:
                reader = PdfReader(handle)
                encrypted = reader.is_encrypted and not reader.decrypt("")
    except Exception:  # Inspection is advisory: unknown/corrupt formats still reach the parser.
        log.debug("parser preflight inconclusive: %s", path.name, exc_info=True)
    if encrypted:
        raise ParserSkip("スキップ: 暗号化されたドキュメントです")


def _open_parser_upload(path: Path) -> BinaryIO:
    """Normalize uploads in memory; never rewrite the source file."""

    if path.suffix.lower() == ".xlsm":
        with zipfile.ZipFile(path) as archive:
            replacements = {}
            for item in archive.infolist():
                if item.filename.startswith("xl/drawings/") and item.filename.endswith(".vml"):
                    original = archive.read(item)
                    repaired = repair_legacy_vml(original)
                    if repaired != original:
                        replacements[item.filename] = repaired
            if replacements:
                upload = io.BytesIO()
                with zipfile.ZipFile(upload, "w") as normalized:
                    normalized.comment = archive.comment
                    for item in archive.infolist():
                        normalized.writestr(item, replacements.get(item.filename, archive.read(item)))
                upload.seek(0)
                return upload
    if path.suffix.lower() not in TEXT_SOURCE_SUFFIXES:
        return path.open("rb")
    return io.BytesIO(read_text_source(path).encode("utf-8"))


def _parser_content_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if path.suffix.lower() in TEXT_SOURCE_SUFFIXES:
        return f"{guessed or 'text/plain'}; charset=utf-8"
    return guessed or "application/octet-stream"


def parse_document(
    path: Path,
    *,
    base_url: str,
    settings: Any,
    timeout_s: float = 7200,
    previous_markdown: str | None = None,
    describe_images: bool | None = None,
    validate_markdown: Callable[[str], None] | None = None,
) -> str:
    preflight_document(Path(path))  # TEMPORARY: runs before XLSM inspection or any upload.
    # The parser is an independent service. Wiki chat credentials must never
    # be forwarded here: doing so makes parser-side image descriptions call
    # the model used by wiki generation. If descriptions are explicitly
    # enabled, the parser uses its own server-side configuration.
    headers: dict[str, str] = {}
    if describe_images is None:
        # Settings flag (WIKI_PARSER_DESCRIBE_IMAGES) controls parser-side
        # vision calls. Missing attributes retain the historical default.
        describe_images = bool(getattr(settings, "parser_describe_images", True))
    manifest = build_manifest(path)
    endpoints = [base_url.rstrip("/")]
    fallback = str(getattr(settings, "parser_fallback_base_url", "") or "").strip().rstrip("/")
    if fallback and fallback not in endpoints:
        endpoints.append(fallback)
    unsupported_endpoints = 0
    for index, endpoint in enumerate(endpoints):
        started = time.monotonic()
        # Opening/preparing a local source is not an endpoint failure. Each
        # request gets a fresh stream of the same immutable queued source.
        with _open_parser_upload(Path(path)) as handle:
            try:
                log.info("stage=parser endpoint=%s attempt=%d file=%s timeout=%.0fs",
                         safe_endpoint(endpoint), index + 1, path.name, timeout_s)
                params = {
                    "images": "true",
                    "describe_images": "true" if describe_images else "false",
                }
                if Path(path).suffix.lower() == ".pdf":
                    params["pdf_client"] = DEFAULT_PDF_CLIENT
                response = requests.post(
                    f"{endpoint}{LLM_WIKI_PARSE_PATH}",
                    params=params,
                    headers=headers,
                    data={"manifest": json.dumps(manifest, ensure_ascii=False)} if manifest else None,
                    files={"file": (Path(path).name, handle, _parser_content_type(Path(path)))},
                    timeout=(30, timeout_s),
                )
                if response.status_code == 415:
                    unsupported_endpoints += 1
                    raise UnsupportedDocument(response.text[:200])
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("doc-parser returned invalid JSON") from exc
                if not isinstance(payload, dict) or "markdown" not in payload:
                    raise RuntimeError("doc-parser: missing markdown or invalid response")
                markdown = payload["markdown"]
                if not isinstance(markdown, str) or not markdown.strip():
                    raise RuntimeError("doc-parser: markdown must be a nonempty string")
                pages = payload.get("pages")
                if pages is not None and (not isinstance(pages, list) or not all(isinstance(page, str) for page in pages)):
                    raise RuntimeError("doc-parser: pages must be a list of strings")
                markdown = apply_manifest(markdown, manifest) if manifest else markdown
                if not markdown.strip():
                    raise RuntimeError("doc-parser: manifest produced empty markdown")
                if validate_markdown is not None:
                    validate_markdown(markdown)
            except Exception as exc:
                log.exception("stage=parser endpoint=%s elapsed=%.2fs fallback=%s",
                              safe_endpoint(endpoint), time.monotonic() - started, index + 1 < len(endpoints))
                if index + 1 == len(endpoints):
                    if TEMPORARY_PARSER_PREFLIGHT and unsupported_endpoints == len(endpoints):
                        raise ParserSkip(f"スキップ: 全パーサーがフォーマットを認識できません: {exc}") from exc
                    raise
            else:
                log.info("stage=parser endpoint=%s elapsed=%.2fs characters=%d",
                         safe_endpoint(endpoint), time.monotonic() - started, len(markdown))
                break
    if previous_markdown is not None:
        # Reuse descriptions already present in the previous output. New
        # descriptions come from the parser service and its own configuration.
        describe = lambda _data_url, _alt: ""
        markdown = reuse_image_descriptions(
            previous_markdown,
            markdown,
            describe,
            repeat_descriptions=Path(path).suffix.lower() != ".pptx",
        )
    return _repair_image_description_fences(markdown)


__all__ = [
    "UnsupportedDocument", "parse_document", "LLM_WIKI_PARSE_PATH", "DEFAULT_PDF_CLIENT",
    "TEXT_SOURCE_SUFFIXES", "decode_text_bytes", "read_text_source",
]
