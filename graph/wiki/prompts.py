"""Versioned prompt builders.

Resume must never combine decisions produced by an incompatible prompt, so
every builder is tagged with :data:`PROMPT_VERSION` and the run state records
it.  All builders are pure functions of their inputs: the same inputs always
render the same prompt, which is what makes a replay test meaningful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from .config import (
    PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    SEED_PLAN_COMPILE_VERSION,
    SEED_PLAN_VERSION,
)
from .images import SanitizedSource

COMMON_RULES = (
    "あなたは決定論的な文書Wiki化パイプラインの一作業者である。\n"
    "- 原文の意味を変えず、事実を捏造してはならない。\n"
    "- 判断できない場合は推測せず、注記またはレビュー対象として報告する。\n"
    "- 要求された構造化結果だけを返す。外側の文章は無視される。"
)


# Defects the pipeline produced by itself in past runs, turned into rules:
# repetition loops, invented schema words, wrong-script text, and convert junk.
QUALITY_RULES = (
    "# 品質ルール（Wiki側が自分で作り出す欠陥はすべて不可）\n"
    "- 同じ文・見出し・表を2回以上書かない。ただし原文が繰り返している表の行は全行そのまま写す。\n"
    "- 原文にない表ヘッダ・列名・単位・数値・英語ラベル・略語の正式名称を作らない。"
    "判読できない文字は推測で埋めず「判読不可」と注記する。\n"
    "- 指定の出力言語でない文字（中国語簡体字など）や、日本語の表の英語訳・英語だけの"
    "説明を書かない。\n"
    "- 変換ゴミ（`<nl>`・`<fcel>`・`<lcel>`、`#REF!`、`%%` だけの行、内部型名 `text_image`）を"
    "そのまま写さない。\n"
    "- 「出力することができません」などの断り書き、内容のない見出し、空のコードフェンスを"
    "書かない。\n"
    "- この節の原文と無関係な話題（別文書の説明、一般的な解説テンプレート）を書かない。\n"
)


@dataclass(frozen=True)
class Prompt:
    kind: str
    system: str
    body: str
    version: str = PROMPT_VERSION

    def messages(self) -> list[BaseMessage]:
        return [SystemMessage(content=self.system), HumanMessage(content=self.body)]

    def render(self) -> str:
        """Flatten system and user text for artifact logging."""

        return f"{self.system}\n\n{self.body}"

    def fingerprint(self) -> str:
        from .storage import sha256_text

        return sha256_text(f"{self.version}\n{self.system}\n{self.body}")


def _schema_hint(model: Any) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False)


def _language_rule(config_language: str) -> str:
    return f"タイトル、要約、説明文は必ず{config_language}で書くこと。"


# --------------------------------------------------------------------------
# Stage A
# --------------------------------------------------------------------------


def window_inventory_prompt(
    *,
    window_start: int,
    window_end: int,
    block: SanitizedSource,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    from .wire import WindowInventory

    correction = ""
    if last_error:
        correction = (
            "\n前回の回答は検証に失敗した。修正して有効な結果だけを返すこと。\n"
            f"検証エラー:\n{last_error}\n"
        )

    body = (
        "番号付きソースに何が書かれているかを観察し、範囲付きの目録を作る。"
        "これはページ分割ではなく、後段の計画者へ渡す証拠である。\n\n"
        f"観察ウィンドウ: 原文 {window_start}-{window_end}行（両端を含む）。\n"
        f"{_language_rule(output_language)}\n\n"
        "必須条件:\n"
        f"- 各source_start/source_endは必ず {window_start}-{window_end} の内側にする。\n"
        "- 観察範囲は重複・包含してよい。節全体と、その中の個別APIの両方を記録してよい。\n"
        "- 見えている範囲を無理に隙間なく分割しない。空行や装飾だけの範囲は不要である。\n"
        "- 見出し、説明、手順、制約、警告、表、画像、コード、API、関数、コマンド、"
        "エラーコード、メッセージ、設定項目などを具体的に記録する。\n"
        "- 同じ形式で列挙される具体的エンティティは1件ずつ記録し、kindと"
        "enumeration_familyを統一する。repeated_format=trueにする。\n"
        "- 普通の短い箇条書きや説明上の概念列挙は、個別エンティティとして水増ししない。\n"
        "- 対象がウィンドウの前から始まっていればcontinues_before、後へ続けばcontinues_afterをtrueにする。\n"
        "- 境界付近で途切れて見える内容を、完結した内容だと決めつけない。\n"
        "- 画像や表がどの説明・エンティティに属するかをsummaryまたはparentへ書く。\n"
        f"{correction}\n"
        f"番号付きソース:\n{block.text}\n"
    )
    return Prompt(
        kind="window_inventory",
        system=(
            "重複を許す範囲目録を作り、構造化結果だけを返す。ページ境界は決めない。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(WindowInventory)
        ),
        body=body,
    )


# --------------------------------------------------------------------------
# Stage B
# --------------------------------------------------------------------------


def regional_plan_prompt(
    *,
    region_number: int,
    source_start: int,
    source_end: int,
    window_reports: str,
    previous_region: str,
    output_language: str,
    last_error: str | None = None,
    page_target_lines: int = 200,
) -> Prompt:
    from .wire import RegionalPlan

    correction = f"\n検証エラー:\n{last_error}\n" if last_error else ""
    return Prompt(
        kind="regional_plan",
        version=SEED_PLAN_VERSION,
        system=(
            "最大10個の重複ウィンドウ目録を、一つの地域的なWiki候補地図へ整理する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(RegionalPlan)
        ),
        body=(
            f"地域 {region_number}: 原文 {source_start}-{source_end}行。\n"
            f"{_language_rule(output_language)}\n"
            "重複ウィンドウによる同じ観察を統合し、ページ候補を返す。ここでは候補範囲の重複や"
            "前後地域への継続を許す。最終的な無重複分割は後段が行う。\n"
            f"各候補範囲は今回見えている {source_start}-{source_end}行の内側だけで示し、"
            "地域外へ続く場合はcontinues_before/continues_afterを使う。\n"
            "API関数、コマンド、メッセージ型、設定項目、実質的なエラー項目など、同じ書式で"
            "繰り返される具体的エンティティは個別に把握する。独立した具体的エンティティは"
            "短くても1つ候補として立てる。概念・カテゴリ・導入文・前後の断片だけでは候補を作らず、"
            "短い隣接エンティティは同じ種類・目的のまとまりとしてグループ化してよい。"
            "列挙の導入・共通規則は最も適切なまとまりへ含める。\n"
            f"1候補の最大は{2 * page_target_lines}行。超える章・節は"
            "見出しやエンティティの境界で複数の候補に分ける。\n"
            "ただし、普通の短いリスト、数段落だけの説明、抽象概念を機械的に細分化しない。"
            "行数を揃えるための切断や、エンティティ途中での切断を提案しない。\n"
            f"{correction}\n\n"
            f"前地域の末尾文脈:\n{previous_region or '（なし）'}\n\n"
            f"今回のウィンドウ目録:\n{window_reports}"
        ),
    )


def semantic_plan_prompt(
    *,
    source_line_count: int,
    regional_reports: str,
    output_language: str,
    last_error: str | None = None,
    page_target_lines: int = 200,
) -> Prompt:
    from .wire import SemanticPlan

    correction = f"\n前回の問題:\n{last_error}\n" if last_error else ""
    return Prompt(
        kind="semantic_plan",
        version=SEED_PLAN_VERSION,
        system=(
            "地域地図から文書全体のWiki分割方針を考える意味計画者である。"
            "厳密なJSON範囲表の作成は次の専用作業者が行うため、ここでは意味判断に集中する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(SemanticPlan)
        ),
        body=(
            f"原文は1-{source_line_count}行である。計画文は{output_language}で書くこと。\n"
            "自然なページ境界、各ページの題名とおおよその行範囲、章構造、境界を選ぶ理由を示す。\n"
            "最重要規則:\n"
            "- 原文の各行は最終的に必ず1個だけのシードに属する。隣のシードへ行を漏らしたり、"
            "同じ行を二重所有させたり、どこにも属させたりしない。\n"
            "- 見出し、導入、注記、表、画像、例を、それが説明する本文・エンティティから切り離さない。\n"
            "- ページの長さを揃えるために関数や項目の途中で切らない。\n"
            "- 空行、見出しだけ、導入文だけ、前後の続きだけを独立ページにしない。見出しは原則として"
            "その直後にある本文へ含める。独立した具体的エンティティ（関数、コマンド、メッセージ型、"
            "定義ファイル、設定項目）は短くても1ページにしてよい。概念・章そのもの・導入文だけの"
            "ページは作らない。意味的に密接に連続する短いエンティティは、1テーマとして"
            "まとめてもよい。\n"
            "- 同一書式で列挙されるAPI関数、コマンド、メッセージ型、設定項目、実質的な"
            "エラー項目は、個別に理解・参照できる単位なら1ページにする。ページが極端に"
            "短くなる場合のみ、同じ種類・目的の隣接項目と意味のある単位へまとめる。\n"
            "- 普通の短い箇条書き、説明の一部、単なる抽象概念は独立ページにしない。\n"
            "- 地域境界を越えて続くエンティティを一つに戻し、重複観察を一件として扱う。\n"
            "- ページ境界は行数ではなくエンティティで決める。各ページは最大"
            f"{2 * page_target_lines}行まで。長い章・まとまりは見出し・エンティティの"
            "境界で分割する（章全体を1ページにしない）。\n"
            f"{correction}\n\n地域地図:\n{regional_reports}"
        ),
    )


def seed_plan_compile_prompt(
    *,
    source_line_count: int,
    semantic_plan: str,
    regional_reports: str,
    output_language: str,
    last_error: str | None = None,
    previous_plan: str = "",
    page_target_lines: int = 200,
) -> Prompt:
    from .wire import SeedPlan

    correction = ""
    if last_error:
        correction = (
            "\n\n前回の構造化結果は無効だった。以下の検証結果を読み、全ページを再提出すること。\n"
            "検証エラーの修正は意味計画より優先する。意味計画の範囲が重複・欠落していても"
            "そのまま写さず、上の絶対条件を満たすよう境界を直すこと。検証エラーがページの"
            "結合を要求した場合は、意味計画のページ数を維持せず、指定範囲をちょうど1ページに"
            "結合すること。同じ結果を繰り返さないこと。\n"
            f"検証エラー: {last_error}\n"
            f"前回の結果:\n{previous_plan or '（取得できず）'}\n"
        )
    return Prompt(
        kind="seed_plan_compile",
        version=SEED_PLAN_COMPILE_VERSION,
        system=(
            "意味計画を、単純で厳密な連続シード範囲表へ変換する専用作業者である。"
            "新しい意味方針を考え直さず、構造と境界の整合性に集中する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(SeedPlan)
        ),
        body=(
            f"原文は1-{source_line_count}行である。タイトル等は{output_language}で書くこと。\n"
            "pagesは原文順に並べ、各ページは一つの連続したsource_start/source_endだけを持つ。\n"
            "絶対条件:\n"
            "- 先頭ページはsource_start=1。\n"
            "- 次ページのsource_startは直前ページのsource_end+1。\n"
            f"- 最終ページはsource_end={source_line_count}。\n"
            "- したがって全行をちょうど1回だけ所有し、漏れ・重複・飛び越しを一切作らない。\n"
            "- fenced code、Markdown表、画像ブロック（<image-unit>/<img>/<embed>/Markdown画像）、一つの具体的エンティティの途中を境界にしない。\n"
            "- 意味計画で独立指定された列挙エンティティを勝手に分断しない。\n"
            "- 空行、見出しだけ、導入文だけ、前後ページの断片だけを1ページにしない。見出しは"
            "通常、その直後の本文と同じページに入れる。\n"
            "- 独立した具体的エンティティは短くても1ページとしてよい。ただし意味計画が"
            "まとめた短い項目を勝手に分断せず、空行や断片だけでページを埋めない。\n"
            "- ページを空にせず、題名と短いsummaryを付ける。\n"
            "- ページは最大"
            f"{2 * page_target_lines}行。超えるページは見出し・エンティティの境界で分ける。\n"
            f"\n\n意味計画:\n{semantic_plan}\n\n地域地図:\n{regional_reports}{correction}"
        ),
    )


def reference_research_prompt(
    *,
    target_number: int,
    target_title: str,
    target_ranges: str,
    target_summary: str,
    references: str,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    """Compare compact summaries of related seed pages."""

    from .wire import ReferenceResearchResult

    correction = (
        "\n\n前回の構造化結果は検証に失敗した。以下を修正し、useful_facts全体を返すこと。\n"
        f"検証エラー: {last_error}\n"
        if last_error
        else ""
    )
    return Prompt(
        kind="reference_research",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの資料調査者である。対象ページと参照ページの要約は"
            "プロンプト内に与えられている。記事や計画は書かず、参照ページから"
            "対象記事へ本当に追加すべき事実だけを構造化JSONで返す。\n\n"
            "JSON形式:\n" + _schema_hint(ReferenceResearchResult)
        ),
        body=(
            f"対象: {target_number:03d} {target_title}（原文 {target_ranges}行）\n"
            f"説明は{output_language}で書くこと。\n\n"
            "判定規則:\n"
            "- 目標は、この対象ページだけでエンティティを完全に理解・使用・実装・運用・"
            "障害対応できる状態にすることである。参照ページ要約に対象エンティティ関連の"
            "情報が残っている限り、網羅的に拾うこと。遠慮して漏らす方が誤り。\n"
            "- 対象ページ要約に既にある事実は追加候補にしない。\n"
            "- 前提、用語定義、関連エンティティとの関係、使用条件、制約、引数や"
            "設定項目の意味、出力メッセージ、返回値、注意、障害事例など、"
            "対象ページを単独で理解、利用、実装、運用、障害対応するために有用な"
            "事実をすべて選ぶ。\n"
            "- 提示された要約にない内容は推測せず、追加候補にしない。\n"
            "- 単なる関数一覧、章番号、同じ説明の言い換え、ナビゲーション用リンクは選ばない。\n"
            "- 各事実のsource_start/source_endは、必ず該当する参照ページに表示された原文範囲内にする。\n"
            "- target_lineには、その事実を本文へ入れるべき対象原文の行番号"
            "（対象原文に表示された番号のうち、最も関係の深い行）を書く。\n"
            "- descriptionには追加する事実、reasonには必要な理由、insertion_pointには"
            "対象記事のどこへ入れるかを書く。\n"
            "- 有用な事実がなければuseful_factsを空にし、"
            "no_useful_information_reasonへ具体的な理由を書く。捏造して水増ししない。\n\n"
            f"--- 対象ページ要約 ---\n{target_summary}\n\n"
            f"{references}{correction}"
        ),
    )


def page_judge_prompt(
    *,
    page_title: str,
    owner_ranges: str,
    numbered_original: str,
    candidate: str,
    output_language: str,
    policy_rules: str = "",
) -> Prompt:
    """Check both important factual coverage and standalone Wiki usefulness."""

    from .wire import PageJudgeResult

    return Prompt(
        kind="page_judge",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの情報欠落と、独立したWiki記事としての完成度を判定する査読者である。"
            "文章を書き直してはならない。"
            "構造化JSONだけを返す。\n"
            "重要な欠落とは、利用者の理解・実装・運用・障害対応に影響する事実、条件、制約、"
            "手順、API挙動、引数、戻り値、警告、例外、エラーの意味である。"
            "章番号、節番号、目次、改訂履歴、装飾、重複文、言い回しなどの些細な差は欠落に含めない。"
            "要約や再構成で意味が保持されていれば欠落ではない。捏造された欠落を報告しない。\n"
            "候補は原文の一部（節）だけを書き直したものである。節の範囲外の情報、"
            "より詳しい説明、一般的な解説、構成の改善を要求してはならない。"
            "「他ページから追加した事実」が候補に含まれていなければ、それは欠落として報告する。\n"
            "候補が自分で作り出した欠陥も defects に報告する。原文どおりの繰り返し、表の全行、"
            "識別子の綴り、コードブロック内の内容は欠陥に数えない。報告するのは候補が"
            "自分で書き足し・重複・破損させた分だけである:\n"
            "- 重複: 同じ文・見出し・表のブロックを候補が複数回出力している。\n"
            "- 創出: 原文にない表ヘッダ、列名、単位、数値、英語ラベル、略語の正式名称、"
            "テンプレート図が混ぜられている。\n"
            "- 言語: 出力言語でない文字や、英語だけの表ラベル・解説が混じっている。\n"
            "- ゴミ: 原文にないHTML断片、変換記号、エラー値、果てしない文字の繰り返しが残っている。\n"
            "- 空: 見出しの直後に内容がない。\n"
            "- 逸脱: この節の原文と無関係な話題に書き換わっている。"
            + (("\n\n" + policy_rules.strip() + "\n\n") if policy_rules.strip() else "\n\n")
            + "JSON形式:\n" + _schema_hint(PageJudgeResult)
        ),
        body=(
            f"ページ: {page_title}\n"
            f"所有する原文範囲: {owner_ranges}行\n"
            f"判定文と欠落説明は{output_language}で書くこと。\n"
            "coverage_scoreは重要情報の保持率として0〜100で採点する。"
            "missing_important_informationには本当に重要な欠落だけを入れ、"
            "原文の正確な開始行と終了行を付ける。"
            "defectsには直すべき点を1項目1文で書き、欠陥がなければ空にする。"
            "coverage_scoreは欠落だけで採点し、defectsの有無で下げない。\n\n"
            f"--- 行番号付き原文 ---\n{numbered_original}\n\n"
            f"--- Wiki候補 ---\n{candidate}"
        ),
    )


# --------------------------------------------------------------------------
# Section writing (plain text output)
# --------------------------------------------------------------------------


def _guidance_block(guidance: Sequence[str]) -> str:
    """What human editors want this page to look like; empty (and absent) without guidance."""

    if not guidance:
        return ""
    return "# 人間の編集者の要望（このページの体裁。事実は変えない）\n- " + "\n- ".join(guidance) + "\n\n"


def section_write_prompt(
    *,
    page_title: str,
    page_summary: str,
    index: int,
    count: int,
    source_start: int,
    source_end: int,
    numbered_section: str,
    facts_text: str,
    image_context: str,
    output_language: str,
    feedback: Sequence[str] = (),
    context: str = "",
    code_identifiers: Sequence[str] = (),
    policy_rules: str = "",
    guidance: Sequence[str] = (),
) -> Prompt:
    """Rewrite one section losslessly; everything needed is in this prompt."""

    identifier_block = ""
    if code_identifiers:
        listing = "、".join(f"`{item}`" for item in code_identifiers[:60])
        identifier_block = (
            "# 必須識別子（一つも落とさない）\n"
            "以下は原文に含まれる識別子・定数・コードトークンである。書き直し後も"
            "それぞれ1回以上、大文字小文字・記号（_ など）を一字も変えずに本文へ"
            "含めること。省略・言い換え・翻訳・要約は禁止。Markdown でイタリックや"
            "太字として解釈されないよう、`バッククォート` で囲んでそのまま書くこと\n"
            f"{listing}\n\n"
        )
    feedback_block = ""
    if feedback:
        feedback_block = (
            "# 前回の出力の不足（必ず全て直す）\n- "
            + "\n- ".join(item.strip() for item in feedback if item.strip())
            + "\n\n"
        )
    context_block = f"{context}\n" if context else ""
    return Prompt(
        kind="section_write",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語技術Wikiの執筆者である。原文の一部（節）を、"
            "単独で読んで理解できるWikiの節に書き直す。\n"
            "- 出力はMarkdown本文だけ。前置き、説明、JSON、出力全体をコードフェンスで囲むことは禁止。\n"
            "- 事実を捏造しない。原文と追加事実にない引数、動作、例、一般論を書かない。\n"
            "- 原文の技術情報を一切落とさない。識別子、定数、数値、単位、"
            "エラーコード、警告文は一字も変えずにそのまま写す。\n"
            "- 出力は今回提示された原文に存在する内容だけに限定する。以前の版の出力を保持せず、"
            "原文から削除された段落、文、表、図、箇条書きは出力から完全に削除する。\n"
            "- コードブロック、表、画像は`[[NEO-IMAGE:...]]`トークンとして与えられる。"
            "トークンを一字も変えず、元と同じ話題の直後に1回だけ置く。トークンの中身を書き起こさない。\n\n"
            + QUALITY_RULES
            + (("\n" + policy_rules.strip() + "\n") if policy_rules.strip() else "")
        ),
        body=(
            "# 対象\n"
            f"- ページ: {page_title}\n"
            f"- ページ全体の要約: {page_summary or '要約なし'}\n"
            f"- この節: {index}/{count}（原文 {source_start}-{source_end}行）\n"
            f"- 本文は{output_language}で書く。\n\n"
            + context_block
            + _guidance_block(guidance)
            + "# 書き方\n"
            "- 見出しは`##`以下を使う。`# `（H1）は書かない。\n"
            "- 原文の見出しは残してよいが、内容が分かる名前に変えてよい。\n"
            "- 段落や箇条書きに整理し、何のための機能か、いつ使うか、何に注意するかが"
            "原文から分かる範囲で伝わるようにする。文の意味、条件、順序に関わる情報は変えない。\n"
            "- 原文の語順や文をなぞる最小限の編集ではなく、この節が単独で完結するWikiページに"
            "なっているかを基準に、導入・解説・手順・注意の構成へ書き直す。ただし原文にない"
            "事実は書かない。\n"
            "- 現在の原文にない旧版の段落や文を復活させてはならない。削除された内容は、"
            "見出し、図、表など残っている要素を壊さずに本文から除去する。\n"
            "- 先頭の行番号は出典を示すためのもので、本文には書かない。\n"
            "- リンク（`[...](...)`）は書かない。「関連ページ」などの一覧も作らない。\n"
            "- 章番号、頁番号、目次など技術的な意味のない体裁だけは省いてよい。\n"
            "- 識別子・定数・エラーコード・`__FILE__`/`__LINE__` 系のマクロは"
            "原文のまま正確に写し、`バッククォート` で囲む（イタリック化を防ぐ）。\n"
            "- トークン一覧（各1回だけ置く）:\n"
            f"{image_context}\n\n"
            + identifier_block
            + "# 他ページから追加する事実\n"
            f"{facts_text}\n"
            "各事実は本文の該当箇所へ自然に組み込む。出典ページ名や原文の行番号を本文へ書かない。\n\n"
            f"{feedback_block}"
            "--- 行番号付き原文（この節） ---\n"
            f"{numbered_section}"
        ),
    )


def intro_prompt(
    *,
    page_title: str,
    page_summary: str,
    body: str,
    output_language: str,
    context: str = "",
    guidance: Sequence[str] = (),
) -> Prompt:
    """One additive lead paragraph written from the finished body only."""

    context_block = f"{context}\n" if context else ""

    return Prompt(
        kind="intro",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語技術Wikiの編集者である。Markdown本文だけを出力する。"
            "前置き、見出し、リンク、箇条書き、コードフェンスは書かない。"
            "現在の本文と要約にない旧版の内容や、原文から削除された内容を復活させない。"
        ),
        body=(
            "次のWiki記事の冒頭に置く導入文を書く。2〜6文で、何のための機能・情報か、"
            "いつ使うか、この記事を読むと何が分かるかを説明する。"
            f"本文にない事実、識別子、数値は書かない。{output_language}で書く。\n\n"
            f"# タイトル\n{page_title}\n\n"
            f"# 要約\n{page_summary or '要約なし'}\n\n"
            + context_block
            + _guidance_block(guidance)
            + f"# 本文\n{body}"
        ),
    )


def incremental_page_edit_prompt(
    *,
    page_title: str,
    current_page: str,
    current_source: str,
    edits: str,
    image_context: str,
    output_language: str,
    feedback: Sequence[str] = (),
    reference_only: bool = False,
    guidance: Sequence[str] = (),
) -> Prompt:
    """Ask the model for exact, page-local patches for a small source diff."""

    from .wire import IncrementalPageEditResult

    feedback_block = ""
    if feedback:
        feedback_block = "# 前回出力の問題（全て修正する）\n- " + "\n- ".join(feedback) + "\n\n"
    return Prompt(
        kind="incremental_page_edit",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは既存の日本語技術Wikiを差分更新する編集者である。"
            "指定された構造化結果だけを返す。\n"
            "- EDITのADD、UPDATE、DELETEを一件も漏らさず厳密に反映する。\n"
            "- DELETEは、表現が言い換えられていても対応する内容を完全に削除し、別表現で復活させない。\n"
            "- UPDATEは旧内容を残さず新内容へ置き換え、ADDは適切な位置へ追加する。\n"
            "- patchesのbeforeには現在のWikiページに一字一句同じ形で1回だけ存在する最小限の範囲を入れ、"
            "afterにはその置換後Markdownを入れる。ADDでも挿入位置の既存文をbeforeに含める。\n"
            "- 必要なら編集箇所の直前・直後も同じpatchに含め、afterで文章を自然につなぎ直す。"
            "それ以外の本文、見出し、表、画像、リンク、ナビゲーションは変更しない。\n"
            "- 各EDIT番号は、patchesのedit_idsかunchanged_edit_idsのどちらか一方に必ず1回含める。"
            "同じ変更がページ内の複数箇所に反映されている場合は、対応する全patchで同じEDIT番号を使う。"
            "複数EDITが同じ連続範囲なら一つのpatchでよい。patch同士のbefore範囲は重複させない。\n"
            "- ページの記述に影響しないEDIT（書式だけの変更、番号の振り直し、OCRの揺れ、"
            "このページが述べていない内容）はunchanged_edit_idsに入れ、patchを作らない。"
            "ページが述べている事実を変えるEDITをunchanged_edit_idsに入れてはならない。\n"
            "- 削除対象が見出し、表、画像の場合は対象そのものを削除する。削除対象でない構造物は保持する。\n"
            "- 現行原文にない事実を追加しない。\n\n"
            "JSON形式:\n" + _schema_hint(IncrementalPageEditResult)
        ),
        body=(
            f"# 対象ページ\n{page_title}\n"
            f"本文は{output_language}で書く。\n\n"
            "# 必須編集\n"
            f"{edits}\n\n"
            "# 現行原文（正しい最新版）\n"
            f"{current_source}\n\n"
            "# 画像プレースホルダー\n"
            f"{image_context or 'なし'}\n"
            "削除対象でないプレースホルダーは一字も変えず1回だけ残す。\n\n"
            + (
                "# 注意\nこのページはこれらの行を所有しておらず、他のページの内容として引用しているだけである。"
                "このページが述べている事実が変わる場合だけ修正し、それ以外のEDITはunchanged_edit_idsに入れる。\n\n"
                if reference_only else ""
            )
            + feedback_block
            + _guidance_block(guidance)
            + "# 現在のWikiページ\n"
            f"{current_page}"
        ),
    )
