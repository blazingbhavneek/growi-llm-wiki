"""Small, deliberately boring client for GROWI REST API v3."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import html
import logging
import mimetypes
import posixpath
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

from graph.common.images import DATA_IMAGE_RE, find_images
from graph.common.markdown import LINKS_FOOTER_END, LINKS_FOOTER_START
from graph.wiki.page import strip_reader_references
from graph.wiki.storage import read_json

log = logging.getLogger(__name__)


class GrowiAPIError(RuntimeError):
    def __init__(self, status_code: int, method: str, path: str, detail: str = "") -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        super().__init__(f"GROWI {method} {path} failed with HTTP {status_code}: {detail}")


class GrowiPage(BaseModel):
    page_id: str
    revision_id: str
    path: str
    title: str = ""
    body: str = ""
    updated_at: str = ""
    status: str = ""


class GrowiActivity(BaseModel):
    activity_id: str
    created_at: str
    action: str
    page_id: str = ""
    path: str = ""
    old_path: str = ""
    updated_by: str = ""
    sequence: int | None = None


_CHUNK_MARKER_RE = re.compile(
    r'^(?:<!-- chunk: (?P<comment_id>[^ ]+).*?-->|'
    r'<span hidden data-llm-wiki-chunk="(?P<span_payload>[^"]+)"></span>)[ \t]*$',
    re.MULTILINE,
)
_CHUNK_END_RE = re.compile(
    r'^(?:<!-- chunk-end: (?P<comment_id>[^ ]+) -->|'
    r'<span hidden data-llm-wiki-chunk-end="(?P<span_payload>[^"]+)"></span>)[ \t]*$',
    re.MULTILINE,
)

# One short ownership stamp per published page: an HTML comment GROWI stores verbatim and
# readers drop. The human-readable seed behind the ID lives in the pipeline ledger.
MARKER_FORMAT = "bot-ref-1"
_STAMP_RE = re.compile(r"^<!-- llm-wiki-bot-ref:(?P<id>[A-Za-z0-9_-]+) -->[ \t]*$", re.MULTILINE)


class GrowiClient:
    """Read-only GROWI client; write methods are enabled in WP-12."""

    def __init__(
        self,
        url: str,
        api_token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.transport = transport

    @property
    def api_base(self) -> str:
        return f"{self.url}/_api/v3/"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            # GROWI's v3 API documents bearer access-token authentication.
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers(),
            transport=self.transport,
        ) as client:
            response = await client.request(
                method,
                urljoin(self.api_base, path.lstrip("/")),
                params=params,
                json=json_body,
                data=data,
                files=files,
            )
        if response.is_error:
            detail = response.text[:500]
            raise GrowiAPIError(response.status_code, method, path, detail)
        return response

    @staticmethod
    def _page_from_payload(payload: Any) -> GrowiPage:
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            payload = {}
        page = payload.get("page", payload)
        if isinstance(page, list):
            page = page[0] if page else {}
        if not isinstance(page, dict):
            page = {}
        revision = page.get("revision")
        if isinstance(revision, dict):
            revision_id = (
                revision.get("_id")
                or revision.get("id")
                or revision.get("revisionId")
                or ""
            )
            body = page.get("body") or revision.get("body") or ""
        else:
            revision_id = (
                page.get("revisionId")
                or page.get("revision_id")
                or revision
                or ""
            )
            body = page.get("body") or ""
        path = str(page.get("path") or "")
        return GrowiPage(
            page_id=str(page.get("_id") or page.get("id") or page.get("pageId") or ""),
            revision_id=str(revision_id),
            path=path,
            title=str(page.get("title") or path.rstrip("/").split("/")[-1] or ""),
            body=str(body),
            updated_at=str(page.get("updatedAt") or page.get("updated_at") or ""),
            status=str(page.get("status") or ""),
        )

    async def health(self) -> bool:
        try:
            response = await self._request("GET", "/healthcheck")
        except (GrowiAPIError, httpx.HTTPError):
            return False
        return response.is_success

    async def get_page(
        self,
        path: str | None = None,
        page_id: str | None = None,
    ) -> GrowiPage | None:
        if bool(path) == bool(page_id):
            raise ValueError("provide exactly one of path or page_id")
        params = {"path": path} if path else {"pageId": page_id}
        try:
            response = await self._request("GET", "/page", params=params)
        except GrowiAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        page = self._page_from_payload(response.json())
        return page if page.page_id or page.path else None

    async def list_pages(
        self,
        root_path: str = "/",
        *,
        page: int | None = None,
        limit: int = 100,
        updated_after: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[GrowiPage], int | str | None]:
        # ``page=None`` preserves the pre-GROWI-plan cursor API for callers
        # still on the old sync path; list_all_pages uses the real page API.
        params: dict[str, Any] = {"path": root_path, "limit": limit}
        if page is not None:
            params["page"] = page
        if updated_after:
            params["updatedAfter"] = updated_after
        if cursor:
            params["cursor"] = cursor
        response = await self._request("GET", "/pages/list", params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            return [], None
        raw_pages = payload.get("pages") or payload.get("docs") or []
        pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
        if page is not None:
            return pages, int(payload.get("totalCount") or len(pages))
        paginate = payload.get("paginateResult") or {}
        raw_pages = raw_pages or paginate.get("docs", [])
        pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
        next_cursor = payload.get("nextCursor") or payload.get("next_cursor")
        if next_cursor is None:
            next_page = payload.get("nextPage")
            if next_page is not None:
                next_cursor = str(next_page)
        return pages, str(next_cursor) if next_cursor else None

    async def list_all_pages(self, root_path: str = "/") -> list[GrowiPage]:
        seen: dict[str, GrowiPage] = {}
        page = 1
        while True:
            batch, total = await self.list_pages(root_path, page=page)
            for item in batch:
                if item.page_id:
                    seen.setdefault(item.page_id, item)
            if not batch or len(seen) >= int(total) or page * 100 >= int(total):
                return list(seen.values())
            page += 1

    async def list_activities(self, *, offset: int = 0, limit: int = 100) -> tuple[list[GrowiActivity], int]:
        """Read one newest-first audit-log page using GROWI's offset contract."""

        response = await self._request(
            "POST", "/activity/list", json_body={"offset": offset, "limit": min(100, max(1, limit))}
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("malformed GROWI activity response")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ValueError("malformed GROWI activity data")
        paginate = data.get("serializedPaginationResult") or data.get("paginateResult") or data
        if not isinstance(paginate, dict):
            raise ValueError("malformed GROWI activity pagination")
        rows = paginate.get("docs") or data.get("activities") or payload.get("activities") or []
        if not isinstance(rows, list):
            raise ValueError("malformed GROWI activity rows")
        activities: list[GrowiActivity] = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError("malformed GROWI activity row")
            target_value = raw.get("target")
            target = target_value if isinstance(target_value, dict) else {}
            snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            activity_id = raw.get("_id") or raw.get("id")
            created_at = raw.get("createdAt") or raw.get("created_at")
            action = raw.get("action") or raw.get("event")
            if not activity_id or not created_at or not action:
                raise ValueError("GROWI activity is missing id, timestamp, or action")
            activities.append(GrowiActivity(
                activity_id=str(activity_id),
                created_at=str(created_at),
                action=str(action),
                page_id=str(raw.get("pageId") or snapshot.get("pageId") or target.get("pageId")
                            or target.get("id") or target.get("_id")
                            or (target_value if raw.get("targetModel") == "Page" and isinstance(target_value, str) else "")),
                path=str(raw.get("path") or snapshot.get("pagePath") or target.get("path") or raw.get("newPath") or ""),
                old_path=str(raw.get("oldPath") or snapshot.get("oldPath") or target.get("oldPath") or ""),
                updated_by=str(raw.get("userId") or user.get("_id") or user.get("id") or ""),
                sequence=int(raw["sequence"]) if raw.get("sequence") is not None else None,
            ))
        total = int(paginate.get("totalDocs") or paginate.get("totalCount") or data.get("totalCount") or len(rows))
        return activities, total

    async def create_page(self, path: str, body: str) -> GrowiPage:
        response = await self._request(
            "POST",
            "/page/",
            json_body={"path": path, "body": body},
        )
        return self._page_from_payload(response.json())

    async def revision_bodies(self, page_id: str, limit: int = 100) -> list[str] | None:
        """Bodies of the page's latest revisions; None when the history has more than ``limit``."""
        payload = (await self._request("GET", "/revisions/list", params={"pageId": page_id, "limit": limit})).json()
        revisions = payload.get("revisions") or []
        return None if int(payload.get("totalCount") or 0) > len(revisions) else [str(r.get("body") or "") for r in revisions]

    async def update_page(self, page_id: str, revision_id: str, body: str) -> GrowiPage:
        response = await self._request(
            "PUT",
            "/page/",
            json_body={
                "pageId": page_id,
                "revisionId": revision_id,
                "body": body,
                # GROWI treats editor-origin revisions as collaborative-editor
                # continuations and deliberately accepts a stale revision ID.
                # View-origin writes enforce compare-and-swap and return 409
                # when another revision won the race.
                "origin": "view",
            },
        )
        return self._page_from_payload(response.json())

    async def rename_page(self, page_id: str, revision_id: str, path: str, *, recursively: bool = False) -> GrowiPage:
        response = await self._request(
            "PUT",
            "/pages/rename",
            json_body={
                "pageId": page_id,
                "revisionId": revision_id,
                "newPagePath": path,
                "isRecursively": recursively,
                "isRenameRedirect": False,
                "updateMetadata": False,
            },
        )
        page = self._page_from_payload(response.json())
        if not page.page_id:
            refreshed = await self.get_page(page_id=page_id)
            if refreshed is None:
                raise RuntimeError(f"GROWI renamed page disappeared: {page_id}")
            page = refreshed
        if page.page_id != page_id:
            raise RuntimeError(f"GROWI rename changed page ID: {page_id} -> {page.page_id}")
        return page

    async def delete_pages(self, page_ids_to_revisions: dict[str, str]) -> None:
        items = list(page_ids_to_revisions.items())
        for start in range(0, len(items), 20):
            await self._request(
                "POST",
                "/pages/delete",
                json_body={"pageIdToRevisionIdMap": dict(items[start : start + 20])},
            )

    async def list_attachments(self, page_id: str) -> dict[str, str]:
        attachments: dict[str, str] = {}
        page_number = 1
        while True:
            response = await self._request(
                "GET",
                "/attachment/list",
                params={"pageId": page_id, "pageNumber": page_number, "limit": 100},
            )
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            result = data.get("paginateResult", data) if isinstance(data, dict) else {}
            docs = result.get("docs", []) if isinstance(result, dict) else []
            for item in docs:
                if not isinstance(item, dict) or not item.get("originalName"):
                    continue
                attachment_id = item.get("_id") or item.get("id")
                path = item.get("filePathProxied") or (f"/attachment/{attachment_id}" if attachment_id else "")
                if path:
                    attachments[str(item["originalName"])] = str(path)
            if len(docs) < 100 or page_number >= int(result.get("totalPages") or page_number):
                return attachments
            page_number += 1

    async def upload_attachment(self, page_id: str, name: str, content: bytes, mime: str) -> str:
        response = await self._request(
            "POST",
            "/attachment",
            data={"page_id": page_id},
            files={"file": (name, content, mime)},
        )
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        attachment = data.get("attachment", {}) if isinstance(data, dict) else {}
        attachment_id = attachment.get("_id") or attachment.get("id")
        path = attachment.get("filePathProxied") or (f"/attachment/{attachment_id}" if attachment_id else "")
        if not path:
            raise ValueError("GROWI attachment response contains no image path")
        return str(path)


_GROWI_BAD = re.compile(r"[\^$*+#<>%?\\]")
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)(?P<prefix>\[[^\]\n]*\]\()(?P<target>(?:(?!\]\().)*?\.md)(?P<fragment>#[^)\n]*)?\)"
)
_PERMALINK_RE = re.compile(
    r"(?<!!)(?P<prefix>\[[^\]\n]*\]\()/(?P<page_id>[^/#)\n]+)(?P<fragment>#[^)\n]*)?\)"
)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_IMAGE_DESCRIPTION_RE = re.compile(r"<image-description\b[^>]*>(.*?)</image-description>", re.I | re.S)
_HTML_COMMENT_RE = re.compile(r"<!--\s*(?:table-spec:|chunk(?:-end)?:|llm-wiki-).*?-->", re.DOTALL)  # machine comments only
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?P<text>[^`\n]+)`(?!`)")


def _growi_markdown(body: str) -> str:
    """Render generated prose for GROWI."""

    body = _HTML_COMMENT_RE.sub(
        lambda match: match.group(0) if (
            _STAMP_RE.fullmatch(match.group(0).strip())
            or match.group(0) in {LINKS_FOOTER_START, LINKS_FOOTER_END}
        ) else "",
        body,
    )
    return _rewrite_outside_fences(
        body, _INLINE_CODE_RE, lambda match: match.group("text")
    )


def _marker_id(match: re.Match[str]) -> str:
    return match.group("comment_id") or html.unescape(match.group("span_payload")).split()[0]


def _image_description(unit: str) -> str:
    match = _IMAGE_DESCRIPTION_RE.search(unit)
    text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))) if match else ""
    if text.strip():
        return " ".join(text.split())
    images = find_images(unit)
    return " ".join(images[0].alt.split()) if images and images[0].alt.strip() else "画像"


def _markdown_image(description: str, path: str = "") -> str:
    alt = description.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"![{alt}]({path})"


def _image_fallbacks(body: str) -> str:
    output: list[str] = []
    end = 0
    for image in find_images(body):
        output.append(body[end:image.start])
        output.append(_markdown_image(_image_description(image.raw), image.src if not image.media_sha256 else ""))
        end = image.end
    output.append(body[end:])
    return "".join(output)


async def _publish_images(client: GrowiClient, body: str, page_id: str) -> str:
    matches = find_images(body)
    if not matches:
        return body
    attachments = await client.list_attachments(page_id)
    output: list[str] = []
    end = 0
    for match in matches:
        output.append(body[end:match.start])
        description = _image_description(match.raw)
        data = DATA_IMAGE_RE.match(match.src)
        if data is None:
            output.append(_markdown_image(description, match.src))
            end = match.end
            continue
        mime = data.group("mime").lower()
        try:
            content = base64.b64decode("".join(data.group("data").split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid embedded image data") from exc
        extension = mimetypes.guess_extension(mime) or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        name = f"llm-wiki-{hashlib.sha256(content).hexdigest()[:20]}{extension}"
        path = attachments.get(name)
        if path is None:
            path = await client.upload_attachment(page_id, name, content, mime)
            attachments[name] = path
        output.append(_markdown_image(description, path))
        end = match.end
    output.append(body[end:])
    return "".join(output)


def growi_segment(name: str) -> str:
    cleaned = _GROWI_BAD.sub("-", name.strip()).strip("/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned or "-"


def growi_path(*parts: str) -> str:
    segments = [growi_segment(seg) for part in parts for seg in part.split("/") if seg.strip()]
    return "/" + "/".join(segments)


def rewrite_page_links(body: str, source_rel: str, page_ids: dict[str, str]) -> str:
    """Translate generated local Markdown links to stable GROWI permalinks."""
    source_dir = posixpath.dirname(source_rel)

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        resolved = posixpath.normpath(posixpath.join(source_dir, target))
        page_id = page_ids.get(resolved)
        if not page_id:
            return match.group(0)
        return f'{match.group("prefix")}/{page_id}{match.group("fragment") or ""})'

    return _rewrite_outside_fences(body, _MARKDOWN_LINK_RE, replace)


def restore_page_links(body: str, source_rel: str, page_paths: dict[str, str]) -> str:
    """Translate GROWI permalinks back to local relative Markdown paths."""
    source_dir = posixpath.dirname(source_rel)

    def replace(match: re.Match[str]) -> str:
        target = page_paths.get(match.group("page_id"))
        if not target:
            return match.group(0)
        relative = posixpath.relpath(target, source_dir or ".")
        return f'{match.group("prefix")}{relative}{match.group("fragment") or ""})'

    return _rewrite_outside_fences(body, _PERMALINK_RE, replace)


def _rewrite_outside_fences(body: str, pattern: re.Pattern[str], replace: Any) -> str:
    fence: str | None = None
    output: list[str] = []
    for line in body.splitlines(keepends=True):
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            output.append(line)
        elif line.startswith(("    ", "\t")):
            output.append(line)
        else:
            def outside_link(match: re.Match[str]) -> str:
                prefix = line[:match.start()]
                if prefix.rfind("](") > prefix.rfind(")"):
                    return match.group(0)
                return replace(match)

            output.append(line if fence is not None else pattern.sub(outside_link, line))
    return "".join(output)


def _page_stamps(body: str) -> list[re.Match]:
    """Locate real ownership stamps, ignoring examples inside fenced code."""
    from graph.common.markdown import scan_markdown_fences

    lines = body.splitlines(keepends=True)
    scan = scan_markdown_fences(lines).inside_after_line
    flags = [inside or (i > 0 and scan[i - 1]) for i, inside in enumerate(scan)]
    stamps, offset = [], 0
    for line, fenced in zip(lines, flags):
        if not fenced and _STAMP_RE.fullmatch(line.rstrip("\n")):
            stamps.append(_STAMP_RE.match(body, offset))
        offset += len(line)
    return stamps


def managed_page_markdown(body: str, marker_id: str) -> str | None:
    """Recover locally editable content while excluding unowned remote text."""
    stamps = _page_stamps(body)
    if stamps:
        if len(stamps) != 1 or stamps[0].group("id") != marker_id:
            return None
        return body[:stamps[0].start()].rstrip() + "\n"
    # Pages published before the bottom stamp existed, until the next sweep rewrites them.
    sections = _marked_sections(body)
    if marker_id not in sections:
        return None
    recovered: list[str] = []
    for chunk_id, (start, end) in sorted(sections.items(), key=lambda item: item[1][0]):
        section = body[start:end]
        if chunk_id in {marker_id, marker_id + "-links"}:
            lines = section.splitlines()
            if lines and _CHUNK_MARKER_RE.fullmatch(lines[0]):
                lines.pop(0)
            if lines and _CHUNK_END_RE.fullmatch(lines[-1]):
                lines.pop()
            section = "\n".join(lines)
        recovered.append(section.strip("\n"))
    return "\n\n".join(part for part in recovered if part).rstrip() + "\n"


def team_of_path(path: str, write_path: str) -> str | None:
    base = "/" + write_path.strip("/")
    rest = path
    if base != "/":
        if not path.startswith(base.rstrip("/") + "/"):
            return None
        rest = path[len(base):]
    head, sep, _tail = rest.strip("/").partition("/")
    return head if sep and head else None


def wrap_page(body: str, *, page_id: str) -> str:
    """Close a page with its ownership stamp, so the body starts with real content."""

    if _page_stamps(body):
        return body
    return f"{body.rstrip()}\n\n<!-- llm-wiki-bot-ref:{page_id} -->\n"


def split_footer(body: str) -> tuple[str, str]:
    """Split the managed linker footer from the generated body."""
    from publisher.human_changes import footer_span

    span = footer_span(body)
    if span is None:
        return body, ""
    start, end = span
    return body[:start] + body[end:], body[start:end]


def assert_publish_path(path: str, *, mode: str, write_path: str, root_path: str = "/") -> None:
    """Reject writes outside the configured attach/own boundary."""
    normalized = "/" + posixpath.normpath(path).lstrip("/")
    boundary = "/" + posixpath.normpath(write_path or "/").lstrip("/")
    if mode == "attach" and not (
        normalized == boundary or normalized.startswith(boundary.rstrip("/") + "/")
    ):
        raise PermissionError(
            f"attach-mode GROWI write outside write_path: {normalized} not under {boundary}"
        )
    if mode == "own":
        own_boundary = "/" + posixpath.normpath(root_path or "/").lstrip("/")
        if not (
            own_boundary == "/"
            or normalized == own_boundary
            or normalized.startswith(own_boundary.rstrip("/") + "/")
        ):
            raise PermissionError(
                f"own-mode GROWI write outside root_path: {normalized} not under {own_boundary}"
            )


def _marked_sections(body: str) -> dict[str, tuple[int, int]]:
    matches = list(_CHUNK_MARKER_RE.finditer(body))
    sections: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        boundary = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        end = _CHUNK_END_RE.search(body, match.end(), boundary)
        marker_id = _marker_id(match)
        sections[marker_id] = (
            match.start(),
            end.end() if end and _marker_id(end) == marker_id else boundary,
        )
    return sections


def merge_marked_sections(existing: str, additions: str) -> str:
    """Replace the page above our stamp, keeping any text a human added below it."""
    if not _page_stamps(additions):
        raise ValueError("GROWI publish body contains no chunk markers")
    stamps = _page_stamps(existing)
    if stamps:
        tail = existing[stamps[-1].end():].strip("\n")
        return additions.rstrip() + ("\n\n" + tail + "\n" if tail else "\n")
    if existing.strip() and not _CHUNK_MARKER_RE.search(existing):
        # Somebody else's page: publish alongside it instead of overwriting it.
        return existing.rstrip() + "\n\n" + additions.rstrip() + "\n"
    # A page we published in the older chunk-marker format is wholly ours to rewrite.
    return additions


def _complete_page(page: GrowiPage, path: str, body: str) -> GrowiPage:
    if page.path and page.body:
        return page
    return page.model_copy(update={"path": page.path or path, "body": page.body or body})


async def publish_pages(
    client: GrowiClient,
    pages: list[dict[str, str]],
    *,
    mode: str,
    write_path: str,
    root_path: str = "/",
    known_page_ids: dict[str, str] | None = None,
    expected_pages: dict[str, dict[str, Any]] | None = None,
    on_revision: Any = None,
    on_conflict: Any = None,
    on_prepared: Any = None,
    on_confirmed: Any = None,
    on_reconcile: Any = None,
    on_progress: Any = None,
) -> list[GrowiPage]:
    """Resolve every page ID first, then publish stable permalink bodies."""
    current: dict[str, GrowiPage | None] = {}

    async def report_page(
        step: str,
        index: int,
        item: dict[str, str],
        *,
        stage: str = "growi-publish",
        **extra: Any,
    ) -> None:
        await _maybe_await(on_progress, {
            "stage": stage,
            "step": step,
            "current": index,
            "total": len(pages),
            "page": item.get("local_path") or item.get("path") or "<page>",
            **extra,
        })

    # Inspect the whole batch before creating/updating any page. The following
    # PUT still uses this exact revision, so a later race fails with HTTP 409.
    for index, item in enumerate(pages, 1):
        try:
            path = item["path"]
            body = item["body"]
            assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
            expected = (expected_pages or {}).get(item.get("local_path", ""))
            existing = await client.get_page(**({"page_id": str(expected["page_id"])} if expected else {"path": path}))
            if expected:
                if existing is None:
                    raise RuntimeError(f"GROWI page disappeared before publication: {path}")
                if existing.path != path or existing.page_id != expected.get("page_id"):
                    raise RuntimeError(f"GROWI page moved before publication: {path}")
                if not expected.get("revision_id") or existing.revision_id != expected["revision_id"]:
                    raise RuntimeError(f"GROWI page changed before publication: {path}")
                if managed_page_markdown(existing.body, str(expected.get("marker_id") or "")) is None:
                    raise RuntimeError(f"GROWI ownership marker missing before publication: {path}")
            elif existing is not None:
                stamps = _page_stamps(body)
                marker = stamps[0] if len(stamps) == 1 else None
                if marker is None or managed_page_markdown(existing.body, marker.group("id")) is None:
                    await _maybe_await(on_conflict, item, existing, "unowned_destination")
                    raise RuntimeError(f"GROWI destination is not owned by this page: {path}")
                initial = _growi_markdown(_image_fallbacks(body))
                if merge_marked_sections(existing.body, initial) != existing.body:
                    if not await _maybe_await(on_reconcile, item, existing):
                        await _maybe_await(on_conflict, item, existing, "missing_published_snapshot")
                        raise RuntimeError(f"GROWI destination has no inspected published baseline: {path}")
            current[path] = existing
            await report_page(
                "page_checked",
                index,
                item,
                stage="growi-preflight",
                status="existing" if existing is not None else "new",
            )
        except Exception as exc:
            await report_page(
                "page_failed",
                index,
                item,
                stage="growi-preflight",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
    await _maybe_await(on_progress, {
        "stage": "growi-preflight",
        "step": "done",
        "current": len(pages),
        "total": len(pages),
        "pages": len(pages),
    })
    for item in pages:
        path, body = item["path"], item["body"]
        existing = current[path]
        if existing is None:
            initial_body = _growi_markdown(_image_fallbacks(body))
            await _maybe_await(on_prepared, item, None, initial_body)
            existing = _complete_page(await client.create_page(path, initial_body), path, initial_body)
            await _maybe_await(on_confirmed, item, existing, initial_body)
            await _maybe_await(on_revision, existing)
        if not existing.page_id:
            raise ValueError(f"GROWI returned no page ID for {path}")
        current[path] = existing

    page_ids = dict(known_page_ids or {})
    page_ids.update({
        item["local_path"]: current[item["path"]].page_id
        for item in pages
        if item.get("local_path") and current[item["path"]].page_id
    })
    results: list[GrowiPage] = []
    try:
        for index, item in enumerate(pages, 1):
            path = item["path"]
            existing = current[path]
            body = rewrite_page_links(item["body"], item.get("local_path", ""), page_ids)
            body = await _publish_images(client, body, existing.page_id)
            merged = merge_marked_sections(existing.body, _growi_markdown(body))
            if merged == existing.body:
                results.append(existing)
                await _maybe_await(on_revision, existing)
                await report_page("page_done", index, item, status="unchanged")
                continue
            await _maybe_await(on_prepared, item, existing, merged)
            try:
                page = _complete_page(
                    await client.update_page(existing.page_id, existing.revision_id, merged), path, merged
                )
            except GrowiAPIError as exc:
                if exc.status_code == 409:
                    observed = await client.get_page(page_id=existing.page_id)
                    await _maybe_await(on_conflict, item, observed, "revision_race")
                raise
            results.append(page)
            await _maybe_await(on_confirmed, item, page, merged)
            await _maybe_await(on_revision, page)
            await report_page("page_done", index, item, status="published")
    except Exception as exc:
        failed_index = len(results)
        failed_item = pages[failed_index] if failed_index < len(pages) else pages[-1]
        await report_page("page_failed", failed_index, failed_item, error=f"{type(exc).__name__}: {exc}")
        raise
    return results


class GrowiPublisher:
    """Publish one generated document and trash only pages marked by us."""

    def __init__(
        self,
        client: GrowiClient,
        connection: Any,
        *,
        human_sync_policy: Any = None,
        human_model_factory: Any = None,
    ) -> None:
        from graph.config import HumanSyncPolicy

        self.client = client
        self.connection = connection
        self.on_revision: Any = None
        # Runtime construction always supplies Settings.  The compatibility
        # default keeps direct deterministic callers on their historic path.
        self.human_sync_policy = human_sync_policy or HumanSyncPolicy.resolve("apply")
        self.human_model_factory = human_model_factory
        self.human_sync_summary: dict[str, Any] = {}

    def doc_path(self, project: Any, rel: str) -> str:
        folder = project.wiki_dir(rel).relative_to(project.wiki).as_posix()
        return growi_path(self.connection.write_path, folder)

    def page_marker_seed(self, project: Any, local_path: str) -> str:
        """The readable page identity behind a page's stamp ID (the local half of the map)."""
        document = Path(local_path).parent.as_posix()
        marker = read_json(Path(project.wiki) / document / "_planning" / "source.json", default={})
        id_seed = str(marker.get("id_seed") or document)
        return growi_path(self.connection.write_path, id_seed, Path(local_path).name)

    def page_marker_id(self, project: Any, local_path: str) -> str:
        seed = self.page_marker_seed(project, local_path)
        return "b" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]

    def _document_pages(self, project: Any, rel: str) -> list[dict[str, str]]:
        folder = project.wiki_dir(rel)
        doc_path = self.doc_path(project, rel)
        pages: list[dict[str, str]] = []
        for md in sorted(folder.glob("*.md")):
            name = growi_segment(md.name)
            body = strip_reader_references(md.read_text(encoding="utf-8"))
            page_path = f"{doc_path}/{name}"
            local_path = md.relative_to(project.wiki).as_posix()
            page_id = self.page_marker_id(project, local_path)
            pages.append({
                "local_path": local_path,
                "path": page_path,
                "body": wrap_page(body, page_id=page_id),
            })
        return pages

    def publish_documents(
        self,
        project: Any,
        rels: list[str],
        known_pages: dict[str, dict[str, Any]] | None = None,
        only_pages: set[str] | None = None,
        cleanup_revisions: dict[str, str] | None = None,
        on_progress: Any = None,
    ) -> dict[str, GrowiPage]:
        from publisher.human_changes import HumanStore

        HumanStore(project).audit()
        pages: list[dict[str, str]] = []
        document_paths: dict[str, set[str]] = {}
        for rel in dict.fromkeys(rels):
            document_pages = self._document_pages(project, rel)
            if only_pages is not None:
                document_pages = [
                    page for page in document_pages if page["local_path"] in only_pages
                ]
            # Newest page first: GROWI lists by last-updated, so 001 lands on top (00-目次 follows after).
            pages.extend(reversed(document_pages))
            document_paths[self.doc_path(project, rel)] = {page["path"] for page in document_pages}
        if len({page["path"] for page in pages}) != len(pages):
            raise ValueError("multiple local wiki pages resolve to the same GROWI path")
        if on_progress is not None:
            on_progress({
                "stage": "growi-publish",
                "step": "start",
                "current": 0,
                "total": len(pages),
                "documents": len(rels),
                "pages": len(pages),
            })
        scoped = {
            path: row for path, row in (known_pages or {}).items()
            if any(path.startswith(project.wiki_dir(rel).relative_to(project.wiki).as_posix() + "/") for rel in rels)
        }
        self.assert_known_revisions(scoped)
        def record_conflict(item: dict, observed: GrowiPage | None, reason: str) -> None:
            from publisher.human_changes import HumanStore

            if observed is None:
                return
            store = HumanStore(project)
            marker = self.page_marker_id(project, item["local_path"])
            data = store.page(marker)
            data.update({"schema_version": 1, "marker_id": marker,
                         "observed_revision": observed.revision_id,
                         "observed_remote_blob": store.put(observed.body), "publication_error": reason,
                         "publication_error_attempt_id": str(data.get("prepared_attempt_id") or "")})
            attempt_id = str(data.get("prepared_attempt_id") or "")
            for attempt in data.get("attempt_history", []):
                if attempt.get("attempt_id") == attempt_id:
                    attempt.update({"status": "rejected" if reason == "revision_race" else "failed",
                                    "error": reason, "observed_revision": observed.revision_id})
            store.save_page(data)

        def record_prepared(item: dict, inspected: GrowiPage | None, body: str) -> None:
            store = HumanStore(project)
            marker = self.page_marker_id(project, item["local_path"])
            data = store.page(marker)
            remote_blob = store.put(body)
            local_blob = store.put((project.wiki / item["local_path"]).read_text(encoding="utf-8"))
            attempt_id = "hattempt-" + uuid.uuid4().hex[:24]
            history = data.setdefault("attempt_history", [])
            previous_attempt = str(data.get("prepared_attempt_id") or "")
            for attempt in history:
                if attempt.get("attempt_id") == previous_attempt and attempt.get("status") not in {
                    "recovered_exact", "captured_late_human", "not_landed",
                }:
                    attempt.update({"status": "superseded", "settled_at": datetime.now(timezone.utc).isoformat()})
            history.append({
                "attempt_id": attempt_id,
                "status": "prepared",
                "path": item["path"],
                "page_id": inspected.page_id if inspected else "",
                "revision": inspected.revision_id if inspected else "",
                "remote_blob": remote_blob,
                "local_blob": local_blob,
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            })
            data.update({"schema_version": 1, "marker_id": marker, "local_path": item["local_path"],
                         "prepared_attempt_id": attempt_id,
                         "prepared_path": item["path"], "prepared_page_id": inspected.page_id if inspected else "",
                         "prepared_revision": inspected.revision_id if inspected else "",
                         # A fresh attempt starts without a conflict, so a recorded
                         # publication_error always describes this prepared write.
                         "publication_error": "", "publication_error_attempt_id": "",
                         "prepared_remote_blob": remote_blob,
                         "prepared_local_blob": local_blob})
            store.save_page(data)

        def record_confirmed(item: dict, page: GrowiPage, body: str) -> None:
            """Persist per-page success before the next page can be mutated."""
            store = HumanStore(project)
            marker = self.page_marker_id(project, item["local_path"])
            data = store.page(marker)
            attempt_id = str(data.get("prepared_attempt_id") or "")
            if not attempt_id:
                return
            confirmation = {
                "attempt_id": attempt_id,
                "page_id": page.page_id,
                "revision": page.revision_id,
                "path": page.path,
                "remote_blob": store.put(body),
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            data["publication_confirmation"] = confirmation
            for attempt in data.get("attempt_history", []):
                if attempt.get("attempt_id") == attempt_id:
                    attempt.update({"status": "confirmed", **confirmation})
            store.save_page(data)

        def reconcile_prepared(item: dict, observed: GrowiPage) -> bool:
            """True when an unledgered page already holds exactly the body we prepared."""
            from publisher.human_changes import HumanStore

            return bool(HumanStore(project).prepared_match(self.page_marker_id(project, item["local_path"]), observed))

        results = asyncio.run(publish_pages(
            self.client,
            pages,
            mode=self.connection.mode,
            write_path=self.connection.write_path,
            root_path=self.connection.root_path,
            known_page_ids={
                path: str(row.get("page_id"))
                for path, row in (known_pages or {}).items()
                if row.get("page_id")
            },
            expected_pages=scoped,
            on_revision=self.on_revision,
            on_conflict=record_conflict,
            on_prepared=record_prepared,
            on_confirmed=record_confirmed,
            on_reconcile=reconcile_prepared,
            on_progress=on_progress,
        ))
        if on_progress is not None:
            on_progress({
                "stage": "growi-publish",
                "step": "done",
                "current": len(results),
                "total": len(pages),
                "documents": len(rels),
                "pages": len(results),
            })
        if only_pages is None:
            for doc_path, keep in document_paths.items():
                asyncio.run(self._trash_under(doc_path, keep=keep, expected_revisions={
                    **(cleanup_revisions or {}),
                    **{str(row["page_id"]): str(row["revision_id"]) for row in scoped.values()},
                }, on_deleted=lambda page: self._remember_deleted(project, scoped, page)))
        return {item["local_path"]: page for item, page in zip(pages, results)}

    def _remember_deleted(self, project: Any, known_pages: dict[str, dict[str, Any]], page: GrowiPage) -> None:
        from publisher.human_changes import HumanStore, now

        item = next(((path, row) for path, row in known_pages.items() if row.get("page_id") == page.page_id), None)
        if item is None:
            return
        local_path, row = item
        store = HumanStore(project)
        marker = str(row.get("marker_id") or self.page_marker_id(project, local_path))
        data = store.page(marker)
        data.update({"schema_version": 1, "marker_id": marker, "deleted_page_id": page.page_id,
                     "deleted_revision": page.revision_id, "deleted_path": page.path,
                     "deleted_remote_blob": store.put(page.body), "deleted_at": now()})
        store.save_page(data)

    def discover_documents(self, project: Any, rels: list[str]) -> dict[str, GrowiPage]:
        pages = [page for rel in dict.fromkeys(rels) for page in self._document_pages(project, rel)]

        async def fetch() -> list[GrowiPage | None]:
            return [await self.client.get_page(path=page["path"]) for page in pages]

        return {
            item["local_path"]: page
            for item, page in zip(pages, asyncio.run(fetch()))
            if page is not None
        }

    def assert_known_revisions(self, pages: dict[str, dict[str, Any]]) -> None:
        async def check() -> None:
            for local_path, row in pages.items():
                page_id = str(row.get("page_id") or "")
                revision_id = str(row.get("revision_id") or "")
                if not page_id or not revision_id:
                    raise RuntimeError(f"cannot verify GROWI revision for {local_path}")
                current = await self.client.get_page(page_id=page_id)
                if current is None:
                    raise RuntimeError(f"GROWI page disappeared: {local_path}")
                if row.get("growi_path") and current.path != row["growi_path"]:
                    raise RuntimeError(f"GROWI page moved by another editor: {current.path}")
                if row.get("marker_id") and managed_page_markdown(current.body, str(row["marker_id"])) is None:
                    raise RuntimeError(f"GROWI ownership markers were removed: {current.path}")
                if current.revision_id != revision_id:
                    raise RuntimeError(f"GROWI page changed by another editor: {current.path}")

        asyncio.run(check())

    def publish_document(self, project: Any, rel: str) -> list[GrowiPage]:
        return list(self.publish_documents(project, [rel]).values())

    def move_document(
        self,
        project: Any,
        old_rel: str,
        new_rel: str,
        known_pages: dict[str, dict[str, Any]],
        *,
        check_revisions: bool = True,
    ) -> dict[str, GrowiPage]:
        """Rename owned pages in place, then refresh their managed bodies."""
        old_doc_path = self.doc_path(project, old_rel)
        new_doc_path = self.doc_path(project, new_rel)

        async def rename() -> dict[str, GrowiPage]:
            moved: dict[str, GrowiPage] = {}
            inspected: dict[str, GrowiPage] = {}
            for local_path, row in sorted(known_pages.items()):
                page_id = str(row.get("page_id") or "")
                if not page_id:
                    continue
                current = await self.client.get_page(page_id=page_id)
                if current is None:
                    raise RuntimeError(f"GROWI page missing during move: {page_id}")
                if current.page_id != page_id:
                    raise RuntimeError(f"GROWI page ID mismatch during move: {page_id}")
                expected_revision = str(row.get("revision_id") or "")
                if check_revisions and not expected_revision:
                    raise RuntimeError(f"cannot verify GROWI revision for move: {local_path}")
                if check_revisions and expected_revision and current.revision_id != expected_revision:
                    raise RuntimeError(f"GROWI page changed by another editor: {current.path}")
                if row.get("growi_path") and current.path != row["growi_path"]:
                    raise RuntimeError(f"GROWI page moved by another editor: {current.path}")
                marker = str(row.get("marker_id") or self.page_marker_id(project, local_path))
                if managed_page_markdown(current.body, marker) is None:
                    raise RuntimeError(f"GROWI ownership markers were removed: {current.path}")
                inspected[page_id] = current
            root = await self.client.get_page(path=old_doc_path)
            if root is not None:
                root = await self.client.rename_page(root.page_id, root.revision_id, new_doc_path, recursively=False)
                await _maybe_await(self.on_revision, root)
            for local_path, row in sorted(known_pages.items()):
                page_id = str(row.get("page_id") or "")
                if not page_id:
                    continue
                current = inspected[page_id]
                destination = f"{new_doc_path}/{growi_segment(Path(local_path).name)}"
                # GROWI moves child pages with the document page, even with isRecursively false.
                fresh = await self.client.get_page(page_id=page_id) if root is not None else None
                page = fresh if fresh is not None and fresh.path == destination else (
                    current if current.path == destination
                    else await self.client.rename_page(page_id, current.revision_id, destination)
                )
                if page.page_id != page_id:
                    raise RuntimeError(f"GROWI move did not retain page ID: {page_id}")
                if page.body and page.body != current.body:
                    raise RuntimeError(f"GROWI page body changed during move: {destination}")
                page = _complete_page(page, destination, current.body)
                await _maybe_await(self.on_revision, page)
                destination_local = project.wiki_dir(new_rel).relative_to(project.wiki).as_posix() + "/" + Path(local_path).name
                moved[destination_local] = page
            return moved

        moved = asyncio.run(rename())
        published = self.publish_documents(project, [new_rel], known_pages={
            path: {"page_id": page.page_id, "revision_id": page.revision_id,
                   "growi_path": page.path, "marker_id": self.page_marker_id(project, path)}
            for path, page in moved.items()
        })
        if moved and {page.page_id for page in published.values()} != {page.page_id for page in moved.values()}:
            raise RuntimeError("GROWI move changed the published page set")
        return published

    def pull_changes(
        self,
        project: Any,
        published_pages: dict[str, dict[str, Any]],
        unchanged_documents: set[str],
        on_progress: Any = None,
    ) -> tuple[list[str], list[str], set[str]]:
        """Accept human revisions: each changed page is re-based onto the document's current pages."""
        from publisher.human_changes import HumanStore, editable, merge, rebase

        store = HumanStore(project)
        store.audit()
        policy = self.human_sync_policy
        mode = policy.mode.value
        model = self.human_model_factory(project) if self.human_model_factory is not None else None

        async def fetch() -> dict[str, GrowiPage | None]:
            return {
                local_path: await self.client.get_page(page_id=str(row.get("page_id", "")))
                for local_path, row in published_pages.items()
                if row.get("page_id")
            }

        remote = asyncio.run(fetch())
        if on_progress is not None:
            on_progress({
                "stage": "growi-preflight",
                "step": "start",
                "current": 0,
                "total": len(remote),
                "pages": len(remote),
            })
        page_paths = {str(row["page_id"]): path for path, row in published_pages.items() if row.get("page_id")}
        pulled, conflicts, blocked = [], [], set()
        decisions = {"unchanged": 0, "observed": 0, "captured": 0, "blocked": 0, "recovered": 0}
        wiki_root = Path(project.wiki).resolve()

        for index, (local_path, page) in enumerate(remote.items(), 1):
            row = published_pages[local_path]
            document = posixpath.dirname(local_path)
            row["marker_id"] = str(row.get("marker_id") or self.page_marker_id(project, local_path))
            target = (wiki_root / local_path).resolve()
            try:
                target.relative_to(wiki_root)
                if page is None:
                    raise ValueError("remote_deleted")
                if page.status == "deleted":
                    raise ValueError("remote_deleted")
                if page.page_id != row.get("page_id"):
                    raise ValueError("remote page ID mismatch")
                if row.get("growi_path") and page.path != row["growi_path"]:
                    raise ValueError("remote_moved")
                markdown = managed_page_markdown(page.body, row["marker_id"])
                if markdown is None:
                    raise ValueError("GROWI ownership markers were removed")
                stamp = read_json(target.parent / "_planning" / "source.json", default={})
                raw_rel = str(stamp.get("raw") or "")
                if not raw_rel:
                    raise ValueError("source mapping is missing")
                baseline = store.page(row["marker_id"])
                if baseline.get("blocked") or row.get("human_sync_blocked"):
                    # Rechecked on every pull: the checks decide again, so a resolved page (moved back,
                    # mode switched) is unblocked even without a new revision, and a real problem blocks again.
                    baseline["blocked"] = ""
                    row.pop("human_sync_blocked", None)
                    store.save_page(baseline)
                store.retire_unlanded(row["marker_id"], page)
                if baseline and baseline.get("accepted_revision") == row.get("revision_id") == page.revision_id:
                    decisions["unchanged"] += 1
                    log.debug(
                        "human_sync event=unchanged document=%s page=%s revision=%s",
                        document, local_path, page.revision_id,
                    )
                    continue
                try:
                    accounted = store.account_prepared(row, page, remote=markdown)
                except ValueError:
                    # An unconfirmed write (for example an HTTP 500) under a later revision: the page
                    # history decides. Only if no revision holds its exact body did it never land.
                    prepared = store.prepared_pending(row["marker_id"], page)
                    bodies = asyncio.run(self.client.revision_bodies(page.page_id)) if hasattr(self.client, "revision_bodies") else None
                    if bodies is None or store.get(prepared["prepared_remote_blob"]) in bodies:
                        raise
                    store.settle_prepared(prepared, status="not_landed")
                    accounted = {}
                if accounted.get("exact"):
                    # The page holds our own unrecorded write: bot intent, not human.
                    decisions["recovered"] += 1
                    log.info(
                        "GROWIパブリッシュ状態を復旧しました: ドキュメント=%s ページ=%s",
                        document, local_path,
                    )
                    log.debug(
                        "human_sync event=recovered_exact document=%s page=%s revision=%s",
                        document, local_path, page.revision_id,
                    )
                    continue
                local = target.read_text(encoding="utf-8") if not baseline else store.get(baseline["local_blob"])
                if not baseline:
                    # Old ledgers have no exact remote snapshot. A clean revision
                    # can establish one; a changed one cannot be told apart from
                    # transport spelling, so it blocks until a baseline exists.
                    if document not in unchanged_documents:
                        raise ValueError("missing published snapshot for changed local page")
                    expected_local = _growi_markdown(strip_reader_references(local))
                    canonical_remote = restore_page_links(markdown, local_path, page_paths)
                    if editable(canonical_remote) != editable(expected_local):
                        if policy.observes:
                            store.record_observation(
                                mode=mode, decision="blocked", local_path=local_path,
                                page_id=page.page_id, revision_id=page.revision_id,
                                before=expected_local, after=canonical_remote,
                                proposed_operation="replace", proposed_status="blocked",
                                match_reason="missing_verified_baseline",
                            )
                            decisions["observed"] += 1
                        store.record_event(
                            mode=mode, decision="blocked_remote_difference", local_path=local_path,
                            page_id=page.page_id, revision_id=page.revision_id,
                            reason="missing_verified_baseline",
                        )
                        raise ValueError("missing verified baseline; the remote page differs from the local page")
                    row["revision_id"] = page.revision_id
                    store.remember_page(local_path, row, markdown, local, published=False)
                    log.debug(
                        "human_sync event=baseline_registered document=%s page=%s revision=%s",
                        raw_rel, local_path, page.revision_id,
                    )
                    continue
                if baseline["accepted_revision"] != row.get("revision_id"):
                    raise ValueError("published revision and human baseline disagree")
                if page.revision_id == row.get("revision_id"):
                    log.debug(
                        "human_sync event=revision_unchanged document=%s page=%s revision=%s",
                        raw_rel, local_path, page.revision_id,
                    )
                    continue
                previous_remote = store.get(baseline["remote_blob"])
                previous_local = store.get(baseline["local_blob"])
                if accounted:
                    # A late revision sits on our unrecorded write, so it is rebased on
                    # that write: only the human delta is accepted and the bot delta stays
                    # generated state.
                    prepared_remote = managed_page_markdown(accounted["remote"], row["marker_id"])
                    if prepared_remote is None:
                        raise ValueError("unrecorded bot write is missing its ownership marker")
                    previous_remote, previous_local = prepared_remote, accounted["local"]
                # Rebase the exact remote delta onto its matching local snapshot.
                # This restores permalink/image spelling without inventing text.
                canonical, status = merge(editable(previous_remote), editable(markdown), editable(previous_local))
                if status == "conflict":
                    page_name = Path(local_path).name
                    log.info(
                        "GROWIユーザー編集の複雑な差分をマージしています: ドキュメント=%s ページ=%s",
                        raw_rel, local_path,
                    )
                    merged, entries = rebase(
                        {page_name: editable(previous_remote)},
                        {page_name: editable(markdown)},
                        {page_name: editable(previous_local)},
                        model=model if policy.captures else None,
                        concurrency=getattr(model, "concurrency", 1),
                    )
                    canonical = merged[page_name]
                    for entry in entries:
                        if entry.strip() and entry not in canonical:
                            canonical = canonical.rstrip() + "\n\n" + entry.strip() + "\n"
                    log.info(
                        "GROWIユーザー編集の複雑な差分をマージしました: ドキュメント=%s ページ=%s",
                        raw_rel, local_path,
                    )
                    log.debug(
                        "human_sync event=transport_rebased document=%s page=%s revision=%s appendix_entries=%d",
                        raw_rel, local_path, page.revision_id, len(entries),
                    )
                canonical = restore_page_links(canonical, local_path, page_paths)
                # Linker output and transport spelling are not human changes, in every mode.
                base, human = store.separate(raw_rel, local_path, previous_local, canonical)
                if base != human:
                    proposed_operation = (
                        "delete" if not editable(canonical).strip()
                        else "add" if not editable(previous_local).strip()
                        else "replace"
                    )
                    log.debug(
                        "human_sync event=user_edit_detected document=%s page=%s revision=%s operation=%s mode=%s",
                        raw_rel, local_path, page.revision_id, proposed_operation, mode,
                    )
                    if policy.observes:
                        store.record_observation(
                            mode=mode,
                            decision="capture" if policy.captures else "blocked",
                            local_path=local_path,
                            page_id=page.page_id,
                            revision_id=page.revision_id,
                            before=previous_local,
                            after=canonical,
                            proposed_operation=proposed_operation,
                            proposed_status=status,
                            match_reason="deterministic_page_rebase",
                        )
                        decisions["observed"] += 1
                    if not policy.captures:
                        store.record_event(
                            mode=mode,
                            decision="blocked_remote_difference",
                            local_path=local_path,
                            page_id=page.page_id,
                            revision_id=page.revision_id,
                            reason="authoritative_capture_disabled",
                        )
                        raise ValueError(
                            f"remote difference requires human_sync_mode=apply (current mode: {mode})"
                        )
                row["observed_revision_id"] = page.revision_id
                if base != human:
                    rewritten = sorted(store.accept(
                        raw_rel, local_path, row["marker_id"], page.revision_id, base, human, model=model,
                    ))
                    pulled.extend(rewritten)
                    decisions["captured"] += 1
                    operation_ja = {"add": "追加", "delete": "削除", "replace": "更新"}[proposed_operation]
                    log.info(
                        "GROWIユーザー編集を取り込みました: ドキュメント=%s ページ=%s 操作=%s",
                        raw_rel, local_path, operation_ja,
                    )
                    log.debug(
                        "human_sync event=captured document=%s page=%s revision=%s operation=%s rewritten=%s",
                        raw_rel, local_path, page.revision_id, proposed_operation, rewritten,
                    )
                else:
                    log.info(
                        "GROWIページ更新を確認しました（ユーザー編集なし）: ドキュメント=%s ページ=%s",
                        raw_rel, local_path,
                    )
                    log.debug(
                        "human_sync event=accepted_without_user_edit document=%s page=%s revision=%s",
                        raw_rel, local_path, page.revision_id,
                    )
                row["revision_id"] = page.revision_id
                row["marker_seed"] = self.page_marker_seed(project, local_path)
                store.remember_page(local_path, row, markdown, canonical, published=False)
                if accounted and accounted.get("attempt_id"):
                    settled = store.page(row["marker_id"])
                    if settled.get("prepared_attempt_id") == accounted["attempt_id"]:
                        store.settle_prepared(settled, status="captured_late_human")
            except (OSError, KeyError, TypeError, ValueError) as exc:
                log.exception("GROWIのプルに失敗: %s", local_path)
                reason = str(exc)
                store.block_page(local_path, row, reason, page)
                blocked.add(document)
                conflicts.append(f"{local_path}: {reason}")
                decisions["blocked"] += 1
            finally:
                if on_progress is not None:
                    on_progress({
                        "stage": "growi-preflight",
                        "step": "page_done",
                        "current": index,
                        "total": len(remote),
                        "page": local_path,
                    })
        if on_progress is not None:
            on_progress({
                "stage": "growi-preflight",
                "step": "done",
                "current": len(remote),
                "total": len(remote),
                "pages": len(remote),
            })
        self.human_sync_summary = {"mode": mode, **decisions}
        return list(dict.fromkeys(pulled)), conflicts, blocked

    def delete_document(self, project: Any, rel: str, *, known_pages: dict[str, dict[str, Any]] | None = None) -> int:
        return asyncio.run(self._trash_under(self.doc_path(project, rel), keep=set(), expected_revisions=(
            {str(row["page_id"]): str(row["revision_id"]) for row in known_pages.values()}
            if known_pages is not None else None
        ), on_deleted=lambda page: self._remember_deleted(project, known_pages or {}, page)))

    def reset(self, *, on_progress: Any = None) -> int:
        """Trash every publisher-marked page below the configured write path."""
        return asyncio.run(self._trash_under(
            growi_path(self.connection.write_path), keep=set(), on_progress=on_progress,
        ))

    async def _trash_under(self, doc_path: str, *, keep: set[str], expected_revisions: dict[str, str] | None = None,
                           on_deleted: Any = None, on_progress: Any = None) -> int:
        doomed: dict[str, str] = {}
        inspected: dict[str, GrowiPage] = {}
        listed_pages = [
            listed for listed in await self.client.list_all_pages(doc_path)
            if listed.path != doc_path and listed.path not in keep
        ]
        if on_progress is not None:
            on_progress({
                "stage": "reset", "step": "start", "current": 0,
                "total": len(listed_pages), "pages": len(listed_pages),
            })
        for index, listed in enumerate(listed_pages, 1):
            full = await self.client.get_page(page_id=listed.page_id)
            if full and (_STAMP_RE.search(full.body) or _CHUNK_MARKER_RE.search(full.body)):
                if expected_revisions is not None and full.revision_id != expected_revisions.get(full.page_id):
                    raise RuntimeError(f"GROWI page changed before deletion: {full.path}")
                doomed[full.page_id] = full.revision_id
                inspected[full.page_id] = full
            if on_progress is not None:
                on_progress({
                    "stage": "reset", "step": "page_checked", "current": index,
                    "total": len(listed_pages), "page": listed.path,
                    "status": "owned" if full and full.page_id in doomed else "skipped",
                })
        items = list(doomed.items())
        for start in range(0, len(items), 20):
            batch = dict(items[start:start + 20])
            await self.client.delete_pages(batch)
            for page_id in batch:
                await _maybe_await(on_deleted, inspected[page_id])
        if on_progress is not None:
            on_progress({
                "stage": "reset", "step": "done", "current": len(listed_pages),
                "total": len(listed_pages), "deleted": len(doomed),
            })
        return len(doomed)


async def _sync_growi_pages_legacy(
    client: GrowiClient,
    registry: Any,
    connection: Any,
    *,
    on_page: Any,
    on_delete: Any,
    on_rename: Any | None = None,
) -> dict[str, int | str | None]:
    """Incrementally sync a GROWI page listing into a local index.

    The sync cursor is recorded only after every page callback succeeds. A
    crash therefore repeats work safely instead of skipping an unseen page.
    """
    # A changed-only listing cannot prove that an absent page was deleted.
    # Enumerate the root on each poll, then fetch bodies only for new/revised
    # pages.  The cursor is still honored for pagination within this crawl.
    remote_by_id: dict[str, GrowiPage] = {}
    cursor: str | None = None
    next_cursor: str | None = None
    while True:
        batch, batch_cursor = await client.list_pages(
            connection.root_path,
            updated_after=None,
            cursor=cursor,
        )
        for page in batch:
            remote_by_id[page.page_id] = page
        next_cursor = batch_cursor
        if not batch_cursor or batch_cursor == cursor:
            break
        cursor = batch_cursor
    remote = list(remote_by_id.values())
    local = {item.page_id: item for item in registry.pages(connection.name)}
    remote_ids = {page.page_id for page in remote}
    added = changed = renamed = deleted = 0

    for listed in remote:
        previous = local.get(listed.page_id)
        if previous and previous.revision_id == listed.revision_id:
            if previous.path != listed.path:
                if on_rename is not None:
                    await _maybe_await(on_rename, previous, listed)
                registry.upsert_page(
                    registry_page(
                        connection.name,
                        listed,
                        indexed_at=previous.indexed_at,
                    )
                )
                renamed += 1
            continue

        full = await client.get_page(page_id=listed.page_id)
        if full is None:
            continue
        await _maybe_await(on_page, full, previous)
        registry.upsert_page(registry_page(connection.name, full))
        if previous is None:
            added += 1
        else:
            changed += 1

    for page_id, previous in local.items():
        if page_id in remote_ids:
            continue
        await _maybe_await(on_delete, previous)
        registry.delete_page(connection.name, page_id)
        deleted += 1

    # This is intentionally last: callback failures leave the old cursor in
    # place, so a subsequent run cannot skip the failed page.
    registry.record_sync(
        connection.name,
        cursor=next_cursor,
        synced_at=datetime.now(timezone.utc).isoformat(),
        error=None,
    )
    return {
        "added": added,
        "changed": changed,
        "renamed": renamed,
        "deleted": deleted,
        "cursor": next_cursor,
    }


def _parent(path: str) -> str:
    return posixpath.dirname(path.rstrip("/")) or "/"


async def sync_growi_pages(
    client: GrowiClient,
    registry: Any,
    connection: Any,
    *,
    on_document: Any | None = None,
    on_delete_document: Any | None = None,
    on_page: Any | None = None,
    on_delete: Any | None = None,
    on_rename: Any | None = None,
) -> dict[str, Any]:
    """Diff GROWI and revise each changed document folder atomically."""
    if on_document is None or on_delete_document is None:
        return await _sync_growi_pages_legacy(
            client, registry, connection,
            on_page=on_page or (lambda *_: None),
            on_delete=on_delete or (lambda *_: None),
            on_rename=on_rename,
        )
    remote = {p.page_id: p for p in await client.list_all_pages(connection.root_path)}
    local = {p.page_id: p for p in registry.pages(connection.name)}
    touched: set[str] = set()
    for page_id, page in remote.items():
        previous = local.get(page_id)
        if previous is None or previous.revision_id != page.revision_id or previous.path != page.path:
            touched.add(_parent(page.path))
            if previous is not None:
                touched.add(_parent(previous.path))
    for page_id, previous in local.items():
        if page_id not in remote:
            touched.add(_parent(previous.path))
    by_document: dict[str, list[GrowiPage]] = {}
    for page in remote.values():
        by_document.setdefault(_parent(page.path), []).append(page)
    revised = deleted = 0
    for document in sorted(touched):
        listed = sorted(by_document.get(document, []), key=lambda p: p.path)
        if not listed:
            await _maybe_await(on_delete_document, document)
            deleted += 1
        else:
            full = [await client.get_page(page_id=p.page_id) for p in listed]
            await _maybe_await(on_document, document, [p for p in full if p is not None])
            revised += 1
        for page in listed:
            registry.upsert_page(registry_page(connection.name, page))
        for page_id, previous in local.items():
            if page_id not in remote and _parent(previous.path) == document:
                registry.delete_page(connection.name, page_id)
    registry.record_sync(
        connection.name,
        cursor=None,
        synced_at=datetime.now(timezone.utc).isoformat(),
        error=None,
    )
    return {"pages": len(remote), "documents_revised": revised, "documents_deleted": deleted}


async def _maybe_await(callback: Any, *args: Any) -> Any:
    if callback is None:
        return None
    result = callback(*args)
    if hasattr(result, "__await__"):
        return await result
    return result


def registry_page(name: str, page: GrowiPage, *, indexed_at: str | None = None) -> Any:
    from .registry import GrowiPageIndex

    return GrowiPageIndex(
        name=name,
        page_id=page.page_id,
        revision_id=page.revision_id,
        path=page.path,
        indexed_at=indexed_at or datetime.now(timezone.utc).isoformat(),
    )
