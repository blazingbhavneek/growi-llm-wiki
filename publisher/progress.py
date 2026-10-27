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


NAME_THRESHOLD = 48
DOC_WIDTH = 40


def _cells(value: str) -> int:
    """Terminal columns a string occupies, counting CJK as two."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def shorten_name(value: str, threshold: int = NAME_THRESHOLD) -> str:
    """Shorten terminal labels by display width, preserving most of the name's start."""
    if _cells(value) <= threshold:
        return value
    available = max(0, threshold - 1)  # one column for the ellipsis
    tail_budget = available // 3
    head_budget = available - tail_budget
    head, tail = "", ""
    for char in value:
        if _cells(head + char) > head_budget:
            break
        head += char
    for char in reversed(value):
        if _cells(char + tail) > tail_budget:
            break
        tail = char + tail
    return f"{head}…{tail}"


def _elapsed_text(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    if not minutes:
        return f"{seconds}秒"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}分{seconds:02d}秒"
    return f"{hours}時間{minutes:02d}分{seconds:02d}秒"


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
        # A caller may supply a complete Japanese explanation while retaining the
        # original exception for debug.log. Otherwise add the reason only once.
        if exception is not None and getattr(record, "live_exception", True):
            reason = f"{type(exception).__name__}: {exception}"
            if reason not in text:
                text = f"{text} エラー={reason}"
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
    unit: str = "件"
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
        "capture": "GROWIの編集内容の取得",
        "parse": "文書の変換",
        "planner": "ページ構成の作成",
        "research": "関連情報の収集",
        "writer": "Wikiの生成・確認",
        "writer-save": "Wikiの保存",
        "fast-repair": "Wikiの修正・確認",
        "linker-entities": "リンク対象の抽出",
        "linker-edges": "ページ間リンクの作成",
        "growi-preflight": "公開前の確認",
        "growi-publish": "GROWIへの公開",
        "reset": "リモートページのリセット",
        "reset-index": "インデックスのリセット",
        "index": "検索インデックスの更新",
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
                desc="文書",
                unit="件",
                position=0,
                leave=True,
                dynamic_ncols=True,
                bar_format="{desc}を確認中 [{elapsed}]{postfix}",
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
            if self._documents:
                self._doc_bar.bar_format = "{desc}: {n_fmt}/{total_fmt}件 |{bar}| [{elapsed}]{postfix}"
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
        return str(value)

    @staticmethod
    def _document_label(document: str, *, terminal: bool = False) -> str:
        if document == "<batch>":
            return "全体"
        # Full paths identify log records and phase state. Only terminal labels shrink.
        return shorten_name(Path(document).name) if terminal else document

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
            phase = _Phase(self._LABELS[key[1]],
                           unit="ページ" if key[1] in {"writer", "writer-save", "fast-repair", "research", "planner"} else "件")
            self._phases[key] = phase
        if phase.started is None:
            phase.started = time.monotonic()
            log.info("[%s] %sを開始します", self._document_label(key[0]), phase.label)
        if total is not None and total > 0:
            phase.total = total
        if self._active_key != key:
            self._close_visible()
            self._active_key = key
            if self._doc_bar is not None:
                self._doc_bar.set_postfix_str(self._document_label(key[0], terminal=True))
            if tqdm is not None:
                self._stage_bar = tqdm(
                    total=phase.total,
                    desc=phase.label,
                    unit=phase.unit,
                    position=1,
                    leave=False,
                    dynamic_ncols=True,
                    bar_format=self._stage_format(phase),
                    disable=not self._bars_enabled,
                    file=self._progress_stream,
                )
        elif self._stage_bar is not None and phase.total != self._stage_bar.total:
            self._stage_bar.total = phase.total
            self._stage_bar.bar_format = self._stage_format(phase)
            self._stage_bar.refresh()
        return phase

    @staticmethod
    def _stage_format(phase: _Phase) -> str:
        if phase.total is None:
            return "{desc} [{elapsed}]{postfix}"
        return "{desc}: {n_fmt}/{total_fmt}{unit} |{bar}| [{elapsed}]{postfix}"

    def _update_bar(self, phase: _Phase, *, step: str = "") -> None:
        if self._stage_bar is None:
            return
        desired = max(self._stage_bar.n, phase.current)
        if desired > self._stage_bar.n:
            self._stage_bar.update(desired - self._stage_bar.n)
        if step:
            label = {"start": "開始", "page_start": "処理中", "page_done": "ページ完了",
                     "page_judged": "内容を確認済み", "resumed": "保存済みの結果を再利用",
                     "page_started": "ページを確認中", "rewrite_started": "修正中",
                     "page_checkpointed": "結果を保存済み", "page_resumed": "保存済みの結果を再利用",
                     "judge_pass_started": "内容を確認中", "writer_pass_started": "修正中",
                     "chunks": "本文を分割中", "entities": "リンク対象を抽出中",
                     "edge_target_done": "リンク作成済み", "incremental_scope": "更新対象を確認中",
                     "page_curated": "リンク対象を選択済み",
                     "document_done": "ドキュメント完了", "progress": "処理中", "document": "ドキュメント",
                     "folder": "フォルダー", "done": "完了", "complete": "完了", "batch_done": "バッチ完了",
                     "failed": "エラー", "page_failed": "ページエラー", "clean": "チェック済み",
                     "repaired": "修正済み", "review": "要確認", "written": "保存済み",
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
        label = {"done": "完了", "failed": "エラー", "review": "要確認", "pending": "保留", "cancelled": "キャンセル"}.get(status, status)
        count = f"、{phase.current}/{phase.total}{phase.unit}" if phase.total is not None else ""
        self._write(f"[{self._document_label(key[0])}] {phase.label}: {label}（{elapsed:.1f}秒{count}）")

    def on_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if str(event.get("stage") or "") == "queue-claim":
            self.add_documents(event.get("paths", []))
            return
        if event.get("step") == "waiting":
            document = self._document(event)
            # Wiki heartbeats cover its planner/writer subphases; do not create a
            # new phase or revive a completed one just to show elapsed time.
            active = self._active_key
            if active and active[0] == document:
                phase = self._phases[active]
                elapsed = int(time.monotonic() - phase.started) if phase.started is not None else 0
                elapsed_text = _elapsed_text(elapsed)
                self._update_bar(phase, step=f"処理中（{elapsed_text}経過）")
                count = f"、{phase.current}/{phase.total}{phase.unit}完了" if phase.total is not None else ""
                log.info("[%s] %sを継続中（%s経過%s）",
                         self._document_label(document), phase.label, elapsed_text, count)
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
            detail = step
            if event.get("page"):
                detail += f": {shorten_name(str(event['page']), DOC_WIDTH)}"
            self._update_bar(phase, step=detail)
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
                          "generated": "生成・確認済み", "verbatim": "原文を使用",
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
            self._update_bar(phase, step=f"{pages or 0}ページの確認を開始")
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
                    f" (採用した試行 {int(event['selected_attempt'])}, "
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
            detail = f"候補を不採用 ({str(event.get('reason') or '不明')})"
        if page:
            detail += f": {page}"
        if isinstance(attempt, int):
            detail += f" ({attempt}回目)"
        self._update_bar(phase, step=detail)

    def close(self) -> None:
        self._close_visible()
        if self._doc_bar is not None:
            self._doc_bar.close()
        if self._documents:
            self._write(
                f"文書の処理結果: {len(self._completed)}/{len(self._documents)}件完了"
            )
