"""Fast wiki behaviour: titles, identifiers, plans and context without extra model calls."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any, Callable, Sequence

from graph.wiki.storage import read_json, write_json_atomic

# "1.", "1.2 ", "（1）", "第3章": a section number ends in punctuation or a space,
# so "2024-04", "1.5倍" and "1:1" are left alone.
HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節条項部編]"
    r"|[(（]?[0-9０-９]{1,3}(?:[.．][0-9０-９]{1,3})*(?:[.．)）、](?![0-9０-９])|(?=\s)))\s*"
)
ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# A translation rightly writes ``\beta_1`` as β₁ and "## 6.1 EXPERIMENT: LOGISTIC REGRESSION"
# in the output language; neither is an identifier to keep (every pdf retry was one of these).
TEX_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
TEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
SHOUTED_WORD_RE = re.compile(r"\b[A-Z]{2,}\b")
LOW_COVERAGE_THRESHOLD = 80
_IMAGE_TOKEN_RE = re.compile(
    r"\[\[(?:NEO-IMAGE:[^\]]+|SOURCE_IMAGE_\d+|WIKI_IMAGE_\d+)\]\]"
)
_LOW_COVERAGE_BLOCK_RE = re.compile(
    r"<!-- 情報保持スコアが80未満です。.*?</span>", re.DOTALL
)


FAST_GENERATION_JUDGE_RULES = """# Fast Wiki査読規則
行番号付き原文と明示された追加事実だけが事実の根拠である。ページ要約、周辺コンテキスト、
見出し名は執筆位置を示す参考情報であり、それだけでは本文へ新しい事実を追加する根拠にならない。
この査読は原文の体裁再現ではなく、候補が意味を完全かつ自然な日本語で保持したかを確認する。

## 必ず保持する意味
- 処理の目的、対象、入力、出力、動作、条件、制約、例外、処理順。
- 正式な機能名、モジュール名、処理名、入力名、出力名、画面名、DB名、項目名、識別子、コード。
- 数値、単位、閾値、日時、式、エラー値、および「または/かつ」「含む/除く」「以上/未満」等の論理。
- 保存/表示、適用/除外、入力/出力、上/下、送信/受信、連係元/連係先、率/量等の区別。
- 表の各ラベル、セルの意味、値、注記、凡例、○/◎等の記号と対応関係。
- 図表キャプションに含まれる説明語。図表番号だけは不要だが、説明部分は情報として保持する。
- 質問・確認依頼・提案・仮定・未確定事項という発言の意図、確認先と依頼内容、確定/未確定の区別。
  「三菱様、この理解で相違ございませんでしょうか？」を確定した仕様の断言に変えたら、
  確認依頼の欠落と根拠のない確定として不承認にし、coverage_scoreにも情報損失を反映する。
  疑問符の有無ではなく、「三菱への確認事項：この理解が正しいか」のような意味の保持を確認する。

## 要約による欠落を承認しない
修復は要約ではない。原文に裏付けられる異なる一覧項目、索引項目、表行、短い説明を一件ずつ照合する。
多数の項目を少数の一般名へ統合した候補、複合名称を上位概念へ短縮した候補、括弧内限定や接頭辞・
接尾辞を落とした候補は、文章が自然でも欠落である。例えば
「調整電源(火力)上下限カーブ作成」を「火力」にしてはならない。「入力データ」「出力データ」
「処理内容」という表示形式は変えてよいが、その配下の固有項目名と説明は削除してはならない。

## 原文構造との違いは欠陥にしない
章/節/項番号、図/表/画像番号、ページ番号、原文行番号、Markdown見出しレベル、見出し階層、
字下げ、目次リンク、単なる箇条書き番号、ページ区切り、空プレースホルダー、表の罫線やHTML属性、
原文と同じ行順・行数の不在は欠落ではない。ただし番号を省いても処理順の意味は残す。
見出しレベルの飛びや原文と異なるネストだけをdefectsへ報告しない。空見出し、反復見出し、
意味を誤分類する見出し、原文にない仕様を主張する見出しだけを報告する。
Mermaid図の有無・構文・描画・矢印数は査読・採点の対象外とし、図の再現を要求しない。ただし原文の図に含まれる項目、値、条件、処理順、接続元/接続先と対応関係は情報であり、文章や表などで全て保持されているか照合する。

## 表の判定
候補の表は有効なHTMLの`table`、`thead`、`tbody`、`tr`、`th`、`td`で構成する。
Markdownパイプ表は本文品質の欠陥として不承認にする。単純な丸/バツの表記用TeXは○/×へ統一する。
数式として意味を持つTeXや変数は保持し、◎など意味の異なる記号を○へまとめない。
Markdown表やOCRの罫線、壊れたフェンスをそのまま再現する必要はない。行列の対応、見出しと値、
意味のある空セル・空行、セル結合が示すグループ関係を保持したかを判定する。OCRが作った重複行や、
対応関係を持たない全空行を除去したことは欠落にしない。

## 候補が作った欠陥
H1の直後には、このページの対象と内容が分かる短い導入段落を置く。原文の事実を言い換えた導入は
捏造ではない。本文の詳細を削らず、表だけのページでも原文で分かる表の対象や読み方を説明する。
ただし原文がごく短い一文だけなら、その一文を導入と本文に重複させなくてよい。
原文が説明文のない保護画像だけの場合は導入を要求せず、画像内容を推測しない。
導入の欠落、説明のない表、文脈から浮いたOCR断片や不自然な文末は本文品質の欠陥として不承認にする。
意味のある断片は適切な説明や表注記へ統合し、情報を捨てていないか確認する。
原文で裏付けられない導入、説明、一般論、分類、見出し、表ヘッダ、英語展開、数値、単位、動作を一つでも
追加していればdefectsへ報告する。「正規化した」「変換した」「保存する」等を文章のつなぎとして
補っても捏造である。意味の狭窄/拡張、論理の反転、項目間の対応変更も報告する。
中国語簡体字、意味不明な外国語、拒否文、話題逸脱、文字化け、%%、無限反復、同じ文・語・見出しの
不自然な反復、壊れたMarkdown、装飾用の単独`---`を報告する。原文にも反復があるという理由だけで
候補の反復を許可しない。同じ事実のOCR重複は一つへ統合し、時系列値や表の対応など反復自体に意味が
ある場合だけ保持する。

原文から候補への照合と、候補から原文への逆照合を両方行う。一つでも意味ある欠落、固有名称の短縮、
意味変更、無根拠な追加、日本語品質の欠陥があれば具体的な問題語と正しい原文内容を返す。
coverage_scoreは意味の保持率であり、体裁だけの欠陥では下げない。導入の欠落・表記不統一・読みにくい
断片はbody_defectsへ報告してapproved=falseにする。coverage_scoreが高くても問題を空にしてはならない。
過去の試行だけにあった問題を現在候補へ持ち越さない。"""


FAST_GENERATION_WRITER_RULES = """# Fast Wiki執筆・修復規則
これは要約ではなく、提示された原文を情報欠落なく日本語Wikiへ整える作業である。短くすること、
項目数や行数を減らすこと、多数の項目を総称へまとめることを品質改善とみなさない。
前回の査読がある場合は現在案の正しい部分を保ち、指摘箇所だけを必要最小限に直す。
Wikiは文章・表・既存画像だけで簡潔に構成し、Mermaidなどの図式は作成・出力せず、原文の図式に含まれる情報は文章や表で表す。

## 保持する情報
- 原文に裏付けられる異なる事実、一覧項目、索引項目、表の意味、短い説明を一件ずつ残す。
- 正式な機能名、モジュール名、処理名、入力名、出力名、画面名、DB名、項目名を完全な名称で残す。
- 複合名称を短い上位概念へ置換せず、括弧内の対象、方向、処理種別、連係元/連係先、DB区分、
  保存/表示、適用/除外、率/量など項目を区別する語を落とさない。
- 条件、制約、例外、処理順、または/かつ、含む/除く、以上/未満、数値、単位、閾値、日時、式、
  エラー値、識別子、コードを変えない。
- 質問・確認依頼・提案・仮定・未確定事項の意図を保持し、確定した仕様や決定事項へ変えない。
  確認先と何を確認したいかも残す。「三菱様、この理解で相違ございませんでしょうか？」は、
  例えば「三菱への確認事項：この理解が正しいか」と整理できるが、「この理解が正しい」と断言しない。
  業務上の質問や確認依頼は内部作業メモやOCRノイズではない。原文にない回答を補わない。
- 表は必ず読みやすいHTML表へ作り直す。`table`、`thead`、`tbody`、`tr`、`th`、`td`を正しく閉じ、
  全ラベル、セルの意味、値、注記、凡例、○/◎、行列の対応、意味のある空セル・空行を残す。
  セル結合が意味を持つ場合は`rowspan`/`colspan`で同じ関係を表す。OCR由来の壊れた罫線、
  重複した全空行、表を囲むだけのコードフェンスはコピーしない。
  Markdownパイプ表は使わない。単純な丸/バツを表すTeX（\\bigcirc、\\times等）は○/×へ統一する。
  数式として意味を持つTeXや変数、意味の異なる○/◎などの区別は保持する。
- 「図 1-1 処理フロー」の番号は省いてよいが「処理フロー」は残す。数字だけの図表ラベルは追加しない。

## 持ち込まない構造とノイズ
章/節/項番号、図表番号、ページ番号、原文行番号、目次リンク、単なる箇条書き番号、空見出し、
ページ区切り、OCR重複、文字化け、%%、モデル用の内部メモ、モデル名はWiki本文へ移植しない。
見出しレベルや表レイアウトを原文と同じにする必要はないが、構造を変えるために事実を削除しない。
通常の表やコードフェンスは原文として見えている。`[[NEO-IMAGE:...]]`だけが保護対象の画像である。
表やフェンスを画像トークンとして扱わず、内容を読んでWiki向けに再構成する。

## 捏造と退行を防ぐ
H1の直後に、今回の原文に裏付けられる短い導入段落を書き、何についてのページかを説明する。
表が中心でも、表の対象や原文で分かる読み方を自然な文章で説明する。導入は詳細の代わりではなく、
本文の事実や表セルを省略しない。原文がごく短い一文だけなら、その一文を重複させなくてよい。
原文が説明文のない保護画像だけの場合は、画像内容や導入を推測して作らない。
原文に裏付けられる既存の導入は保持・改善する。孤立したOCR断片は、意味があれば適切な段落や
表注記へ自然に統合し、意味を復元できないノイズだけを除く。
ページ要約や周辺コンテキストを原文扱いせず、今回の原文または明示された追加事実にない導入、一般論、
分類名、見出し、表ヘッダ、英語展開、数値、単位、動作を作らない。「正規化した」「変換した」
「保存する」等を滑らかな説明として補わない。原文にない長い総称を考案せず、既存語を勝手に膨らませない。
導入を各節で繰り返さず、このページが所有する事実だけを書く。

中国語簡体字が一字だけ混じる場合は周辺の名称・表行を削除せず、日本語の字へ直す
（值→値、压→圧、单→単、项→項、关→関、变→変、应→応、查→査、运→運、总→総、发→発、图→図、录→録）。
拒否文、話題逸脱、不自然な外国語、無限反復、重複見出し、壊れたMarkdown、装飾用の単独`---`を出力しない。

出力前に、原文の各固有項目、複合名称、括弧内限定、論理語、条件、方向、入出力、値、単位、表凡例、
○/◎、質問・確認依頼・提案・仮定・未確定事項を一つずつ照合し、欠落・統合・改名・意味変更・
無根拠な追加がないことを確認する。"""


def generation_prompt_rules(role: str) -> str:
    """Shared fast-generation rubric, also embedded by the standalone repair pass."""

    if role == "judge":
        return FAST_GENERATION_JUDGE_RULES
    if role == "writer":
        return FAST_GENERATION_WRITER_RULES
    return ""


def recent_writer_context(
    *,
    page: Any,
    pages: Sequence[Any],
    previous_source: str,
    base: str = "",
    source_limit: int = 12_000,
    line_limit: int = 50,
    page_limit: int = 3,
) -> str:
    """Orientation for fast writers: recent summaries plus preceding source text."""

    def value(item: Any, name: str, default: Any = "") -> Any:
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    current = int(value(page, "number", 0) or 0)
    previous_pages = sorted(
        (item for item in pages if int(value(item, "number", 0) or 0) < current),
        key=lambda item: int(value(item, "number", 0) or 0),
    )[-page_limit:]
    out = [base.rstrip()] if base.strip() else []
    if previous_pages:
        out.extend([
            "## 直前のWikiページ要約（文脈専用）",
            "以下は話の流れを理解するためだけに使い、この節の事実根拠にはしない。",
        ])
        for item in previous_pages:
            title = str(value(item, "title", "")).strip() or "無題"
            summary = str(value(item, "summary", "")).strip() or "要約なし"
            out.append(f"- {title} — {summary[:400]}")
    prior = "\n".join(previous_source.splitlines()[-line_limit:])
    if len(prior) > source_limit:
        prior = prior[-source_limit:]
    prior = prior.strip()
    if prior:
        out.extend([
            "## 直前の原文（直前50行・最大12,000文字・文脈専用）",
            "これは現在のページより前の流れを理解するための資料である。現在のページにない事実をコピーしない。",
            prior,
        ])
    return "\n".join(out).rstrip() + ("\n" if out else "")


def append_low_coverage_source(markdown: str, source: str, score: int | None) -> str:
    """Keep readable output while retaining raw evidence when fact coverage is low."""

    if score is None or score >= LOW_COVERAGE_THRESHOLD:
        return markdown
    raw = _IMAGE_TOKEN_RE.sub("", source or "").strip()
    if not raw:
        return markdown
    # Keep source HTML inert so it cannot escape the hidden safety container.
    raw = escape(raw, quote=False)
    warning = (
        "<!-- 情報保持スコアが80未満です。以下の非表示原文を確認し、失われた事実があれば本文へ"
        "復元してください。人間の編集者が確認後にこのspanを削除しても構いません。 -->"
    )
    hidden = (
        '<span hidden style="display:none" data-llm-wiki-low-coverage="true">\n'
        + raw
        + "\n</span>"
    )
    return markdown.rstrip() + "\n\n" + warning + "\n" + hidden + "\n"


def strip_heading_number(text: str) -> str:
    """Remove a formatting prefix from a title without touching body text."""

    return HEADING_NUMBER_RE.sub("", text, count=1).strip()


def strip_heading_numbers(markdown: str) -> str:
    """strip_heading_number on every ATX heading outside code fences."""

    protected, restore = mask_math(markdown)
    out: list[str] = []
    fenced = False
    for line in protected.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (match := ATX_HEADING_RE.match(line)):
            line = f"{match.group(1)} {strip_heading_number(match.group(2)) or match.group(2)}"
        out.append(line)
    return restore("\n".join(out))


def translatable(text: str) -> str:
    """Drop TeX command names inside math and all-caps title words on headings."""

    text = TEX_MATH_RE.sub(lambda match: TEX_COMMAND_RE.sub(" ", match.group(0)), text)
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and not re.search(r"[a-z]", line):
            words = SHOUTED_WORD_RE.findall(line)
            # one short all-caps word in a heading is an acronym (## CIO の役割); keep it
            if len(words) >= 2 or any(len(word) >= 6 for word in words):
                line = SHOUTED_WORD_RE.sub(" ", line)
        lines.append(line)
    return "\n".join(lines)


def code_tokens(text: str) -> set[str]:
    from graph.wiki.page import code_tokens as tokens

    return tokens(text, prepare=translatable)


def merge_small_pages(plan: Any, *, target: int) -> Any:
    """Fold each page under half the target into its smaller neighbour (merged <= 2x target).

    The planner often returns 10-30 line pages, and every page costs at least one
    writer call, so a 373-line paper became nine pages instead of four.
    """

    from graph.wiki.document_map import _merge_seed_ranges
    from graph.wiki.schemas import CompiledSeedPlan

    pages = list(plan.pages)

    def size(page: Any) -> int:
        return page.source_end - page.source_start + 1

    while True:
        for _, i in sorted((size(page), i) for i, page in enumerate(pages) if size(page) < target // 2):
            neighbours = sorted(
                (size(pages[j]), j)
                for j in (i - 1, i + 1)
                if 0 <= j < len(pages) and size(pages[i]) + size(pages[j]) <= 2 * target
            )
            if neighbours:
                first, last = sorted((i, neighbours[0][1]))
                pages[first:last + 1] = [_merge_seed_ranges(pages[first:last + 1])]
                break
        else:
            return CompiledSeedPlan(summary=plan.summary, pages=pages)


def deterministic_seed_plan(lines: Sequence[str]) -> Any:
    """A safe single-page plan when no model client exists."""

    from graph.wiki.schemas import CompiledSeedPlan

    first = next((line.strip() for line in lines if line.strip()), "Document")
    title = re.sub(r"^#{1,6}\s+", "", first).strip() or "Document"
    return CompiledSeedPlan(
        summary="fast deterministic plan",
        pages=[{"title": title, "summary": title, "source_start": 1, "source_end": len(lines)}],
    )


def hierarchy(pages: Sequence[Any], lines: Sequence[str], *, checkpoint: Path, version: str) -> dict[str, str]:
    """Parent and page summaries from source leads and seed summaries; no model call."""

    from graph.formats.tree import lead

    cached = read_json(checkpoint, default={}) if Path(checkpoint).exists() else {}
    groups: dict[tuple[str, ...], list[Any]] = {}
    for page in pages:
        groups.setdefault(tuple(page.path[:-1]), []).append(page)
    parents: dict[str, str] = dict(cached.get("parents", {}))
    summaries: dict[str, str] = dict(cached.get("pages", {}))
    for chain, group in groups.items():
        key = " › ".join(chain)
        parents.setdefault(key, lead(lines, group[0].owner_ranges[0][0], group[-1].owner_ranges[-1][1], limit=400))
        for page in group:
            summaries.setdefault(str(page.number), page.summary or lead(lines, page.owner_ranges[0][0], page.owner_ranges[-1][1], limit=400))
    # "policy" marks the checkpoint so the standard path never reuses it as model output.
    write_json_atomic(checkpoint, {"parents": parents, "pages": summaries, "policy": version})
    for page in pages:
        page.summary = summaries.get(str(page.number), page.summary)
    return parents


MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)


def mask_math(text: str) -> tuple[str, Callable[[str], str]]:
    """Hide math and low-coverage evidence while automatic links are inserted."""

    spans: list[str] = []

    def hide(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"{len(spans) - 1}"

    masked = _LOW_COVERAGE_BLOCK_RE.sub(hide, text)
    masked = MATH_RE.sub(hide, masked)
    if not spans:
        return text, _same

    def unmask(value: str) -> str:
        return re.sub("(\\d+)", lambda match: spans[int(match.group(1))], value)

    return masked, unmask


def _same(text: str) -> str:
    return text
