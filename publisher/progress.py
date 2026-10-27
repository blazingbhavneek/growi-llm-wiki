"""Terminal progress rendering for the foreground sync command."""

from __future__ import annotations

import datetime
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is an optional terminal aid
    tqdm = None

log = logging.getLogger("progress")


NAME_KEEP = 5
NAME_THRESHOLD = 25
NAME_JOIN = "....."
# 5 + 5 + 5 characters are at most 25 columns when CJK counts as two.
DOC_WIDTH = 25


def _cells(value: str) -> int:
    """Terminal columns a string occupies, counting CJK as two."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def shorten_name(value: str, threshold: int = NAME_THRESHOLD) -> str:
    """Keep the first and last NAME_KEEP characters of a name wider than threshold."""
    if _cells(value) <= threshold:
        return value
    return f"{value[:NAME_KEEP]}{NAME_JOIN}{value[-NAME_KEEP:]}"


class LiveLogFormatter(logging.Formatter):
    """Lay out one timestamped live record without rewriting its message.

    Never emits a traceback, ANSI escape or carriage return, so ``live.log`` stays a
    staff-readable single-line record whatever the debug handler does with the same record.
    """

    _ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")

    def __init__(self, *, run_id: str = "", redact: Any = None) -> None:
        super().__init__("%(message)s")
        self.run_id = run_id
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        exception = record.exc_info[1] if record.exc_info and record.exc_info[1] is not None else None
        if exception is not None:
            text = f"{text} エラー={type(exception).__name__}: {exception}"
        text = self._ANSI.sub("", text)
        if self.redact is not None:
            text = self.redact(text)
        stamp = datetime.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        level = {logging.INFO: "情報", logging.WARNING: "警告", logging.ERROR: "エラー",
                 logging.CRITICAL: "重大エラー", logging.DEBUG: "デバッグ"}.get(record.levelno, record.levelname)
        fields = [stamp, level]
        if self.run_id:
            fields.append(f"run={self.run_id}")
        fields.append(" ".join(text.split()))  # one physical line: no CR/LF, no traceback
        return " ".join(field for field in fields if field)


@dataclass
class _Phase:
    label: str
    started: float | None = None
    total: int | None = None
    current: int = 0
    completed: int = 0
    reported: bool = False


class SyncProgress:
    """Render one document bar and one transient bar for the active phase.

    Progress is intentionally kept outside the pipeline.  The pipeline emits
    structured events; this class owns terminal redraws and timing only.
    """

    _LABELS = {
        "capture": "キャプチャー",
        "parse": "パーサー",
        "planner": "プランナー",
        "research": "ウィキリサーチ",
        "writer": "ウィキ生成・チェック",
        "fast-repair": "ウィキリペア・チェック",
        "linker-entities": "リンクエンティティ",
        "linker-edges": "メインリンク",
        "growi-preflight": "パブリッシュ事前チェック",
        "growi-publish": "パブリッシュ",
        "reset": "リモートページのリセット",
        "reset-index": "インデックスのリセット",
        "index": "インデックス",
    }

    def __init__(self, *, stream: Any = None) -> None:
        self.stream = stream or sys.stderr
        self._progress_stream = self.stream
        self._tty = bool(getattr(self._progress_stream, "isatty", lambda: False)())
        self._bars_enabled = tqdm is not None and self._tty
        self._documents: set[str] = set()
        self._completed: set[str] = set()
        self._phases: dict[tuple[str, str], _Phase] = {}
        self._active_key: tuple[str, str] | None = None
        self._stage_bar: Any = None
        self._doc_bar: Any = None
        if tqdm is not None:
            self._doc_bar = tqdm(
                total=0,
                desc="ドキュメント",
                unit="件",
                position=0,
                leave=True,
                dynamic_ncols=True,
                disable=not self._bars_enabled,
                file=self._progress_stream,
            )

    def add_documents(self, paths: Iterable[Any]) -> None:
        for path in paths:
            if isinstance(path, dict):
                path = path.get("to") or path.get("path") or ""
            value = str(path or "")
            if not value or value in self._documents:
                continue
            self._documents.add(value)
        if self._doc_bar is not None:
            self._doc_bar.total = len(self._documents)
            self._doc_bar.refresh()

    @property
    def has_documents(self) -> bool:
        return bool(self._documents)

    def add_scan_result(self, result: dict[str, Any]) -> None:
        moved = result.get("moved") or []
        self.add_documents([
            *result.get("added", []),
            *result.get("updated", []),
            *result.get("deleted", []),
            *(item.get("to", "") if isinstance(item, dict) else item for item in moved),
        ])

    def mark_completed(self, paths: Iterable[Any]) -> None:
        for path in paths:
            value = str(path or "")
            if not value:
                continue
            self.add_documents([value])
            if value in self._completed:
                continue
            self._completed.add(value)
            if self._doc_bar is not None:
                self._doc_bar.update(1)

    def _phase_name(self, event: dict[str, Any]) -> str | None:
        stage = str(event.get("stage") or "")
        step = str(event.get("step") or "")
        if stage in self._LABELS:
            return stage
        if stage == "linker":
            if step in {"chunks", "entities"}:
                return "linker-entities"
            if step in {"edge_target_done", "incremental_scope", "page_curated"}:
                return "linker-edges"
        return None

    @staticmethod
    def _document(event: dict[str, Any]) -> str:
        if str(event.get("stage") or "") in {
            "capture", "growi-publish", "reset", "reset-index", "index"
        }:
            return "<batch>"
        value = event.get("document") or event.get("file") or "<batch>"
        value = str(value)
        if value.startswith("/"):
            value = Path(value).name
        return shorten_name(value)

    def _write(self, message: str) -> None:
        """Progress milestones are log records, so live.log gets them without a tee."""
        log.info(message)

    def _close_visible(self) -> None:
        if self._stage_bar is not None:
            self._stage_bar.close()
            self._stage_bar = None
        self._active_key = None

    def _ensure_phase(
        self,
        key: tuple[str, str],
        *,
        total: int | None = None,
    ) -> _Phase:
        phase = self._phases.get(key)
        if phase is None or phase.reported:
            phase = _Phase(self._LABELS[key[1]])
            self._phases[key] = phase
        if phase.started is None:
            phase.started = time.monotonic()
            log.info("[%s] %s: 開始", key[0].replace('<batch>', '<バッチ>'), phase.label)
        if total is not None and total > 0:
            phase.total = total
        if self._active_key != key:
            self._close_visible()
            self._active_key = key
            if tqdm is not None:
                self._stage_bar = tqdm(
                    total=phase.total,
                    desc=f"{key[0].replace('<batch>', '<バッチ>')} | {phase.label}",
                    unit="件",
                    position=1,
                    leave=False,
                    dynamic_ncols=True,
                    disable=not self._bars_enabled,
                    file=self._progress_stream,
                )
        elif self._stage_bar is not None and phase.total != self._stage_bar.total:
            self._stage_bar.total = phase.total
            self._stage_bar.refresh()
        return phase

    def _update_bar(self, phase: _Phase, *, step: str = "") -> None:
        if self._stage_bar is None:
            return
        desired = max(self._stage_bar.n, phase.current)
        if desired > self._stage_bar.n:
            self._stage_bar.update(desired - self._stage_bar.n)
        if step:
            label = {"start": "スタート", "page_start": "ページ開始", "page_done": "ページ完了",
                     "document_done": "ドキュメント完了", "progress": "処理中", "document": "ドキュメント",
                     "folder": "フォルダー", "done": "完了", "complete": "完了", "batch_done": "バッチ完了",
                     "failed": "エラー", "page_failed": "ページエラー", "clean": "チェック済み",
                     "repaired": "リペア済み", "review": "レビュー", "written": "保存済み",
                     "unchanged": "変更なし", "indexed": "インデックス更新済み", "deleted": "削除済み"}
            heading, separator, detail = step.partition(": ")
            first, space, rest = heading.partition(" ")
            step = label.get(first, first) + space + rest + separator + detail
            self._stage_bar.set_postfix_str(step[:80], refresh=False)
        self._stage_bar.refresh()

    def _finish(self, key: tuple[str, str], event: dict[str, Any], *, status: str = "done") -> None:
        phase = self._phases.get(key)
        if phase is None or phase.reported:
            return
        if phase.started is None:
            phase.started = time.monotonic()
        if status == "done" and phase.total is not None:
            phase.current = max(phase.current, phase.total)
        elapsed = time.monotonic() - phase.started
        phase.reported = True
        if self._active_key == key:
            self._update_bar(phase, step=status)
            self._close_visible()
        label = {"done": "完了", "failed": "エラー", "review": "レビュー", "pending": "保留", "cancelled": "キャンセル"}.get(status, status)
        self._write(f"[{key[0].replace('<batch>', '<バッチ>')}] {phase.label}: {elapsed:.1f}秒 ({label})")

    def on_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if str(event.get("stage") or "") == "queue-claim":
            self.add_documents(event.get("paths", []))
            return
        phase_name = self._phase_name(event)
        if phase_name is None:
            return
        if phase_name == "fast-repair":
            self._on_repair_event(event)
            return
        document = self._document(event)
        key = (document, phase_name)
        step = str(event.get("step") or "")
        previous = self._phases.get(key)
        if previous is not None and previous.reported and step in {"done", "complete", "batch_done"}:
            return
        total = event.get("total")
        total = total if isinstance(total, int) and total > 0 else None

        if step in {"start", "page_start"}:
            phase = self._ensure_phase(key, total=total)
            if step == "page_start" and phase.total is not None:
                phase.total = max(phase.total, total or phase.total)
            self._update_bar(phase, step=step)
            return

        phase = self._ensure_phase(key, total=total)
        if step in {"page_done", "document_done"}:
            phase.completed += 1
            phase.current = max(phase.current, phase.completed)
            if total is not None:
                phase.total = total
            detail = step
            if event.get("page"):
                detail += f": {shorten_name(str(event['page']), DOC_WIDTH)}"
            if event.get("status"):
                status = str(event["status"])
                status = {"written": "保存済み", "unchanged": "変更なし", "indexed": "インデックス更新済み",
                          "deleted": "削除済み", "skipped": "スキップ", "failed": "エラー"}.get(status, status)
                detail += f" ({status})"
            self._update_bar(phase, step=detail)
            if phase.total is not None and phase.completed >= phase.total:
                self._finish(key, event)
            return

        current = event.get("current")
        if isinstance(current, int):
            phase.current = max(phase.current, current)
        self._update_bar(phase, step=step)
        if step in {"done", "complete", "batch_done"}:
            self._finish(key, event)
        elif step in {"failed", "page_failed"}:
            self._finish(key, event, status="failed")

    def _on_repair_event(self, event: dict[str, Any]) -> None:
        """Map concurrent page repair events onto one completion bar per document."""

        document = self._document(event)
        key = (document, "fast-repair")
        step = str(event.get("step") or "")
        if step == "document_started":
            pages = event.get("pages")
            total = pages if isinstance(pages, int) and pages > 0 else None
            phase = self._ensure_phase(key, total=total)
            self._update_bar(phase, step=f"{pages or 0}ページをスタート")
            return
        if step == "document_done":
            status = str(event.get("status") or "done")
            self._finish(key, event, status="done" if status == "repaired" else status)
            return

        phase = self._ensure_phase(key)
        page = shorten_name(str(event.get("page") or ""), DOC_WIDTH)
        if step in {"page_clean", "page_repaired", "page_review"}:
            phase.completed += 1
            phase.current = max(phase.current, phase.completed)
            label = step.removeprefix("page_")
            if step == "page_review" and int(event.get("selected_attempt") or 0) > 0:
                label += (
                    f" (ベストトライ {int(event['selected_attempt'])}, "
                    f"スコア {int(event.get('selected_score') or 0)})"
                )
            self._update_bar(phase, step=f"{label}: {page}")
            if phase.total is not None and phase.completed >= phase.total:
                self._finish(key, event)
            return

        attempt = event.get("attempt")
        detail = step
        if step == "candidate_judged":
            detail = (
                f"チェックスコア {int(event.get('score') or 0)}"
                f"、問題 {int(event.get('issues') or 0)}件"
            )
        elif step == "candidate_rejected":
            detail = f"リジェクト ({str(event.get('reason') or '不明')})"
        if page:
            detail += f": {page}"
        if isinstance(attempt, int):
            detail += f" (トライ {attempt})"
        self._update_bar(phase, step=detail)

    def close(self) -> None:
        self._close_visible()
        if self._doc_bar is not None:
            self._doc_bar.close()
        if self._documents:
            self._write(
                f"ドキュメント: {len(self._completed)}/{len(self._documents)}件を処理"
            )
