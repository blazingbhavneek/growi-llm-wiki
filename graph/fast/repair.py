"""Concurrent, page-local repair pass for already generated fast wikis.

Each page is one asynchronous state machine (judge -> writer -> judge).  Model
work is concurrent, but the coordinator commits completed pages one at a time.
Images are deliberately opaque in this pass: prompts contain stable tags and
accepted drafts get the existing generated image bytes restored unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.policy import policy_of
from common.storage import read_json, sha256_text, write_json_atomic, write_text_atomic
from graph.common.async_tools import run_async_blocking
from graph.common.images import find_images, replace_images_preserving_lines
from graph.common.markdown import scan_markdown_fences
from graph.formats import is_tabular, kind_of
from graph.wiki.storage import normalize_source, split_source_lines
from .model import FAST_WRITER_MAX_OUTPUT_TOKENS
from .passes import bounded_as_completed
from .wiki import (
    append_low_coverage_source,
    generation_prompt_rules,
    mask_math,
    recent_writer_context,
)


VERSION = "wiki-repair-ja-7"
MAX_REWRITE_ATTEMPTS = 4
JUDGE_CALL_ATTEMPTS = 2
REVIEW_START = "<!-- fast-repair:start -->"
REVIEW_END = "<!-- fast-repair:end -->"

_H1_RE = re.compile(r"^# (?P<title>[^\n]+)(?:\n|$)")
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)
_WIKI_IMAGE_RE = re.compile(r"\[\[WIKI_IMAGE_\d+\]\]")
_SOURCE_IMAGE_RE = re.compile(r"\[\[SOURCE_IMAGE_\d+\]\]")
_PROTECTED_IMAGE_RE = re.compile(
    r"\[\[(?:WIKI_IMAGE_\d+|NEO-IMAGE:[A-Za-z0-9_-]+)\]\]"
)
log = logging.getLogger(__name__)


def _is_tabular_document(raw_rel: str) -> bool:
    return is_tabular(kind_of(raw_rel))


class RepairJudgeResult(BaseModel):
    """One strict verdict.  Empty issue lists alone do not imply approval."""

    approved: bool = Field(
        default=False,
        description="True only when the current candidate has no factual loss, factual addition, semantic change, or Japanese-quality defect.",
    )
    coverage_score: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Percentage of source-backed information retained, including question/request intent and uncertainty; never score document structure, numbering, or layout.",
    )
    title_defects: list[str] = Field(
        default_factory=list,
        description="Concrete defects in the current H1 only; name the unsupported, unclear, non-Japanese, or overlong wording.",
    )
    body_defects: list[str] = Field(
        default_factory=list,
        description="Concrete additions, semantic changes, Japanese defects, repetitions, or malformed content that exists in the current candidate.",
    )
    missing_important_information: list[str] = Field(
        default_factory=list,
        description="Each source-backed fact, question/request intent, recipient, uncertainty, full technical name, qualifier, list entry, table meaning, legend, or caption missing from the current candidate.",
    )
    notes: str = Field(
        default="",
        description="Brief current-candidate-only rationale; do not carry over defects seen only in an earlier discarded draft.",
    )


@dataclass(frozen=True)
class _ImageSlot:
    tag: str
    raw: str


@dataclass(frozen=True)
class _PageShell:
    title: str
    body: str
    footer: str


@dataclass
class _Artifact:
    relative: str
    value: str | dict[str, Any]


@dataclass
class _PageInput:
    document: str
    number: int
    filename: str
    owner_ranges: list[tuple[int, int]]
    reference_ranges: list[tuple[int, int]]
    original_text: str
    shell: _PageShell
    masked_body: str
    images: list[_ImageSlot]
    owner_evidence: str
    reference_evidence: str
    allowed_source: str
    context: str
    page_path: Path
    state_path: Path
    state: dict[str, Any]
    guidance: list[str] = field(default_factory=list)  # what human editors want this page to look like
    has_initial_candidate: bool = True


@dataclass
class _PageOutcome:
    page: _PageInput
    accepted: bool
    status: str
    title: str
    masked_body: str
    final_text: str
    attempts: int
    judge_score: int | None
    selected_attempt: int = 0
    selection: str = "original"
    defects: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    artifacts: list[_Artifact] = field(default_factory=list)


@dataclass
class _RepairSession:
    """Mutable state for one page while document-level rounds are running."""

    page: _PageInput
    artifacts: list[_Artifact] = field(default_factory=list)
    title: str = ""
    body: str = ""
    feedback: list[str] = field(default_factory=list)
    feedback_history: list[str] = field(default_factory=list)
    best_title: str = ""
    best_body: str = ""
    best_final: str = ""
    best_verdict: RepairJudgeResult | None = None
    best_feedback: list[str] = field(default_factory=list)
    best_attempt: int = 0
    best_rank: tuple[int, int, int] = (-1, -10**9, -1)
    attempts: int = 0
    pending_title: str | None = None
    pending_body: str | None = None


def _new_repair_session(page: _PageInput) -> _RepairSession:
    return _RepairSession(
        page=page,
        title=page.shell.title,
        body=page.masked_body,
        best_title=page.shell.title,
        best_body=page.masked_body,
        best_final=page.original_text,
    )


def _emit(callback: Any, step: str, **details: Any) -> None:
    if callback:
        try:
            callback({"stage": "fast-repair", "step": step, **details})
        except Exception:
            # Progress reporting is advisory.  A broken logger/UI callback
            # must not turn a page-local repair into a pipeline failure.
            log.exception("fast repair progress callback failed: %s", step)


def _valid_ranges(value: Any, line_count: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"invalid source range: {item!r}")
        start, end = int(item[0]), int(item[1])
        if not 1 <= start <= end <= line_count:
            raise ValueError(f"source range outside document: {start}-{end}")
        result.append((start, end))
    return sorted(set(result))


def _slice_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    return "\n\n".join("\n".join(lines[start - 1:end]) for start, end in ranges)


def _numbered_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    rows: list[str] = []
    for start, end in ranges:
        rows.append(f"--- {start}-{end}行 ---")
        rows.extend(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
    return "\n".join(rows) if rows else "（なし）"


def _mask_source_images(text: str) -> str:
    tags = {
        image.start: f"[[SOURCE_IMAGE_{index:04d}]]"
        for index, image in enumerate(find_images(text), start=1)
    }
    return replace_images_preserving_lines(text, lambda image: tags[image.start])


def _mask_page_images(text: str) -> tuple[str, list[_ImageSlot]]:
    images = find_images(text)
    slots = [
        _ImageSlot(f"[[WIKI_IMAGE_{index:03d}]]", image.raw)
        for index, image in enumerate(images, start=1)
    ]
    masked = text
    for image, slot in reversed(list(zip(images, slots))):
        masked = masked[:image.start] + slot.tag + masked[image.end:]
    return masked, slots


def _restore_page_images(text: str, slots: Sequence[_ImageSlot]) -> str:
    result = text
    expected = {slot.tag for slot in slots}
    found = set(_WIKI_IMAGE_RE.findall(result))
    if found != expected:
        raise ValueError(
            "image tags changed: expected " + ", ".join(sorted(expected))
            + "; got " + ", ".join(sorted(found))
        )
    for slot in slots:
        if result.count(slot.tag) != 1:
            raise ValueError(f"{slot.tag} must occur exactly once")
    by_tag = {slot.tag: slot.raw for slot in slots}
    return _WIKI_IMAGE_RE.sub(lambda match: by_tag[match.group(0)], result)


def _navigation_footer_start(text: str) -> int | None:
    """Locate only the generated final navigation footer, never an earlier rule."""

    lines = text.splitlines(keepends=True)
    nonblank = [index for index, line in enumerate(lines) if line.strip()]
    if len(nonblank) < 2:
        return None
    nav_index = nonblank[-1]
    parts = [part.strip() for part in lines[nav_index].strip().split("｜")]
    if not parts or any(
        not re.match(r"^(?:親|前のページ|次のページ):\s*\[[^\]\n]+\]\([^\n]+\)$", part)
        for part in parts
    ):
        return None
    rule_index = nonblank[-2]
    if lines[rule_index].strip() != "---":
        return None
    return sum(len(line) for line in lines[:rule_index])


def _split_page(text: str) -> _PageShell:
    match = _H1_RE.match(text)
    if not match:
        raise ValueError("generated page must start with exactly one H1")
    footer_start = _navigation_footer_start(text)
    footer = text[footer_start:] if footer_start is not None else ""
    body_end = footer_start if footer_start is not None else len(text)
    body = text[match.end():body_end].strip()
    return _PageShell(match.group("title").strip(), body, footer)


def _assemble_page(title: str, body: str, footer: str) -> str:
    page = f"# {title.strip()}\n\n{body.strip()}\n"
    if footer:
        page = page.rstrip() + "\n\n" + footer.lstrip("\r\n")
    return page if page.endswith("\n") else page + "\n"


def _normalize_navigation_spacing(text: str) -> str:
    """Keep an approved body intact while separating its generated footer."""

    start = _navigation_footer_start(text)
    if start is None:
        return text
    return text[:start].rstrip() + "\n\n" + text[start:]


def _clean_writer_output(raw: str) -> tuple[str, str]:
    text = _THINK_RE.sub("", raw or "").strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].strip() in {"```", "```md", "```markdown"} and lines[-1].strip() == "```":
        lines = lines[1:-1]
    text = "\n".join(line.rstrip() for line in lines).strip()
    match = _H1_RE.match(text + ("\n" if "\n" not in text else ""))
    if not match:
        raise ValueError("output must start with one '# title' line")
    body = text[match.end():].strip()
    if any(line.startswith("# ") for line in body.splitlines()):
        raise ValueError("output contains more than one H1")
    # Navigation is pipeline-owned. If the model ignored the instruction and
    # emitted an exact generated-style footer, remove only that terminal block;
    # creation builds the current footer and repair restores the one it split
    # off before prompting.
    footer_start = _navigation_footer_start(text)
    if footer_start is not None:
        body = text[match.end():footer_start].strip()
    return match.group("title").strip(), body


def _render_messages(messages: Sequence[Any]) -> str:
    return "\n\n".join(str(getattr(message, "content", message)) for message in messages)


def _issues(verdict: RepairJudgeResult) -> list[str]:
    return [
        *[f"タイトル: {item.strip()}" for item in verdict.title_defects if item.strip()],
        *[f"本文: {item.strip()}" for item in verdict.body_defects if item.strip()],
        *[f"欠落: {item.strip()}" for item in verdict.missing_important_information if item.strip()],
    ]


def _feedback(verdict: RepairJudgeResult) -> list[str]:
    feedback = _issues(verdict)
    if verdict.notes.strip():
        feedback.append("査読メモ: " + verdict.notes.strip())
    if verdict.coverage_score < 80 and not verdict.missing_important_information:
        feedback.append(
            "情報保持スコアが80未満である。失われた具体的な事実を原文と照合して復元すること。"
        )
    if not feedback and not verdict.approved:
        feedback.append("査読者が未承認にした。原文と照合し、タイトルと本文を全面的に再確認すること。")
    return feedback


def _approved(verdict: RepairJudgeResult) -> bool:
    return verdict.approved and verdict.coverage_score >= 80 and not _issues(verdict)


def _judge_rank(
    verdict: RepairJudgeResult,
    *,
    attempt: int,
) -> tuple[int, int, int]:
    """Rank rewrites by the judge's factual coverage, then reported quality.

    Coverage is deliberately first because losing a source-backed fact is the
    most damaging repair regression. For equal coverage, prefer fewer judge
    findings, then the later draft (which has seen more feedback).
    """

    return (
        int(verdict.coverage_score),
        -len(_issues(verdict)),
        int(attempt),
    )


def _judge_messages(page: _PageInput, title: str, body: str) -> list[Any]:
    schema = json.dumps(RepairJudgeResult.model_json_schema(), ensure_ascii=False)
    has_initial = bool(getattr(page, "has_initial_candidate", True))
    baseline = (
        "（新規Wiki作成のため開始時候補なし）"
        if not has_initial
        else (
            "（現在候補と同一。初回査読なので差分なし）"
            if title == page.shell.title and body == page.masked_body
            else f"# {page.shell.title}\n\n{page.masked_body}"
        )
    )
    return [
        SystemMessage(content=(
            "あなたは原文から作る日本語技術Wikiの品質を確認する厳格な査読者である。文章は書き直さず、"
            "JSONだけを返す。所有範囲と明示された参照範囲以外の事実を正当化してはならない。\n"
            "行番号付き原文は事実確認のための証拠であり、候補Wikiが再現すべき文書構造ではない。"
            "原文にはOCR誤り、重複行、ページ由来のノイズ、無関係な見出しがあり得るので、"
            "それを『欠落』として要求しない。候補にない原文の見出し・節番号・図表番号・"
            "ページ番号・箇条書き番号・空のプレースホルダー・原文の順序は欠陥ではない。\n"
            "開始時Wikiがある場合、それは修復による退行を見つける比較対象であり、事実の根拠ではない。"
            "現在候補が修復開始時Wikiにあった情報を削除・短縮・一般化・改名した場合、その情報が"
            "所有/参照原文で裏付けられるなら必ず欠落または本文欠陥にする。原文で裏付けられない"
            "開始時Wikiの捏造、重複、文字化け、拒否文、話題逸脱は保持を要求しない。"
            "つまり原文だけが事実の権威であり、開始時Wikiは『修復で正しい内容まで壊していないか』を"
            "確認する差分基準である。修復は要約ではない。短くなったことや行数が減ったことを品質向上と"
            "みなさず、原文に裏付けられる異なる事実を一つずつ保持したかで判定する。完全に同じ事実が"
            "OCRで複数回現れても一つの事実として数え、重複を削除したことを欠落にしない。\n"
            "原文の各行は、次の三分類で判定すること。"
            "【必ず守る意味】処理の目的・対象・入力・出力・動作・条件・制約・例外、"
            "固有の製品名/画面名/DB名/項目名/識別子/コード、数値・単位・閾値・日時・式、"
            "または/かつ・含む/除く・以上/未満などの論理、保存/表示などの状態、"
            "表のセルの意味・凡例・注記・○/◎等の記号、説明語を含むキャプション。"
            "これらは自然な文章に言い換えてもよいが、意味・条件・値を変えたり落としたりしない。"
            "【原則として持ち込まない構造】Markdownの見出し記号、章/節/項の番号、図/表/画像の番号、"
            "PDFページ番号・原文行番号・ファイル名、目次のリンク先/ページ番号だけ、単なる箇条書き番号、"
            "ページ区切り・空見出し・空プレースホルダー、原文の表の罫線/HTML属性/行レイアウト、"
            "SOURCE_IMAGEや画像の生HTML、OCRの重複/文字化け/%%等のノイズ、モデル用の内部作業メモ・モデル名。"
            "業務上の質問・確認依頼・提案・未確定事項はノイズではなく保持する情報である。"
            "これらの不在は欠落ではない。順番の番号を消しても、処理順という意味まで消してはいけない。"
            "【境界】『図 1-1 処理フロー』なら数字と接頭辞は不要だが『処理フロー』は意味があるため残す。"
            "『図 1-1』だけなら戻さない。『## 2.4.1 概要』は見出しと番号を戻さず、"
            "その下の仕様上の事実だけを本文にする。『①入力、②検証』は番号を箇条書きへ変えてよいが、"
            "入力と検証の順序・条件は保つ。表は有効なHTML表へ組み替えてよいが、セルの意味・値・凡例の対応は保つ。"
            "rowspan/colspan等がセルの結合関係や意味を表す場合は、同じ対応関係を保ったか確認する。\n"
            "【目次・一覧・索引の境界】リンク、ページ番号、章番号、字下げ、見出し階層そのものは構造である。"
            "しかし各項目に書かれた正式な機能名、モジュール名、処理名、入力名、出力名、画面名、DB名、"
            "対象、条件、短い説明は意味のある情報である。原文に裏付けられる異なる項目を、候補がまとめて"
            "少数の一般名にしたり、末尾の『入力データ』『出力データ』『処理内容』や括弧内の限定語を削ったり"
            "してはいけない。内部Wikiリンクは後で再構築できるためリンク記法の消失は問わないが、リンクの"
            "表示名に含まれる固有の情報まで消した場合は必ず問う。89個の根拠ある項目を29個へ要約するような"
            "削除は、見た目が整っていても重大な欠落である。\n"
            "【名称の完全性】複合した技術名称は一つの事実である。上位概念や短い通称へ置換しない。"
            "たとえば『調整電源(火力)上下限カーブ作成』を『火力』へ縮めると、対象だけが残り機能名が失われる。"
            "括弧内の対象、方向（上/下、入力/出力、送信/受信）、処理種別、連係元/連係先、DB区分、"
            "保存/表示、適用/除外、率/量など、項目同士を区別する部分を一つでも落としたら欠落である。"
            "候補が原文にない長い総称、分類名、見出し、略語展開へ言い換えた場合も、自然な日本語に見えても"
            "捏造または意味の狭窄/拡張として報告する。\n"
            "Wikiは原文の見出し・節番号・図表番号・ページ番号・箇条書き番号・順番・表レイアウトを"
            "再現する成果物ではない。それらが候補にないこと自体を欠陥や欠落として報告しない。"
            "原文の構造を移植するのではなく、候補Wikiの日本語、読みやすさ、自然なWiki構成を評価する。\n"
            "画像はこの作業の対象外である。SOURCE_IMAGE、WIKI_IMAGE、NEO-IMAGEのタグは画像全体の代用品であり、"
            "画像そのものの内容、画像内の文字、タグの個数、対応関係、図番号を評価しない。"
            "ただし画像のそばにある通常の文章・説明的キャプションは画像ではなくWikiの事実である。"
            "説明的な語を含むキャプションを削除した場合は欠落として報告する。数字だけの図表ラベルは要求しない。\n"
            "タイトルも必ず判定する。長過ぎる題名、中国語簡体字や不自然な言語、原文範囲・要再確認などの内部名、"
            "原文と無関係・一般的過ぎる・捏造された題名をtitle_defectsへ報告する。修復開始時タイトルが"
            "原文に裏付けられ適切だったのに、候補が『処理機能一覧』『概要』『関連情報』等の一般的な題名へ"
            "勝手に改名した場合も退行として報告する。\n"
            "本文では、日本語として不自然な文、中国語混入、同じ語や文の不自然な反復、拒否文、話題逸脱、"
            "原文にない説明・数値・単位・略語展開、壊れたMarkdown、装飾用の単独`---`、"
            "内容のないスタブ、候補が勝手に作った見出しで意味を変えた箇所を報告する。"
            "原文と候補を意味単位で照合し、仕様、条件、動作、値、識別子、固有の用語、"
            "単位、閾値、式、エラー値、記号、表の凡例や○/◎などの修飾を確認する。"
            "または/かつ、含む/除く、保存/表示などの論理や修飾情報を変更・消失させた場合は、"
            "文章が自然でも必ずbody_defectsまたはmissingへ報告する。"
            "事実として重要な仕様、条件、動作、値、識別子、質問・依頼の意図や未確定状態の欠落をmissingへ報告する。"
            "原文見出し・節番号・図表番号・画像番号・ページ番号・表レイアウトの欠落は報告しない。"
            "HTMLの書式やOCRの行数は問わないが、行列の対応、意味のある空セル・空行、"
            "セルの意味・ラベル・値・記号の消失や取り違えは問う。"
            "原文にも存在する用語の必要な反復、識別子、エラー値、コード、変換記号は候補だけの欠陥にしない。"
            "見出しを追加すること自体は欠陥ではないが、原文で裏付けられない分類、"
            "ソースの一つの流れを勝手に分断する見出し、または新しい仕様を暗示する見出しは欠陥である。"
            "見出しレベルの飛び、原文と異なる見出し階層、章番号の不一致、見出しのネストだけは欠陥にしない。"
            "空見出し、同じ見出しの不自然な反復、内容を誤分類する見出し、原文にない仕様を主張する見出しだけを"
            "問う。過去の候補や査読で見た欠陥を現在候補へ持ち越さず、提示された現在候補に実在する問題だけを返す。\n"
            "査読は必ず次の順で行う。"
            "(1)タイトルの言語・範囲・長さと、原文にない命名を確認する。"
            "(2)所有/参照原文から、構造ノイズを除いた意味のある事実を列挙する。特に一覧の各固有項目、"
            "表の各ラベル/値/凡例、複合名称、括弧内限定、論理語、条件、方向、入出力を別々に数える。"
            "(3)現在候補から主張を列挙し、各主張が原文にあるか確認する。無根拠な説明、正規化/変換/保存等の"
            "原文にない動作、勝手に作った分類名、範囲の狭窄/拡張を報告する。"
            "(4)原文の意味ある事実を現在候補へ逆照合し、削除、短縮、一般化、名称変更、表セル/一覧項目の統合を報告する。"
            "(5)修復開始時Wikiと現在候補を比較し、開始時にあって原文で裏付けられる情報の退行を報告する。"
            "(6)中国語簡体字、文字化け、不自然な反復、拒否、話題逸脱、Markdown破損を確認する。"
            "(7)見出し/番号/リンク/レイアウトだけの差は無視する。"
            "(8)一つでも無根拠な追加、意味変更、意味ある欠落、固有名称の短縮があればapproved=falseにする。"
            "全ての意味が保たれ、追加事実がなく、日本語品質にも問題がない場合だけapproved=trueにする。"
            "各欠陥は、現在候補の問題語と、原文にある正しい名称/意味または『原文に根拠なし』を具体的に書く。"
            "『忠実でない』『要確認』だけの曖昧な指摘や、現在候補には存在しない過去の欠陥を書かない。"
            "\n\n" + generation_prompt_rules("judge")
        )),
        HumanMessage(content=(
            f"ページ番号: {page.number}\nファイル名（変更禁止）: {page.filename}\n"
            f"現在の候補タイトル: {title}\n\n"
            f"--- 所有する行番号付き原文 ---\n{page.owner_evidence}\n\n"
            f"--- 許可された参照行（存在する場合だけ） ---\n{page.reference_evidence}\n\n"
            f"--- 修復開始時Wiki（退行比較専用。事実の根拠ではない） ---\n"
            f"{baseline}\n\n"
            f"--- Wiki候補本文（ナビゲーション除外済み） ---\n# {title}\n\n{body}\n\n"
            "approvedはタイトルと本文に欠陥も重要欠落もない場合だけtrueにする。"
            "coverage_scoreは事実として重要な情報の保持率。原文の構造や番号の保持率ではない。"
            "画像タグと画像由来の番号は採点対象外。論理語、条件、値、識別子、記号、説明的キャプションの"
            "意味が保たれているかを優先し、見出し・節番号・図表番号の一致は採点しない。"
            "一つでも仕様の意味を反転・弱め・消失させていればapproved=falseにする。"
            "原文に裏付けられる一覧項目や複合名称を削除/統合/一般化した候補を、簡潔という理由で承認しない。"
            "coverage_scoreが高くても、欠落や捏造が一つでもあればapproved=falseかつ具体的な項目名を返す。\n"
            f"JSON Schema: {schema}"
        )),
    ]


def _writer_messages(
    page: _PageInput,
    title: str,
    body: str,
    feedback: Sequence[str],
    *,
    attempt: int,
    earlier_feedback: Sequence[str] = (),
) -> list[Any]:
    image_tags = "、".join(slot.tag for slot in page.images) or "なし"
    has_initial = bool(getattr(page, "has_initial_candidate", True))
    guidance = (
        "--- 人間の編集者の要望（このページの体裁。事実は変えない） ---\n- " + "\n- ".join(page.guidance) + "\n\n"
        if page.guidance else ""
    )
    baseline = (
        "（新規Wiki作成のため開始時候補なし）"
        if not has_initial
        else (
            "（現在候補と同一。まだ修復による差分なし）"
            if title == page.shell.title and body == page.masked_body
            else f"# {page.shell.title}\n\n{page.masked_body}"
        )
    )
    latest_feedback = "\n- ".join(
        item for item in feedback if item.strip()
    ) or "（なし。原文から新規作成する）"
    return [
        SystemMessage(content=(
            "あなたは原文から人間が読みやすい日本語技術Wikiを作成・修復する編集者である。出力はMarkdownだけで、"
            "必ず1行目を`# Wikiタイトル`にし、その後へ本文を書く。JSON、前置き、全体を囲むコードフェンス、"
            "前後/親ナビゲーションは出力しない。\n"
            "Wikiを原文の転記や構造の複製にしない。原文の見出し、節番号、図表番号、ページ番号、箇条書き番号、"
            "順番、表レイアウトはそのまま持ち込まず、内容に合う自然な日本語Wikiの構成に整理する。"
            "節番号・図表番号・画像番号・ページ番号はWikiで不要なら省略する。画像の前後にある番号や見出しを"
            "画像タグから推測・復元しない。\n"
            "これは要約・短縮ではなく、Wikiを人間が読みやすいページへ作成・修復する作業である。現在候補の"
            "正しい事実は保持しつつ、壊れた構造、表、重複、読みにくい並びはページ全体で再構成してよい。"
            "前の試行で直った事実を次の試行で壊さない。短くすること、項目数や行数を"
            "減らすこと、複数項目を総称へまとめることは修復ではない。削除してよいのは、原文と照合して明らかな"
            "重複、OCRノイズ、中国語混入、拒否文、話題逸脱、無根拠な捏造だけである。削除すべきか迷う"
            "原文裏付け済み情報は保持する。\n"
            "所有原文と明示された参照原文にない事実、仕様、条件、数値、単位、正式名称を作らない。"
            "原文を読むときは、意味を持つ情報だけを抽出する。処理の目的・対象・入力・出力・動作、"
            "条件・制約・例外、固有名詞・項目名・識別子・コード、数値・単位・閾値・式、"
            "または/かつ・含む/除く等の論理、保存/表示等の状態、表の凡例・注記・○/◎、"
            "説明的なキャプションは必ず保持する。構造を変えても、これらの意味は変えない。"
            "一方、章/節/項番号、図/表番号、ページ番号、原文行番号、目次のリンク先やページ番号、単なる箇条書き番号、"
            "空見出し、OCR重複/文字化け/%%、ページ区切り、モデル用の内部メモ、モデル名は本文へ移植しない。"
            "業務上の質問・確認依頼・提案・未確定事項は内部メモではなく、意図と確認先を必ず保持する。"
            "番号付き箇条書きは番号を外してよいが、処理順・条件・内容は残す。\n"
            "原文の事実、動作、条件、意味のある値や識別子は落とさない。原文にある見出しや番号を省略しても、"
            "そこに含まれる仕様上の事実は本文に自然に反映する。特に固有の用語、単位、閾値、式、"
            "エラー値、または/かつ等の論理、含む/除く等の条件、保存/表示等の凡例、○/◎などの記号を"
            "自然な言い換えで別の意味に変えない。表は再構成してよいが、セルの意味・ラベル・値・凡例は全て保つ。"
            "機能名、モジュール名、処理名、入力名、出力名、画面名、DB名、項目名は、原文にある完全な名称を"
            "保持する。複合名称を一部だけへ短縮したり、一般的な上位概念へ置き換えたり、括弧内の対象、"
            "入力/出力、上/下、送信/受信、連係元/連係先、率/量、保存/表示など区別に必要な接頭辞・接尾辞を"
            "落とさない。たとえば『調整電源(火力)上下限カーブ作成』を『火力』にしない。"
            "原文にない長い総称や分類名を考案せず、既存の短い語を説明的に見せるため勝手に膨らませない。\n"
            "一覧、索引、目次風のページでも、リンク記法、ページ番号、章番号、字下げは構造として省略できるが、"
            "各項目の表示名に含まれる固有の機能名・処理名・入出力名・短い説明は本文の情報である。"
            "原文で裏付けられる異なる項目は一件ずつ残し、多数の項目を少数の分類へ統合しない。"
            "『入力データ』『出力データ』『処理内容』等が単なる見出しなら形式を変えてよいが、その配下の"
            "項目名と説明は削除しない。内部Wikiリンク自体は再構築可能なのでリンク先を推測して作らない。\n"
            "重複、無限反復、中国語混入、拒否文、空見出し、話題逸脱、壊れたMarkdown、"
            "装飾用の単独`---`を直す。本文は自然な日本語だけで書く。"
            "中国語簡体字が技術用語の中に一字だけ混じる場合は、周辺の名称や表行を削除せず、その字だけを"
            "文脈に合う日本語表記へ直す（值→値、压→圧、单→単、项→項、关→関、变→変、应→応、"
            "查→査、运→運、总→総、发→発、图→図、录→録）。原文にない『正規化した』『変換した』『保存する』"
            "などの動作を、文章を滑らかにする目的で補わない。"
            "タイトルは査読で具体的な欠陥を指摘された場合だけ直す。修復開始時タイトルが原文に裏付けられ"
            "自然ならそのまま保持し、『処理機能一覧』『概要』『関連情報』等の一般的な題名を新しく考案しない。"
            "修正が必要な場合だけ、原文の内容を正確に表す短い自然な日本語へ直す。ファイル名は題名に含めない。\n"
            "画像は理解も修正もしない。提示された保護画像タグは画像そのものであり、一字も変えず各1回だけ残す。"
            "提示されていないSOURCE_IMAGE、WIKI_IMAGE、NEO-IMAGEタグは新しく作らない。"
            "画像の中身を説明しない。ただし画像のそばにある説明的な通常文・キャプションは、"
            "画像タグとは別のWiki本文なので、意味のある語を保つ。図/表番号だけなら省略してよい。\n"
            "必要な表は有効で読みやすいHTML表に再構成する。table/thead/tbody/tr/th/tdを正しく閉じ、"
            "rowspan/colspanの意味、行列の対応、意味のある空セル・空行、表の中の事実、識別子、"
            "実データ値を保つ。OCRが作った重複した全空行は削除してよい。Markdownのパイプ表や"
            "表を囲むだけのコードフェンスは出力しない。"
            "原文と同じ表記・行順・行数・見出し構造を機械的に再現する必要はない。"
            "ただし原文にない見出しで一つの説明を勝手に分断したり、新しい分類・仕様を作ったりしない。"
            "見出しレベルの飛び、原文と異なる見出し階層、章番号の不一致、ネストだけを直すために本文を"
            "書き換えない。空見出し、反復見出し、内容を誤分類する見出しだけを修正する。"
            "査読フィードバックが原文の節番号・図表番号・ページ番号・見出しの復元を求めていても、"
            "それ自体は修復目標ではない。そこに含まれる意味のある事実だけを反映する。"
            "例えば『図 1-1 処理フロー』は『処理フロー』を残し、『図 1-1』は戻さない。"
            "『## 2.4.1 概要』は番号付き見出しを戻さず、概要の事実を自然な段落またはWiki見出しで書く。"
            "画像タグの前後にある説明的キャプションは削除しない。数字だけの図表ラベルは追加しない。"
            "フィードバックに構造の復元と意味の修復が混在している場合は、意味の修復だけを実行する。"
            "出力前に必ず自己検査する。(1)査読で指定された各欠陥を直したか、(2)現在候補の正しい箇所を"
            "保持したか、(3)修復開始時Wikiにあり原文で裏付けられる各固有項目を削除していないか、"
            "(4)原文の複合名称・括弧内限定・入出力・方向・条件・論理語・値・単位・表凡例・○/◎を"
            "一つずつ保持したか、(5)原文にない新しい説明、分類、見出し、動作を加えていないか、"
            "(6)簡体字、文字化け、反復、単独`---`を残していないかを確認してから出力する。"
            "\n\n" + generation_prompt_rules("writer")
        )),
        HumanMessage(content=(
            f"執筆・修復試行: {attempt}/{MAX_REWRITE_ATTEMPTS}\n"
            f"ページ番号: {page.number}\nファイル名（変更禁止）: {page.filename}\n"
            f"保持必須の保護画像タグ: {image_tags}\n\n"
            f"{guidance}"
            f"--- 直前の文書コンテキスト（流れの理解専用。ここだけにある事実は本文へコピーしない） ---\n"
            f"{getattr(page, 'context', '') or '（なし）'}\n\n"
            f"--- 所有する行番号付き原文 ---\n{page.owner_evidence}\n\n"
            f"--- 許可された参照行 ---\n{page.reference_evidence}\n\n"
            f"--- 開始時Wiki（存在する場合のみ退行防止に使用。原文にない内容は根拠にしない） ---\n"
            f"{baseline}\n\n"
            f"--- 直前のWiki候補（空なら原文から新規作成する） ---\n# {title}\n\n{body}\n\n"
            "--- 直前候補に対する最新の査読・画像整合性（全て直す） ---\n- "
            + latest_feedback
            + (
                "\n\n--- それ以前の指摘（退行チェック用。現在候補ですでに直っているなら再編集しない） ---\n- "
                + "\n- ".join(item for item in earlier_feedback if item.strip())
                if any(item.strip() for item in earlier_feedback)
                else ""
            )
            + "\n\n直前の候補を土台に、最新の指摘を直し、ページ全体が自然に読めるよう構成を整える。"
            "修復開始時Wikiまたは現在候補にある原文裏付け済みの別の事実を削除・統合・改名してはならない。"
        )),
    ]


def _image_errors(page: _PageInput, title: str, body: str) -> list[str]:
    """The only deterministic candidate gate: keep opaque image bytes addressable."""

    del title
    errors: list[str] = []
    expected_tags = {slot.tag for slot in page.images}
    found_tags = set(_PROTECTED_IMAGE_RE.findall(body))
    if found_tags != expected_tags:
        errors.append("保護画像タグを追加・削除・改名しないこと。")
    for tag in expected_tags:
        if body.count(tag) != 1:
            errors.append(f"画像タグ {tag} は1回だけ置くこと。")
    if _SOURCE_IMAGE_RE.search(body):
        errors.append("SOURCE_IMAGEタグは原文の目印であり、Wiki本文へコピーしないこと。")
    if find_images(body) or re.search(r"data:image/", body, re.I):
        errors.append("画像マークアップやbase64を新しく書かず、指定された保護画像タグだけを残すこと。")
    return list(dict.fromkeys(errors))


async def _judge(
    page: _PageInput,
    title: str,
    body: str,
    *,
    model: Any,
    stem: str,
    artifacts: list[_Artifact],
) -> tuple[RepairJudgeResult | None, str]:
    messages = _judge_messages(page, title, body)
    artifacts.append(_Artifact(f"{stem}-prompt.md", _render_messages(messages)))
    last_error = ""
    for call in range(1, JUDGE_CALL_ATTEMPTS + 1):
        try:
            try:
                raw = await model.structured(
                    RepairJudgeResult, messages, max_output_tokens=16000, temperature=0.2
                )
            except TypeError:
                raw = await model.structured(RepairJudgeResult, messages)
            result = raw if isinstance(raw, RepairJudgeResult) else RepairJudgeResult.model_validate(raw)
            artifacts.append(_Artifact(f"{stem}-call-{call:02d}.json", result.model_dump(mode="json")))
            return result, ""
        except Exception as exc:  # bounded transport/schema retry
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            artifacts.append(_Artifact(f"{stem}-call-{call:02d}-error.txt", last_error + "\n"))
    return None, last_error


async def _writer(
    page: _PageInput,
    title: str,
    body: str,
    feedback: Sequence[str],
    *,
    model: Any,
    attempt: int,
    earlier_feedback: Sequence[str],
    artifacts: list[_Artifact],
) -> tuple[str, str]:
    messages = _writer_messages(
        page,
        title,
        body,
        feedback,
        attempt=attempt,
        earlier_feedback=earlier_feedback,
    )
    artifacts.append(_Artifact(f"rewrite-{attempt:02d}-prompt.md", _render_messages(messages)))
    try:
        try:
            raw = await model.text(
                messages,
                max_output_tokens=FAST_WRITER_MAX_OUTPUT_TOKENS,
                temperature=0.7,
            )
        except TypeError:
            raw = await model.text(
                messages, max_output_tokens=FAST_WRITER_MAX_OUTPUT_TOKENS
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:1000]
        artifacts.append(_Artifact(f"rewrite-{attempt:02d}-error.txt", error + "\n"))
        raise RuntimeError(error) from exc
    artifacts.append(_Artifact(f"rewrite-{attempt:02d}.md", str(raw)))
    try:
        return _clean_writer_output(str(raw))
    except Exception as exc:
        # Candidate-format errors are local to this writer attempt.  Turn them
        # into the same bounded retry path as a model/transport error instead
        # of aborting every other page in the document.
        error = f"{type(exc).__name__}: {exc}"[:1000]
        artifacts.append(_Artifact(f"rewrite-{attempt:02d}-parse-error.txt", error + "\n"))
        raise RuntimeError(error) from exc


def _page_exception_outcome(
    page: _PageInput,
    exc: Exception,
    *,
    on_progress: Any,
) -> _PageOutcome:
    """Checkpoint an unexpected page-local failure without cancelling siblings."""

    error = f"{type(exc).__name__}: {exc}"[:1000]
    defect = "ページ修復中に例外が発生したため、元のページを保持した: " + error
    _emit(
        on_progress,
        "page_review",
        document=page.document,
        page=page.filename,
        attempts=0,
        error=error,
    )
    return _PageOutcome(
        page,
        False,
        "review",
        page.shell.title,
        page.masked_body,
        page.original_text,
        0,
        None,
        defects=[defect],
        artifacts=[_Artifact("repair-error.txt", error + "\n")],
    )


def _review_session(session: _RepairSession, *, on_progress: Any) -> _PageOutcome:
    """Return the best safe draft after the session exhausts its writer budget."""

    verdict = session.best_verdict or RepairJudgeResult()
    selected_rewrite = session.best_attempt > 0
    final_feedback = (
        session.best_feedback
        or session.feedback
        or _feedback(verdict)
        or [f"{MAX_REWRITE_ATTEMPTS}回の修復で承認されなかった。"]
    )
    _emit(
        on_progress,
        "page_review",
        document=session.page.document,
        page=session.page.filename,
        attempts=session.attempts,
        selected_attempt=session.best_attempt,
        selected_score=verdict.coverage_score,
    )
    return _PageOutcome(
        session.page,
        selected_rewrite,
        "review",
        session.best_title,
        session.best_body,
        session.best_final,
        session.attempts,
        verdict.coverage_score,
        selected_attempt=session.best_attempt,
        selection="best-judged" if selected_rewrite else "original",
        defects=final_feedback,
        missing=list(verdict.missing_important_information),
        artifacts=session.artifacts,
    )


async def _round_initial_judge(
    session: _RepairSession,
    *,
    judge_model: Any,
    on_progress: Any,
) -> _PageOutcome | None:
    """Judge an untouched page; None means it entered the writer queue."""

    page = session.page
    _emit(on_progress, "page_started", document=page.document, page=page.filename)
    verdict, judge_error = await _judge(
        page,
        session.title,
        session.body,
        model=judge_model,
        stem="initial-judge",
        artifacts=session.artifacts,
    )
    if verdict is None:
        defects = ["初回査読を完了できなかった: " + (judge_error or "judge unavailable")]
        _emit(on_progress, "page_review", document=page.document, page=page.filename, attempts=0)
        return _PageOutcome(
            page,
            False,
            "review",
            session.title,
            session.body,
            page.original_text,
            0,
            None,
            defects=defects,
            artifacts=session.artifacts,
        )

    if _approved(verdict):
        _emit(on_progress, "page_clean", document=page.document, page=page.filename)
        return _PageOutcome(
            page,
            True,
            "clean",
            session.title,
            session.body,
            _normalize_navigation_spacing(page.original_text),
            0,
            verdict.coverage_score,
            artifacts=session.artifacts,
        )

    session.feedback = _feedback(verdict)
    session.feedback_history = list(session.feedback)
    session.best_feedback = list(session.feedback)
    session.best_verdict = verdict
    # The original remains the transport/model-failure fallback, but it must
    # not beat every readable rewrite merely because it retained OCR noise.
    session.best_rank = (-1, -10**9, -1)
    return None


async def _round_write(
    session: _RepairSession,
    *,
    writer_model: Any,
    on_progress: Any,
) -> tuple[str, _PageOutcome | None]:
    """Write one queued page, without judging it in the writer phase."""

    page = session.page
    session.attempts += 1
    attempt = session.attempts
    _emit(
        on_progress,
        "rewrite_started",
        document=page.document,
        page=page.filename,
        attempt=attempt,
    )
    try:
        candidate_title, candidate_body = await _writer(
            page,
            session.title,
            session.body,
            session.feedback,
            model=writer_model,
            attempt=attempt,
            earlier_feedback=[
                item for item in session.feedback_history if item not in session.feedback
            ],
            artifacts=session.artifacts,
        )
    except RuntimeError as exc:
        writer_error = str(exc)[:1000]
        session.feedback = [writer_error]
        session.feedback_history.extend(
            item for item in session.feedback if item not in session.feedback_history
        )
        _emit(
            on_progress,
            "candidate_rejected",
            document=page.document,
            page=page.filename,
            attempt=attempt,
            reason="writer-error",
            checks=1,
            error=writer_error,
        )
        return (
            ("done", _review_session(session, on_progress=on_progress))
            if attempt >= MAX_REWRITE_ATTEMPTS
            else ("retry", None)
        )

    errors = _image_errors(page, candidate_title, candidate_body)
    if errors:
        session.feedback = errors
        session.feedback_history.extend(
            item for item in session.feedback if item not in session.feedback_history
        )
        session.title, session.body = candidate_title, candidate_body
        session.artifacts.append(
            _Artifact(f"rewrite-{attempt:02d}-image-integrity.json", {"errors": errors})
        )
        _emit(
            on_progress,
            "candidate_rejected",
            document=page.document,
            page=page.filename,
            attempt=attempt,
            reason="image-integrity",
            checks=len(errors),
        )
        return (
            ("done", _review_session(session, on_progress=on_progress))
            if attempt >= MAX_REWRITE_ATTEMPTS
            else ("retry", None)
        )

    session.pending_title = candidate_title
    session.pending_body = candidate_body
    return "candidate", None


async def _round_candidate_judge(
    session: _RepairSession,
    *,
    judge_model: Any,
    on_progress: Any,
) -> tuple[str, _PageOutcome | None]:
    """Judge one completed writer candidate, without starting its next writer."""

    page = session.page
    candidate_title = session.pending_title
    candidate_body = session.pending_body
    if candidate_title is None or candidate_body is None:
        raise RuntimeError("writer phase completed without a candidate")
    session.pending_title = None
    session.pending_body = None
    # If judging this candidate is unavailable, the next writer still needs
    # to see the latest candidate and its new feedback, matching the old
    # page-local retry behavior.
    session.title, session.body = candidate_title, candidate_body
    latest, judge_error = await _judge(
        page,
        candidate_title,
        candidate_body,
        model=judge_model,
        stem=f"rewrite-{session.attempts:02d}-judge",
        artifacts=session.artifacts,
    )
    if latest is None:
        session.feedback = [
            "査読を完了できなかった: " + (judge_error or "judge unavailable")
        ]
        session.feedback_history.extend(
            item for item in session.feedback if item not in session.feedback_history
        )
        _emit(
            on_progress,
            "candidate_rejected",
            document=page.document,
            page=page.filename,
            attempt=session.attempts,
            reason="judge-unavailable",
            checks=1,
        )
        return (
            ("done", _review_session(session, on_progress=on_progress))
            if session.attempts >= MAX_REWRITE_ATTEMPTS
            else ("retry", None)
        )

    candidate_feedback = _feedback(latest)
    candidate_rank = _judge_rank(latest, attempt=session.attempts)
    published_body = append_low_coverage_source(
        candidate_body, page.allowed_source, latest.coverage_score
    )
    candidate_final = _restore_page_images(
        _assemble_page(candidate_title, published_body, page.shell.footer),
        page.images,
    )
    if candidate_rank > session.best_rank:
        session.best_title = candidate_title
        session.best_body = candidate_body
        session.best_final = candidate_final
        session.best_verdict = latest
        session.best_feedback = list(candidate_feedback)
        session.best_attempt = session.attempts
        session.best_rank = candidate_rank
    _emit(
        on_progress,
        "candidate_judged",
        document=page.document,
        page=page.filename,
        attempt=session.attempts,
        score=latest.coverage_score,
        approved=_approved(latest),
        issues=len(_issues(latest)),
    )
    if _approved(latest):
        _emit(
            on_progress,
            "page_repaired",
            document=page.document,
            page=page.filename,
            attempt=session.attempts,
        )
        return (
            "done",
            _PageOutcome(
                page,
                True,
                "repaired",
                candidate_title,
                candidate_body,
                candidate_final,
                session.attempts,
                latest.coverage_score,
                selected_attempt=session.attempts,
                selection="approved",
                artifacts=session.artifacts,
            ),
        )

    session.feedback = candidate_feedback
    session.feedback_history.extend(
        item for item in session.feedback if item not in session.feedback_history
    )
    return (
        ("done", _review_session(session, on_progress=on_progress))
        if session.attempts >= MAX_REWRITE_ATTEMPTS
        else ("retry", None)
    )


def _artifact_root(state_root: Path, version: str, number: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", version)
    return state_root / "work" / "repair" / safe / f"page-{number:03d}"


def _write_artifacts(root: Path, artifacts: Sequence[_Artifact]) -> None:
    for artifact in artifacts:
        target = root / artifact.relative
        if isinstance(artifact.value, dict):
            write_json_atomic(target, artifact.value)
        else:
            write_text_atomic(target, artifact.value)


def _write_session_checkpoint(
    state_root: Path,
    version: str,
    session: _RepairSession,
    *,
    stage: str,
    model_name: str,
    model_provider: str,
) -> None:
    """Durably record which role must handle this page next."""

    if stage not in {"initial-judge", "writer", "judge"}:
        raise ValueError(f"invalid repair checkpoint stage: {stage}")
    page = session.page
    root = _artifact_root(state_root, version, page.number)
    _write_artifacts(root, session.artifacts)
    write_json_atomic(
        root / "checkpoint.json",
        {
            "schema_version": 1,
            "repair_version": version,
            "page_number": page.number,
            "page_content_sha256": sha256_text(page.original_text),
            "stage": stage,
            "title": session.title,
            "body": session.body,
            "feedback": list(session.feedback),
            "feedback_history": list(session.feedback_history),
            "best_title": session.best_title,
            "best_body": session.best_body,
            "best_verdict": (
                session.best_verdict.model_dump(mode="json")
                if session.best_verdict is not None
                else None
            ),
            "best_feedback": list(session.best_feedback),
            "best_attempt": session.best_attempt,
            "best_rank": list(session.best_rank),
            "attempts": session.attempts,
            "pending_title": session.pending_title,
            "pending_body": session.pending_body,
        },
    )
    state = dict(page.state)
    state.update(
        {
            "repair_version": version,
            "repair_status": {
                "initial-judge": "initial-judge-pending",
                "writer": "rewrite-pending",
                "judge": "judge-pending",
            }[stage],
            "repair_checkpoint_stage": stage,
            "repair_attempts": session.attempts,
            "repair_feedback": list(session.feedback),
            "repair_model": model_name,
            "repair_model_provider": model_provider,
        }
    )
    write_json_atomic(page.state_path, state)
    page.state = state


def _resume_session_checkpoint(
    state_root: Path,
    version: str,
    page: _PageInput,
) -> tuple[str, _RepairSession] | None:
    """Restore a rejected or not-yet-judged candidate without repeating work."""

    status = str(page.state.get("repair_status") or "")
    expected_stage = {
        "initial-judge-pending": "initial-judge",
        "rewrite-pending": "writer",
        "judge-pending": "judge",
    }.get(status)
    if page.state.get("repair_version") != version or expected_stage is None:
        return None
    checkpoint = read_json(
        _artifact_root(state_root, version, page.number) / "checkpoint.json",
        default={},
    )
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("repair_version") != version
        or int(checkpoint.get("page_number") or 0) != page.number
        or checkpoint.get("page_content_sha256") != sha256_text(page.original_text)
        or checkpoint.get("stage") != expected_stage
    ):
        return None

    session = _new_repair_session(page)
    session.title = str(checkpoint.get("title") or page.shell.title)
    session.body = str(checkpoint.get("body") or page.masked_body)
    session.feedback = [str(item) for item in checkpoint.get("feedback") or []]
    session.feedback_history = [
        str(item) for item in checkpoint.get("feedback_history") or []
    ]
    session.best_title = str(checkpoint.get("best_title") or page.shell.title)
    session.best_body = str(checkpoint.get("best_body") or page.masked_body)
    raw_verdict = checkpoint.get("best_verdict")
    session.best_verdict = (
        RepairJudgeResult.model_validate(raw_verdict) if raw_verdict else None
    )
    session.best_feedback = [
        str(item) for item in checkpoint.get("best_feedback") or []
    ]
    session.best_attempt = max(0, int(checkpoint.get("best_attempt") or 0))
    raw_rank = checkpoint.get("best_rank") or []
    if isinstance(raw_rank, list) and len(raw_rank) == 3:
        session.best_rank = tuple(int(item) for item in raw_rank)
    session.attempts = max(0, int(checkpoint.get("attempts") or 0))
    pending_title = checkpoint.get("pending_title")
    pending_body = checkpoint.get("pending_body")
    session.pending_title = str(pending_title) if pending_title is not None else None
    session.pending_body = str(pending_body) if pending_body is not None else None
    if session.best_attempt > 0:
        restored_body = append_low_coverage_source(
            session.best_body,
            page.allowed_source,
            session.best_verdict.coverage_score if session.best_verdict else None,
        )
        session.best_final = _restore_page_images(
            _assemble_page(
                session.best_title, restored_body, page.shell.footer
            ),
            page.images,
        )
    if expected_stage == "judge" and (
        session.pending_title is None or session.pending_body is None
    ):
        return None
    return expected_stage, session


def _replace_link_title(text: str, filename: str, title: str) -> str:
    pattern = re.compile(
        r"(?<!!)\[[^\]\n]*\]\((?P<prefix>\./)?" + re.escape(filename) + r"\)"
    )
    masked, restore = mask_math(text)
    lines = masked.splitlines(keepends=True)
    scan = scan_markdown_fences(lines)
    protected = [
        inside or (index > 0 and scan.inside_after_line[index - 1])
        for index, inside in enumerate(scan.inside_after_line)
    ]
    return restore("".join(
        line if protected[index] else pattern.sub(
            lambda match: f"[{title}]({match.group('prefix') or ''}{filename})",
            line,
        )
        for index, line in enumerate(lines)
    ))


def _update_review(state_root: Path, version: str) -> bool:
    path = state_root / "wiki" / "_review.md"
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    base = re.sub(
        re.escape(REVIEW_START) + r".*?" + re.escape(REVIEW_END) + r"\n?",
        "", previous, flags=re.S,
    ).rstrip()
    failed: list[dict[str, Any]] = []
    for sidecar in sorted((state_root / "state" / "pages").glob("*.json")):
        row = read_json(sidecar, default={})
        if row.get("repair_version") == version and row.get("repair_status") == "review":
            failed.append(row)
    block = ""
    if failed:
        rows = [REVIEW_START, "## Fast repairで確認が必要なページ", ""]
        for row in failed:
            fallback = f"{int(row.get('number', 0)):03d}.md"
            rows.append(f"- `{row.get('filename') or fallback}`")
            for issue in list(row.get("repair_feedback") or [])[:8]:
                rows.append("  - " + " ".join(str(issue).split()))
        rows.extend([REVIEW_END, ""])
        block = "\n".join(rows)
    updated = (base + ("\n\n" if base and block else "") + block).rstrip() + "\n" if base or block else ""
    if updated == previous:
        return False
    if updated:
        write_text_atomic(path, updated)
    else:
        path.unlink(missing_ok=True)
    return True


class FastRepair:
    """One reusable writer/judge pair and a serial document coordinator."""

    def __init__(
        self,
        settings: Any,
        *,
        project: Any | None = None,
        model: Any | None = None,
        on_progress: Any = None,
        resume_page_states: Sequence[str] | None = None,
    ) -> None:
        from graph.workspace.project import open_project
        from graph.workspace.writer import wiki_config
        from graph.wiki.model import judge_model, writer_model

        self.settings = settings
        self.project = project or open_project(settings)
        self.policy = policy_of(settings)
        if self.policy.name != "fast":
            raise ValueError("--repair requires the fast policy")
        self.version = self.policy.cache_key(VERSION)
        self.resume_page_states = frozenset(resume_page_states or ())
        self.on_progress = on_progress
        self.concurrency = max(
            1, int(getattr(settings, "wiki_rewrite_concurrency", getattr(settings, "concurrency", 1)))
        )
        config = wiki_config(
            settings,
            run_dir=Path(self.project.metadata) / "cache" / "fast-repair" / "model",
        )
        self.model = model or self.policy.model_port(config)
        self.writer_model = writer_model(self.model)
        self.judge_model = judge_model(self.model)
        self.model_name = str(getattr(self.judge_model, "name", "") or config.chat_model)
        self.model_provider = str(getattr(self.judge_model, "provider", "") or config.chat_base_url)
        self.writer_model_name = str(getattr(self.writer_model, "name", "") or config.writer_model or config.chat_model)
        inline = read_json(
            Path(self.project.metadata) / "cache" / "fast-inline-links" / "manifest.json",
            default={},
        )
        if inline.get("pages"):
            raise RuntimeError(
                "fast inline links are active; run `sync --fast --link-reset` before --repair"
            )

    def targets(self, items: Sequence[str] | None = None) -> list[str]:
        from publisher.ledger import load_ledger

        ledger = load_ledger(Path(self.project.metadata) / "pipeline.json")
        sources = {
            str(mount).strip("/"): str(row.get("raw_rel") or "").strip("/")
            for mount, row in ledger.sources.items()
            if row.get("raw_rel")
        }

        def ordered(raw_rels: Sequence[str]) -> list[str]:
            """Start the global repair queue with the smallest documents."""

            def page_count(raw_rel: str) -> int:
                try:
                    plan = read_json(
                        Path(self.project.state_dir(raw_rel)) / "state" / "plan.json",
                        default={},
                    )
                    pages = plan.get("pages", []) if isinstance(plan, dict) else []
                    return len(pages) if isinstance(pages, list) else 10**9
                except (OSError, TypeError, ValueError):
                    # Keep malformed documents eligible; repair_document will
                    # report the actual state error when this target is reached.
                    return 10**9

            return sorted(set(raw_rels), key=lambda raw_rel: (page_count(raw_rel), raw_rel))

        if not items:
            return ordered(
                raw_rel for raw_rel in sources.values() if not _is_tabular_document(raw_rel)
            )
        selected: list[str] = []
        missing: list[str] = []
        for raw_item in items:
            item = str(raw_item).strip().lstrip("/").rstrip("/")
            matches = [
                raw_rel for mount, raw_rel in sources.items()
                if mount == item or mount.startswith(item + "/") or raw_rel == item
            ]
            if not matches:
                missing.append(item)
            selected.extend(
                raw_rel for raw_rel in matches if not _is_tabular_document(raw_rel)
            )
        if missing:
            raise ValueError("unknown repair source selection: " + ", ".join(missing))
        return ordered(selected)

    def _load_document(
        self,
        raw_rel: str,
        *,
        force: bool,
        limit: int | None,
    ) -> tuple[
        Path,
        dict[str, Any],
        dict[str, Any],
        list[_PageInput],
        dict[str, tuple[str, str]],
    ]:
        from publisher.human_changes import HumanStore

        state_root = Path(self.project.state_dir(raw_rel))
        snapshot = state_root / "source" / "original.md"
        raw_path = Path(self.project.raw_file(raw_rel))
        if not snapshot.is_file() or not raw_path.is_file():
            raise FileNotFoundError(f"repair state/source is unavailable for {raw_rel}")
        source_text = snapshot.read_text(encoding="utf-8")
        current_raw = raw_path.read_text(encoding="utf-8")
        if not source_text.strip():
            raise RuntimeError(
                f"{raw_rel}: source is empty; check conversion/extraction before repairing the wiki"
            )
        source_hash = sha256_text(source_text)
        if sha256_text(current_raw) != source_hash:
            raise RuntimeError(f"{raw_rel}: source changed; run a normal sync before repair")
        plan = read_json(state_root / "state" / "plan.json")
        manifest = read_json(state_root / "state" / "manifest.json", default={})
        for label, value in (
            ("plan", plan.get("source_sha256")),
            ("manifest", manifest.get("source_sha256")),
        ):
            if value and value != source_hash:
                raise RuntimeError(f"{raw_rel}: {label} source hash is stale; run a normal sync")

        normalized = normalize_source(source_text)
        masked_source = _mask_source_images(normalized)
        source_lines = split_source_lines(masked_source)
        inputs: list[_PageInput] = []
        pending_title_changes: dict[str, tuple[str, str]] = {}
        examined = 0
        for item in plan.get("pages", []):
            if limit is not None and examined >= limit:
                break
            number = int(item["number"])
            filename = str(item["filename"])
            state_path = state_root / "state" / "pages" / f"{number:03d}.json"
            page_path = state_root / "wiki" / filename
            state = read_json(state_path, default={})
            try:
                state_rel = state_path.relative_to(Path(self.project.root)).as_posix()
            except ValueError:
                state_rel = ""
            resume_checkpoint = state_rel in self.resume_page_states
            if not state or not page_path.is_file():
                raise RuntimeError(f"{raw_rel}: incomplete generated page state for {filename}")
            if state.get("human_edited"):
                raise RuntimeError(f"{raw_rel}: {filename} contains legacy human state; normal sync is required")
            if state.get("filename") not in (None, filename):
                raise RuntimeError(f"{raw_rel}: page filename state mismatch for {filename}")
            provenance_hash = (
                (state.get("provenance") or {}).get("source_document") or {}
            ).get("sha256")
            if provenance_hash and provenance_hash != source_hash:
                raise RuntimeError(f"{raw_rel}: stale provenance for {filename}; run a normal sync")
            text = page_path.read_text(encoding="utf-8")
            content_hash = sha256_text(text)
            if state.get("content_sha256") and state.get("content_sha256") != content_hash:
                raise RuntimeError(f"{raw_rel}: generated page hash mismatch for {filename}")
            shell = _split_page(text)
            if state.get("repair_version") == self.version or resume_checkpoint:
                state_title = str(state.get("title") or shell.title)
                if state_title != shell.title:
                    raise RuntimeError(f"{raw_rel}: repaired title state mismatch for {filename}")
                previous_title = str(
                    state.get("repair_previous_title") or item.get("title") or shell.title
                )
                if previous_title != shell.title or str(item.get("title") or "") != shell.title:
                    pending_title_changes[filename] = (previous_title, shell.title)
            if (
                (not force or resume_checkpoint)
                and (state.get("repair_version") == self.version or resume_checkpoint)
                and state.get("repair_status") in {"clean", "repaired"}
                and state.get("content_sha256") == content_hash
            ):
                continue

            owner = _valid_ranges(state.get("source_ranges") or item.get("owner_ranges"), len(source_lines))
            planned_owner = _valid_ranges(item.get("owner_ranges"), len(source_lines))
            if owner != planned_owner:
                raise RuntimeError(f"{raw_rel}: source ownership mismatch for {filename}")
            references = _valid_ranges(
                state.get("reference_ranges") or item.get("reference_ranges", []), len(source_lines)
            )
            masked_body, images = _mask_page_images(shell.body)
            owned_source = _slice_ranges(source_lines, owner)
            reference_source = _slice_ranges(source_lines, references)
            allowed_source = owned_source + ("\n\n" + reference_source if reference_source else "")
            context = recent_writer_context(
                page=item,
                pages=plan.get("pages", []),
                previous_source="\n".join(source_lines[: owner[0][0] - 1]),
            )
            inputs.append(_PageInput(
                document=raw_rel,
                number=number,
                filename=filename,
                owner_ranges=owner,
                reference_ranges=references,
                original_text=text,
                shell=shell,
                masked_body=masked_body,
                images=images,
                owner_evidence=_numbered_ranges(source_lines, owner),
                reference_evidence=_numbered_ranges(source_lines, references),
                allowed_source=allowed_source,
                context=context,
                page_path=page_path,
                state_path=state_path,
                state=state,
                guidance=HumanStore(self.project).guidance(raw_rel).get(filename, []),
            ))
            examined += 1
        return state_root, plan, manifest, inputs, pending_title_changes

    def _commit_outcome(
        self,
        state_root: Path,
        outcome: _PageOutcome,
        manifest_pages: dict[int, dict[str, Any]],
    ) -> bool:
        page = outcome.page
        artifact_root = _artifact_root(state_root, self.version, page.number)
        _write_artifacts(
            artifact_root,
            [
                *outcome.artifacts,
                _Artifact("model.json", {
                    "model": self.model_name,
                    "provider": self.model_provider,
                    "repair_version": self.version,
                }),
            ],
        )
        changed = False
        if outcome.accepted:
            previous = page.page_path.read_text(encoding="utf-8")
            if previous != outcome.final_text:
                write_text_atomic(page.page_path, outcome.final_text)
                changed = True
        final_text = page.page_path.read_text(encoding="utf-8")
        state = dict(page.state)
        state.update({
            "title": outcome.title if outcome.accepted else page.shell.title,
            "content_sha256": sha256_text(final_text),
            "judge_score": outcome.judge_score,
            "defects": list(outcome.defects),
            "missing_important_information": list(outcome.missing),
            "repair_version": self.version,
            "repair_attempts": outcome.attempts,
            "repair_status": outcome.status,
            "repair_selected_attempt": outcome.selected_attempt,
            "repair_selection": outcome.selection,
            "repair_feedback": list(outcome.defects),
            "repair_model": self.model_name,
            "repair_model_provider": self.model_provider,
        })
        state.pop("repair_checkpoint_stage", None)
        if outcome.accepted and outcome.title != page.shell.title:
            state["repair_previous_title"] = page.shell.title
        write_json_atomic(page.state_path, state)
        page.state = state
        (artifact_root / "checkpoint.json").unlink(missing_ok=True)
        manifest_page = manifest_pages.get(page.number)
        if manifest_page is not None:
            manifest_page.update({
                "title": state["title"],
                "judge_score": outcome.judge_score,
                "defects": list(outcome.defects),
                "missing_important_information": list(outcome.missing),
                "repair_version": self.version,
                "repair_attempts": outcome.attempts,
                "repair_status": outcome.status,
                "repair_selected_attempt": outcome.selected_attempt,
                "repair_selection": outcome.selection,
                "repair_model": self.model_name,
                "repair_model_provider": self.model_provider,
            })
        return changed

    def _apply_title_changes(
        self,
        state_root: Path,
        plan: dict[str, Any],
        manifest: dict[str, Any],
        changes: dict[str, tuple[str, str]],
    ) -> set[str]:
        if not changes:
            return set()
        changed_files: set[str] = set()
        plan_by_name = {str(item["filename"]): item for item in plan.get("pages", [])}
        manifest_by_name = {str(item["filename"]): item for item in manifest.get("pages", [])}

        def update_references(holder: dict[str, Any]) -> bool:
            changed = False
            provenance = holder.get("provenance")
            if not isinstance(provenance, dict):
                return False
            records = provenance.get("imported_from_pages")
            if not isinstance(records, list):
                return False
            for record in records:
                if not isinstance(record, dict):
                    continue
                target = changes.get(str(record.get("filename") or ""))
                if target is not None and record.get("title") != target[1]:
                    record["title"] = target[1]
                    changed = True
            return changed

        for filename, (_old, new) in changes.items():
            if filename in plan_by_name:
                item = plan_by_name[filename]
                old_title = str(item.get("title") or "")
                item["title"] = new
                path = item.get("path")
                if isinstance(path, list) and path and str(path[-1]) == old_title:
                    path[-1] = new
            if filename in manifest_by_name:
                manifest_by_name[filename]["title"] = new
        for item in plan_by_name.values():
            update_references(item)
        for item in manifest_by_name.values():
            update_references(item)

        for path in sorted((state_root / "wiki").glob("*.md")):
            if path.name == "_review.md":
                continue
            text = path.read_text(encoding="utf-8")
            updated = text
            for filename, (_old, new) in changes.items():
                updated = _replace_link_title(updated, filename, new)
            if updated == text:
                continue
            write_text_atomic(path, updated)
            changed_files.add(path.name)

        for filename, item in plan_by_name.items():
            sidecar = state_root / "state" / "pages" / f"{int(item['number']):03d}.json"
            state = read_json(sidecar, default={})
            if not state:
                continue
            state_changed = update_references(state)
            own_change = changes.get(filename)
            if own_change is not None and state.get("title") != own_change[1]:
                state["title"] = own_change[1]
                state_changed = True
            if filename in changed_files:
                state["content_sha256"] = sha256_text(
                    (state_root / "wiki" / filename).read_text(encoding="utf-8")
                )
                state_changed = True
            if state_changed:
                write_json_atomic(sidecar, state)
        return changed_files

    def _clear_title_markers(
        self,
        state_root: Path,
        plan: dict[str, Any],
        filenames: set[str],
    ) -> None:
        by_name = {str(item["filename"]): item for item in plan.get("pages", [])}
        for filename in filenames:
            item = by_name.get(filename)
            if item is None:
                continue
            path = state_root / "state" / "pages" / f"{int(item['number']):03d}.json"
            state = read_json(path, default={})
            if "repair_previous_title" in state:
                state.pop("repair_previous_title", None)
                write_json_atomic(path, state)

    def _promote(self, raw_rel: str, state_root: Path) -> list[str]:
        from graph.wiki.export import export_ingest_layout
        from graph.workspace.writer import publish_output, write_source_stamp
        from publisher.human_changes import LlmHumanModel, apply_generated

        target = Path(self.project.wiki_dir(raw_rel))
        source_stamp = read_json(target / "_planning" / "source.json", default={})
        with tempfile.TemporaryDirectory(prefix="fast-repair-") as temporary:
            staged = export_ingest_layout(
                state_root, Path(temporary) / "out", document_name=raw_rel
            )
            publish_output(staged, target)
        write_source_stamp(
            target,
            Path(self.project.raw_file(raw_rel)),
            raw_rel,
            identity_seed=str(source_stamp.get("id_seed") or raw_rel),
        )
        write_json_atomic(target / "_planning" / "linker.json", {
            "schema_version": 2, "status": "disabled",
        })
        overlay = apply_generated(self.project, raw_rel, model=LlmHumanModel(self.settings, self.project))
        return sorted(overlay.changed_pages)

    def repair_document(
        self,
        raw_rel: str,
        *,
        limit: int | None = None,
        force: bool = False,
        promote: bool = True,
    ) -> dict[str, Any]:
        if _is_tabular_document(raw_rel):
            source_kind = kind_of(raw_rel)
            return {
                "raw_rel": raw_rel,
                "status": "skipped-excel" if source_kind == "xlsx" else "skipped-csv",
                "examined": 0,
                "repaired": 0,
                "clean": 0,
                "review": 0,
                "changed_pages": [],
                "overlay_pages": [],
                "promoted": False,
                "model": self.model_name,
                "writer_model": self.writer_model_name,
                "judge_model": self.model_name,
            }
        state_root, plan, manifest, pages, pending_title_changes = self._load_document(
            raw_rel, force=force, limit=limit
        )
        _emit(
            self.on_progress,
            "document_started",
            document=raw_rel,
            pages=len(pages),
            concurrency=self.concurrency,
        )
        manifest_pages = {int(item["number"]): item for item in manifest.get("pages", [])}
        changed_pages: set[str] = set()
        title_changes: dict[str, tuple[str, str]] = dict(pending_title_changes)
        outcomes: list[_PageOutcome] = []
        page_failures: list[str] = []

        async def run() -> None:
            # Run complete document-level passes so the model gets a warm,
            # consistent prefill: judge every page, write every rejected page,
            # then judge every candidate before the next writer pass.
            write_queue: list[_RepairSession] = []
            candidate_queue: list[_RepairSession] = []

            def commit(outcome: _PageOutcome, phase_error: str = "") -> None:
                if phase_error:
                    page_failures.append(f"{outcome.page.filename}: {phase_error}")
                try:
                    committed = self._commit_outcome(state_root, outcome, manifest_pages)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    page_failures.append(f"{outcome.page.filename}: {error}")
                    _emit(
                        self.on_progress,
                        "page_review",
                        document=raw_rel,
                        page=outcome.page.filename,
                        attempts=outcome.attempts,
                        error=error,
                    )
                    log.exception(
                        "fast repair checkpoint failed for %s page %s",
                        raw_rel,
                        outcome.page.filename,
                    )
                    return
                outcomes.append(outcome)
                if committed:
                    changed_pages.add(outcome.page.filename)
                if outcome.accepted and outcome.title != outcome.page.shell.title:
                    title_changes[outcome.page.filename] = (
                        outcome.page.shell.title, outcome.title
                    )

            def checkpoint(session: _RepairSession, stage: str) -> bool:
                try:
                    _write_session_checkpoint(
                        state_root,
                        self.version,
                        session,
                        stage=stage,
                        model_name=self.model_name,
                        model_provider=self.model_provider,
                    )
                    if stage != "initial-judge":
                        _emit(
                            self.on_progress,
                            "page_checkpointed",
                            document=raw_rel,
                            page=session.page.filename,
                            next_stage=stage,
                            attempt=session.attempts,
                        )
                    return True
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    log.exception(
                        "fast repair pending checkpoint failed for page %s",
                        session.page.filename,
                    )
                    commit(
                        _page_exception_outcome(
                            session.page, exc, on_progress=self.on_progress
                        ),
                        error,
                    )
                    return False

            async def initial_job(page: _PageInput):
                session = _new_repair_session(page)
                try:
                    outcome = await _round_initial_judge(
                        session,
                        judge_model=self.judge_model,
                        on_progress=self.on_progress,
                    )
                    return session, outcome, ""
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    log.exception("fast repair initial judge failed for page %s", page.filename)
                    return session, _page_exception_outcome(
                        page, exc, on_progress=self.on_progress
                    ), error

            async def writer_job(session: _RepairSession):
                try:
                    action, outcome = await _round_write(
                        session,
                        writer_model=self.writer_model,
                        on_progress=self.on_progress,
                    )
                    return session, action, outcome, ""
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    log.exception(
                        "fast repair writer phase failed for page %s",
                        session.page.filename,
                    )
                    return session, "done", _page_exception_outcome(
                        session.page, exc, on_progress=self.on_progress
                    ), error

            async def candidate_job(session: _RepairSession):
                try:
                    action, outcome = await _round_candidate_judge(
                        session,
                        judge_model=self.judge_model,
                        on_progress=self.on_progress,
                    )
                    return session, action, outcome, ""
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:1000]
                    log.exception(
                        "fast repair candidate judge failed for page %s",
                        session.page.filename,
                    )
                    return session, "done", _page_exception_outcome(
                        session.page, exc, on_progress=self.on_progress
                    ), error

            initial_pages: list[_PageInput] = []
            for page in pages:
                try:
                    state_rel = page.state_path.relative_to(
                        Path(self.project.root)
                    ).as_posix()
                except ValueError:
                    state_rel = ""
                restored = (
                    _resume_session_checkpoint(state_root, self.version, page)
                    if state_rel in self.resume_page_states
                    else None
                )
                if restored is None:
                    session = _new_repair_session(page)
                    if checkpoint(session, "initial-judge"):
                        initial_pages.append(page)
                    continue
                stage, session = restored
                _emit(
                    self.on_progress,
                    "page_resumed",
                    document=raw_rel,
                    page=page.filename,
                    next_stage=stage,
                    attempt=session.attempts,
                )
                if stage == "initial-judge":
                    initial_pages.append(page)
                elif stage == "writer":
                    write_queue.append(session)
                else:
                    candidate_queue.append(session)

            # Initial judge pass only contains pages with no durable verdict.
            # Results are checkpointed as they finish while the sliding window
            # continues admitting later pages.
            if initial_pages:
                _emit(
                    self.on_progress,
                    "judge_pass_started",
                    document=raw_rel,
                    kind="initial",
                    pass_number=0,
                    pages=len(initial_pages),
                    concurrency=self.concurrency,
                )
                async for session, outcome, error in bounded_as_completed(
                    initial_pages, self.concurrency, initial_job
                ):
                    if outcome is not None:
                        commit(outcome, error)
                    elif checkpoint(session, "writer"):
                        write_queue.append(session)

            # A candidate produced before an interruption has already had its
            # writer pass, so judge it rather than rewriting or initial-judging it.
            if candidate_queue:
                resumed_candidates = candidate_queue
                candidate_queue = []
                _emit(
                    self.on_progress,
                    "judge_pass_started",
                    document=raw_rel,
                    kind="resumed-candidate",
                    pass_number=0,
                    pages=len(resumed_candidates),
                    concurrency=self.concurrency,
                )
                async for session, action, outcome, error in bounded_as_completed(
                    resumed_candidates, self.concurrency, candidate_job
                ):
                    if outcome is not None:
                        commit(outcome, error)
                    elif action == "retry" and checkpoint(session, "writer"):
                        write_queue.append(session)

            rewrite_pass = 1
            while write_queue:
                # Writer pass: keep a sliding window full across the complete
                # rewrite queue, without judging until the pass is finished.
                current_write_queue = write_queue
                write_queue = []
                candidate_queue = []
                if current_write_queue:
                    _emit(
                        self.on_progress,
                        "writer_pass_started",
                        document=raw_rel,
                        pass_number=rewrite_pass,
                        pages=len(current_write_queue),
                        concurrency=self.concurrency,
                    )
                    async for session, action, outcome, error in bounded_as_completed(
                        current_write_queue, self.concurrency, writer_job
                    ):
                        if outcome is not None:
                            commit(outcome, error)
                        elif action == "candidate" and checkpoint(session, "judge"):
                            candidate_queue.append(session)
                        elif action == "retry" and checkpoint(session, "writer"):
                            write_queue.append(session)

                # Candidate judge pass: keep the judge window full across all
                # candidates before starting another writer pass.
                next_write_queue = write_queue
                write_queue = []
                if candidate_queue:
                    _emit(
                        self.on_progress,
                        "judge_pass_started",
                        document=raw_rel,
                        kind="candidate",
                        pass_number=rewrite_pass,
                        pages=len(candidate_queue),
                        concurrency=self.concurrency,
                    )
                    async for session, action, outcome, error in bounded_as_completed(
                        candidate_queue, self.concurrency, candidate_job
                    ):
                        if outcome is not None:
                            commit(outcome, error)
                        elif action == "retry" and checkpoint(session, "writer"):
                            next_write_queue.append(session)
                write_queue = next_write_queue
                rewrite_pass += 1

        if pages:
            run_async_blocking(run())
        changed_pages.update(self._apply_title_changes(state_root, plan, manifest, title_changes))
        write_json_atomic(state_root / "state" / "plan.json", plan)
        write_json_atomic(state_root / "state" / "manifest.json", manifest)
        review_changed = _update_review(state_root, self.version)
        if outcomes:
            run_state_path = state_root / "state" / "run.json"
            run_state = read_json(run_state_path, default={})
            run_state.update({
                "repair_version": self.version,
                "repair_examined": len(outcomes),
                "repair_repaired": sum(item.status == "repaired" for item in outcomes),
                "repair_review_pages": sum(item.status == "review" for item in outcomes),
                "repair_model": self.model_name,
                "repair_model_provider": self.model_provider,
                "repair_writer_model": self.writer_model_name,
            })
            write_json_atomic(run_state_path, run_state)
        should_promote = bool(changed_pages or title_changes or review_changed)
        overlay_pages: list[str] = []
        if should_promote and promote:
            overlay_pages = self._promote(raw_rel, state_root)
            self._clear_title_markers(state_root, plan, set(title_changes))
        result = {
            "raw_rel": raw_rel,
            "status": (
                "repaired" if should_promote
                else "clean" if outcomes
                else "up-to-date"
            ),
            "examined": len(outcomes),
            "repaired": sum(item.status == "repaired" for item in outcomes),
            "clean": sum(item.status == "clean" for item in outcomes),
            "review": sum(item.status == "review" for item in outcomes),
            "changed_pages": sorted(changed_pages),
            "overlay_pages": overlay_pages,
            "promoted": bool(should_promote and promote),
            "model": self.model_name,
            "writer_model": self.writer_model_name,
            "judge_model": self.model_name,
            "failures": [f"{raw_rel}: {failure}" for failure in page_failures],
        }
        _emit(self.on_progress, "document_done", document=raw_rel, **result)
        return result


__all__ = [
    "FastRepair", "MAX_REWRITE_ATTEMPTS", "RepairJudgeResult", "VERSION",
]
