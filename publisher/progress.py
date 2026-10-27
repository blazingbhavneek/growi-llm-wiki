"""Terminal progress rendering for the foreground sync command."""

from __future__ import annotations

import copy
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


class JapaneseFormatter(logging.Formatter):
    """Translate application log templates, keeping paths and exception text intact."""

    _MESSAGES = {
        "fast repair project=%s has no eligible documents": "リペア対象のドキュメントはありません: プロジェクト=%s",
        "checking GROWI edits before repair": "リペア前にGROWIの編集をチェックします",
        "repairing document path=%s": "ドキュメントをリペアします: パス=%s",
        "publishing repaired document path=%s": "リペア済みドキュメントをパブリッシュします: パス=%s",
        "fast repair failed for %s": "ファストリペアに失敗: %s",
        "fast repair resume inspection failed": "ファストリペアの再開チェックに失敗",
        "fast repair GROWI preflight failed": "ファストリペアのGROWIチェックに失敗",
        "fast repair project=%s documents=%d page_limit=%s concurrency=%d": "ファストリペア: プロジェクト=%s、ドキュメント=%d、ページ上限=%s、並列数=%d",
        "continuing interrupted fast repair versions=%s checkpoints=%d without a preflight GROWI pull; accepted pages will be reused": "中断したファストリペアを再開: バージョン=%s、チェックポイント=%d。GROWIのプルをスキップし、確定ページを再利用します",
        "fast repair progress callback failed step=%s error=%s: %s": "ファストリペアのプログレス更新に失敗: ステップ=%s、エラー=%s: %s",
        "fast repair pending checkpoint failed document=%s page=%s error=%s: %s": "ファストリペアのチェックポイント保存に失敗: ドキュメント=%s、ページ=%s、エラー=%s: %s",
        "fast repair initial judge failed document=%s page=%s error=%s: %s": "ファストリペアの初回チェックに失敗: ドキュメント=%s、ページ=%s、エラー=%s: %s",
        "source discovery failed: %s: %s": "ソースのスキャンに失敗: %s、%s",
        "first pass complete; final retry documents=%d discovery=%d": "初回パス完了。最終リトライ: ドキュメント=%d、スキャンエラー=%d",
        "document attempt %d failed: %s": "ドキュメントの処理に失敗: トライ=%d、パス=%s",
        "publish project=%s allow_unlinked=True; linking skipped": "パブリッシュ: プロジェクト=%s、リンク処理をスキップ",
        "publish completed documents=%s pages=%s indexes=%s failures=%d": "パブリッシュ完了: ドキュメント=%s、ページ=%s、インデックス=%s、エラー=%d",
        "linker first pass complete; final retry documents=%d": "リンク初回パス完了。最終リトライ: ドキュメント=%d",
        "linker document attempt %d failed: %s: %s": "リンク処理に失敗: トライ=%d、ドキュメント=%s、エラー=%s",
        "parse-ahead %s: %.1fs": "先行パース: %s、%.1f秒",
        "plan-ahead %s: %.1fs": "先行プラン: %s、%.1f秒",
        "parse-ahead %s failed; the build will parse it: %s: %s": "先行パースに失敗。ビルド時にリトライ: %s、%s: %s",
        "plan-ahead %s failed; the build will plan it: %s: %s": "先行プランに失敗。ビルド時にリトライ: %s、%s: %s",
        "plan-ahead enqueue %s failed; the build will plan it: %s: %s": "先行プランのキュー登録に失敗。ビルド時にリトライ: %s、%s: %s",
        "plan-ahead disabled for the whole run setting=chat_base_url,chat_api_key,chat_model env=OPENAI_BASE_URL,OPENAI_API_KEY,OPENAI_MODEL_NAME error=%s: %s; the build plans every document itself": "先行プランを全面停止（設定=chat_base_url,chat_api_key,chat_model / 環境変数=OPENAI_BASE_URL,OPENAI_API_KEY,OPENAI_MODEL_NAME）: エラー=%s: %s、ビルド側で全ドキュメントをプランします",
        "link-ahead disabled for the whole run setting=chat_base_url,chat_api_key,chat_model env=OPENAI_BASE_URL,OPENAI_API_KEY,OPENAI_MODEL_NAME error=%s: %s; the linker describes every chunk itself": "先行リンクを全面停止（設定=chat_base_url,chat_api_key,chat_model / 環境変数=OPENAI_BASE_URL,OPENAI_API_KEY,OPENAI_MODEL_NAME）: エラー=%s: %s、リンク側で全チャンクを解析します",
        "link-ahead without Jev roles for the whole run setting=wiki_linker_judge env=WIKI_JEV_BACKEND,WIKI_JEV_BASE_URL,WIKI_JEV_MODEL,WIKI_JEV_TOKENIZER error=%s: %s; the judge runs without the role check": "先行リンクのJevロールチェックを全面スキップ（設定=wiki_linker_judge / 環境変数=WIKI_JEV_BACKEND,WIKI_JEV_BASE_URL,WIKI_JEV_MODEL,WIKI_JEV_TOKENIZER）: エラー=%s: %s、判定はロールチェックなしで実行",
        "link-ahead %s failed; the linker will describe it: %s: %s": "先行リンクに失敗。リンク時にリトライ: %s、%s: %s",
        "link-ahead %s: %d chunks, %d calls, %.1fs": "先行リンク: %s、チャンク=%d、コール=%d、%.1f秒",
        "Jev related document judge failed: %s": "Jevの関連ドキュメントチェックに失敗: %s",
        "index page for %s: %s: %s": "インデックスのエラー: %s、%s: %s",
        "Jev role check failed stage=roles chunk=%s document=%s entities=%d error=%s: %s (entity roles stay the LLM guess, related-page edges are dropped)": "Jevのロールチェックに失敗（エンティティ役割はLLM推定のまま、関連ページエッジは破棄）: ステップ=役割、チャンク=%s、ドキュメント=%s、エンティティ=%d、エラー=%s: %s",
        "LLM tie-break failed stage=tiebreak target=%s candidate=%s error=%s: %s (Jev's no stands, the related-page edge is dropped)": "LLMの同点判定に失敗（Jevの否が優先され関連ページエッジは破棄）: ステップ=同点判定、対象=%s、候補=%s、エラー=%s: %s",
        "Jev page curation failed for %s: %s": "Jevのページチェックに失敗: %s、%s",
        "Jev linker engine unavailable for page curation: %s": "Jevのリンクエンジンを利用できません: %s",
        "Jev edge judge unavailable for %s; using the LLM judge instead: %s": "Jevのリンクチェックを利用できないためLLM判定に切替: %s、%s",
        "fast Jev edge judge failed; all related-page edges for the chunk are dropped chunk=%s leads=%d error=%s: %s": "ファストJevのリンクチェックに失敗（関連ページエッジは保持されません）: チャンク=%s、候補=%d、エラー=%s: %s",
        "Jev linker engine unavailable; using LLM: %s": "Jevのリンクエンジンを利用できないため、LLMを使用します: %s",
        "Jev alias resolution failed for %s: %s": "Jevのエイリアス解決に失敗: %s、%s",
        "Jev primary definer failed for %s: %s": "Jevの定義元チェックに失敗: %s、%s",
        "failed to save failure evidence to %s: %s: %s": "失敗ログの保存に失敗: %s、%s: %s",
        "failed to reset failure summary %s: %s: %s": "失敗ドキュメントの初期化に失敗: %s、%s: %s",
        "failed to remove legacy failure summary %s: %s: %s": "旧失敗ログの削除に失敗: %s、%s: %s",
        "document failed: %s": "失敗ドキュメント: %s",
        "failed documents=%d index errors=%d details=%s": "失敗ドキュメント=%d件、インデックスのエラー=%d件、詳細=%s",
        "processed path=%s status=%s": "処理完了: パス=%s、状態=%s",
        "check %s unreachable url=%s error=%s: %s": "接続チェック失敗: %s、URL=%s、エラー=%s: %s",
        "check %s skipped (not configured)": "接続チェックをスキップ: %s（未設定）",
        "check %s status=%s url=%s": "接続チェック: %s、ステータス=%s、URL=%s",
        "check jev failed: %s": "Jev接続チェック失敗: %s",
        "check jev %s": "Jev接続チェック: %s",
        "data root=%s mount=%s ingest=%s linker=%s": "データ: ルート=%s、マウント=%s、取り込み=%s、リンク=%s",
        "recovered on final retry documents=%s": "最終リトライで復旧: ドキュメント=%s",
        "human sync captured documents=%s": "GROWIの人手編集を収集: ドキュメント=%s",
        "convert done files=%d failed=%d": "コンバート完了: ファイル=%d、エラー=%d",
        "relink done touched=%s": "リンク再処理完了: 更新=%s",
        "growi endpoint or markers changed: full republish %s": "GROWIの設定またはマーカーが変更ため全面リパブリッシュ: %s",
        "run started project=%s run=%s command=%s": "実行開始: プロジェクト=%s、ランID=%s、コマンド=%s",
        "run finished project=%s run=%s command=%s outcome=%s": "実行終了: プロジェクト=%s、ランID=%s、コマンド=%s、結果=%s",
        "command failed command=%s error=%s: %s": "コマンド失敗: %s、エラー=%s: %s",
        "uncaught thread error thread=%s": "スレッドで未捕捉の異常: スレッド=%s",
        # --- recurring sites collected from logs/audit-notes/templates.txt (count >= 2) ---
        "atomic write failed path=%s error=%s: %s (disk full, permissions, or file locked?)": "ファイルの書き込みに失敗: パス=%s、エラー=%s: %s（ディスク容量不足・権限・ファイルロックを確認）",
        "json serialize failed path=%s error=%s: %s": "JSON化に失敗: パス=%s、エラー=%s: %s",
        "json file is absent file=%s reason=%s": "JSONファイルがありません: ファイル=%s、理由=%s",
        "json file could not be read file=%s reason=%s; the default value was used instead": "JSONファイルを読み込めないため既定値を使用: ファイル=%s、理由=%s",
        "json file could not be read file=%s error=%s: %s": "JSONファイルの読み込みに失敗: ファイル=%s、エラー=%s: %s",
        "json file is corrupt, not missing file=%s error=%s: %s": "JSONファイルが破損しています（欠損ではありません）: ファイル=%s、エラー=%s: %s",
        "text file could not be read file=%s reason=%s; the default value was used instead": "テキストファイルを読み込めないため既定値を使用: ファイル=%s、理由=%s",
        "text file could not be read file=%s error=%s: %s": "テキストファイルの読み込みに失敗: ファイル=%s、エラー=%s: %s",
        "workdir create failed path=%s error=%s: %s (the old contents are already deleted: half-deleted state)": "作業ディレクトリの作成に失敗: パス=%s、エラー=%s: %s（旧内容は削除済みのため不完全な状態）",
        "workdir delete failed path=%s error=%s: %s (the directory may be half-deleted)": "作業ディレクトリの削除に失敗: パス=%s、エラー=%s: %s（ディレクトリが半分削除されている可能性があります）",
        "temp file left behind path=%s temp=%s error=%s: %s (the failed write below is the real error)": "一時ファイルが残りました: パス=%s、一時=%s、エラー=%s: %s（後続の書き込み失敗が本来のエラーです）",
        "artifact write failed path=%s error=%s: %s": "アーティファクトの書き込みに失敗: パス=%s、エラー=%s: %s",
        "wiki page scan failed path=%s error=%s: %s (permission, unreadable mount, or too many open files?)": "Wikiページの走査に失敗: パス=%s、エラー=%s: %s（権限・マウント・FD上限を確認）",
        "stored excel story plan has an unexpected shape run=%s file=%s error=%s: %s": "保存済みExcelストーリー計画の形式が不正: ランID=%s、ファイル=%s、エラー=%s: %s",
        "GROWI page marker seed could not be read document=%s file=%s error=%s: %s": "GROWIページのマーカー初期値を読み込めません: ドキュメント=%s、ファイル=%s、エラー=%s: %s",
        "embedder unavailable, vector candidate search is off document=%s model=%s backend=%s endpoint=%s settings=WIKI_EMBED_BACKEND,embed_model,embed_api_key error=%s: %s": "エンベディングを利用できないためベクター検索を停止: ドキュメント=%s、モデル=%s、バックエンド=%s、エンドポイント=%s、設定=WIKI_EMBED_BACKEND,embed_model,embed_api_key、エラー=%s: %s",
        "run=%s embedder disabled (WIKI_EMBED_BACKEND=off)": "ランID=%s エンベディング無効（WIKI_EMBED_BACKEND=off）",
        "run=%s stage=embedder error=%s: %s": "ランID=%s ステップ=エンベディング エラー=%s: %s",
        "run=%s stage=index error=%s": "ランID=%s ステップ=インデックス エラー=%s",
        "run=%s stage=index error=%s: %s": "ランID=%s ステップ=インデックス エラー=%s: %s",
        "chunk metadata call failed after retry stage=describe chunk=%s document=%s page=%s error=%s: %s (the chunk keeps a degraded 300-character summary)": "チャンク要約のモデル呼び出しがリトライ後も失敗（300文字の簡易要約のまま）: ステップ=チャンク解析、チャンク=%s、ドキュメント=%s、ページ=%s、エラー=%s: %s",
        "describe prompt artifact could not be written stage=describe chunk=%s document=%s path=%s error=%s: %s (an optional artifact aborts the whole describe pass)": "チャンク解析のプロンプト保存に失敗（任意アーティファクトの原因で全チャンク処理が中断）: ステップ=チャンク解析、チャンク=%s、ドキュメント=%s、パス=%s、エラー=%s: %s",
        "describe result artifact could not be written stage=describe chunk=%s path=%s error=%s: %s (an optional artifact aborts the whole describe pass)": "チャンク解析の結果保存に失敗（任意アーティファクトの原因で全チャンク処理が中断）: ステップ=チャンク解析、チャンク=%s、パス=%s、エラー=%s: %s",
        "page reference state could not be written document=%s path=%s error=%s: %s (the next run re-plans every page of this document)": "ページの参照状態を保存できません（次回は全ページを再プラン）: ドキュメント=%s、パス=%s、エラー=%s: %s",
        "wiki plan has no page list file=%s key=%s document=%s error=%s: %s": "Wikiプランにページ一覧がありません: ファイル=%s、キー=%s、ドキュメント=%s、エラー=%s: %s",
        "up-to-date stamp is unreadable file=%s document=%s error=%s: %s; the document is rebuilt": "最新化スタンプを読み込めないため再構築します: ファイル=%s、ドキュメント=%s、エラー=%s: %s",
    }
    _STAGES = {
        "parser": "パーサー", "parse": "パース", "index": "インデックス", "generate": "ドキュメント生成",
        "publish": "パブリッシュ", "preflight": "事前チェック", "delete": "削除",
        "embedder": "エンベディング", "embeddings": "エンベディング", "link": "リンク", "links": "リンク",
        "operation": "処理", "recovery": "リカバリー", "describe": "チャンク解析", "chunk": "チャンク",
        "snapshot": "スナップショット", "llm": "LLM", "verify": "検証", "tiebreak": "同点判定",
        "sync": "同期", "semantic": "意味解析", "seed": "初期値", "screen": "スクリーニング",
        "convert": "コンバート", "repair": "リペア", "export": "エクスポート", "plan": "プラン",
        "judge": "チェック", "checkpoint": "チェックポイント", "jev": "Jev", "storage": "ストレージ",
        "human": "人手編集", "mount": "マウント", "scan": "スキャン", "queue": "キュー",
        "watch": "監視", "hierarchy": "階層化", "compile": "コンパイル", "region": "領域分割",
        "window": "ウィンドウ", "intro": "序論生成", "writer": "本文生成", "research": "調査",
    }
    # Field labels for templates that have no exact _MESSAGES entry, so live.log keeps
    # Japanese field names with the original reason text instead of raw English.
    _KEYS = {
        "run": "ランID", "project": "プロジェクト", "document": "ドキュメント", "documents": "ドキュメント数",
        "page": "ページ", "pages": "ページ数", "path": "パス", "file": "ファイル", "error": "エラー",
        "last_error": "最終エラー", "reason": "理由", "setting": "設定", "settings": "設定", "value": "値",
        "model": "モデル", "backend": "バックエンド", "endpoint": "エンドポイント", "url": "URL",
        "attempt": "トライ", "attempts": "トライ数", "retries": "リトライ数", "elapsed": "経過時間",
        "timeout": "タイムアウト", "chunk": "チャンク", "chunks": "チャンク数", "sheet": "シート",
        "workbook": "ワークブック", "section": "セクション", "window": "ウィンドウ", "region": "領域",
        "state": "状態", "schema": "スキーマ", "mode": "モード", "kind": "種別", "event": "イベント",
        "command": "コマンド", "binary": "コマンド", "repo": "リポジトリ", "revision": "リビジョン",
        "device": "デバイス", "dtype": "データ型", "batch": "バッチ", "status": "ステータス",
        "question": "質問", "questions": "質問数", "request": "リクエスト", "requests": "リクエスト数",
        "thread": "スレッド", "version": "バージョン", "run_id": "ランID", "step": "ステップ",
        "lines": "行数", "line": "行", "source_lines": "元行数", "old_lines": "旧行数", "new_lines": "新行数",
        "characters": "文字数", "bytes": "バイト数", "size": "サイズ", "count": "件数", "dir": "ディレクトリ",
        "folder": "フォルダー", "mount": "マウント", "target": "対象", "source": "ソース", "key": "キー",
        "policy": "ポリシー", "stage": "ステップ", "threshold": "しきい値", "sample": "サンプル数",
        "env": "環境変数", "entities": "エンティティ数", "candidates": "候補数", "leads": "候補数",
        "temp": "一時ファイル", "encoding": "エンコーディング", "encodings": "エンコーディング", "code": "終了コード",
        "returncode": "終了コード", "root": "ルート", "operation": "オペレーションID", "out": "出力先",
        "stderr": "標準エラー", "failures": "失敗件数", "errors": "エラー件数", "expected": "期待値",
        "edges": "エッジ数", "config": "設定ファイル", "boundary": "境界", "team": "チーム",
        "phase": "フェーズ", "name": "名前", "missing": "欠損", "commit": "コミット",
        "touched": "更新件数", "tier": "ティア", "sections": "セクション数", "score": "スコア",
        "scope": "スコープ", "data_root": "データルート", "action": "処置", "target_name": "プロジェクト名",
        "response": "応答", "output": "出力", "files": "ファイル数", "dest": "保存先",
        "slides": "スライド数", "valid": "有効値", "found": "検出数", "first": "最初の1件",
        "max_batch_requests": "最大バッチ数", "max_batch_tokens": "最大バッチトークン",
        "batch_wait_ms": "バッチ待機ms", "concurrency": "並列数", "record": "記録",
    }
    _KEY_RE = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(sorted(_KEYS, key=len, reverse=True)) + r")="
    )

    def format(self, record: logging.LogRecord) -> str:
        localized = self._localize(record)
        return logging.Formatter("%(levelname)s %(message)s").format(localized)

    def _localize(self, record: logging.LogRecord) -> logging.LogRecord:
        """Return a translated COPY of the record; never touch the shared original."""
        localized = copy.copy(record)
        # live.log must stay traceback-free even when the debug handler already
        # formatted (and cached) the exception for this same record.
        localized.exc_info = None
        localized.exc_text = None
        localized.stack_info = None
        template = str(record.msg)
        if template in self._MESSAGES:
            template = self._MESSAGES[template]
        else:
            for stage, label in self._STAGES.items():
                template = template.replace(f"stage={stage}", f"ステップ={label}")
            template = self._KEY_RE.sub(lambda m: self._KEYS[m.group(1)] + "=", template)
            # only tokens are reworded here; prose stays verbatim so the reason is never mangled
            template = template.replace("retry_later", "後でリトライ").replace("not found", "見つかりません")
            if template.isascii():
                template = "ログ詳細（原文）: " + template
        localized.msg = template
        localized.levelname = {"INFO": "インフォ", "WARNING": "ワーニング", "ERROR": "エラー",
                               "CRITICAL": "重大エラー", "DEBUG": "デバッグ"}.get(record.levelname, record.levelname)
        return localized


class LiveJapaneseFormatter(JapaneseFormatter):
    """One timestamped Japanese line per live record; full traces stay in debug.log.

    Never emits a traceback, ANSI escape or carriage return, so ``live.log`` stays a
    staff-readable single-line record whatever the debug handler does with the same record.
    """

    _ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")

    def __init__(self, *, run_id: str = "", redact: Any = None) -> None:
        super().__init__("%(message)s")
        self.run_id = run_id
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        localized = self._localize(record)
        text = localized.getMessage()
        exception = record.exc_info[1] if record.exc_info and record.exc_info[1] is not None else None
        if exception is not None:
            text = f"{text} error={type(exception).__name__}: {exception}"
        text = self._ANSI.sub("", text)
        if self.redact is not None:
            text = self.redact(text)
        stamp = datetime.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        fields = [stamp, localized.levelname]
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
