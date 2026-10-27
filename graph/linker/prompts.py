from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from graph.common.prompts import EDGE_PROMPT, KEYWORD_PROMPT, SUMMARY_PROMPT
from graph.wiki.prompts import COMMON_RULES, Prompt, _language_rule

from .wire import ChunkMeta, NeoEdgeSuggestions

CHUNK_META_VERSION = "wiki-chunk-meta-8"
EDGE_VERSION_LEGACY = "wiki-link-edge-legacy-4"
EDGE_VERSION_NEO = "wiki-link-edge-neo-4"
EDGE_VERSION_JEV = "wiki-link-edge-jev-5"
REFERENCE_PLAN_VERSION = "wiki-link-reference-plan-2"

JEV_ROLE_QUESTION = "この節は「{name}」を定義・仕様説明していますか？ 単に使っている・言及しているだけの場合は いいえ と答えてください。\n選択肢: はい / いいえ"
JEV_ALIAS_QUESTION = "AとBは、それぞれの説明から見て同じ対象を指していますか？ 名前が似ているだけで別のものなら いいえ と答えてください。\n選択肢: はい / いいえ"
JEV_MAIN_DEFINITION_QUESTION = "この節は「{name}」の主要な定義（最も詳しく完全な説明）ですか？\n選択肢: はい / いいえ"
JEV_SCREEN_QUESTION = "候補の節を読むことは、対象の節の読者が内容を理解・実行するのに具体的に役立ちますか？\n選択肢: はい / いいえ"
JEV_VERIFY_QUESTION = "候補の節は、対象の節の読者が内容を理解・実行するために読むべき具体的な情報（前提・結果・制約・代替・同じ対象の別の側面など）を含んでいますか？ 同じ語が出てくるだけ、一般的な関連があるだけなら いいえ と答えてください。\n選択肢: はい / いいえ"
JEV_CURATE_QUESTION = "このページの読者に、関連資料「{peer_title} › {peer_heading}」（{peer_summary}）を案内する価値がありますか？\n選択肢: はい / いいえ"
JEV_DOCUMENT_RELATION_QUESTION = "この2つの文書は、読者が相互に参照すべき関係（同じ対象の前提・続き・詳細・版違いなど）にありますか？\n選択肢: はい / いいえ"

READER_SUMMARY_RULES = (
    "\n- label は内部処理専用であり、読者には表示されない。"
    "\n- 対象ごとに、本当に有用な参照だけを最大2件選ぶ。"
    "\n- 単なる類似、同じ単語の出現、同じヘッダー、漠然とした関連性は採用しない。"
    "\n- label は defines, defined-by, uses, used-by, requires, required-by, prerequisite, prerequisite-for, "
    "implements, implemented-by, configures, configured-by, triggers, triggered-by, consequence, "
    "consequence-of, constraint, constrains, alternative, alternative-to, contradicts, example-of, has-example のいずれかにする。"
    "\n- summary はリンク先を参照すると何が分かるのかを、自然な日本語の1文で説明する。"
    "\n- 『新しいノード』『対象ノード』『候補ノード』『チャンク』、内部ID、英語の関係ラベルをsummaryに書かない。"
    "\n- 『関連しているため』だけの説明ではなく、本文中の具体的な機能・条件・手順・相違点を書く。"
)

NEO_EDGE_PROMPT = (
    "あなたはWiki横断リンクの判定者である。対象チャンクと、エンティティの振る舞いグラフを"
    "1〜3ホップたどって見つかった候補チャンクが渡される。各候補には、どのエンティティを"
    "経由して到達したか（via）と、候補側の entities / behaviours が付いている。\n"
    "ルール:\n"
    "- 与えられた候補IDのみを使う。\n"
    "- リンクを提案するのは、読者が対象チャンクを理解・実行する上で候補が具体的に役立つ"
    "場合だけ（前提、結果、制約、代替、矛盾、同じエンティティの別の振る舞い）。\n"
    "- 単に同じ語が出てくるだけの候補は提案しない。候補が少なくても無理につなげない。\n"
    "- label は列挙値から選ぶ。summary は、どのエンティティのどの振る舞いでつながるかを"
    "1文で書く。"
    + READER_SUMMARY_RULES
)


def chunk_meta_prompt(
    *, page_title: str, heading: str, document: str, text: str,
    output_language: str, known_entities: list[dict[str, str]] | None = None,
) -> Prompt:
    registry = (
        "- 次の既知エンティティと同一なら、その name を再利用して表記揺れや重複を増やさない。\n"
        "- 後の記述から既知エンティティが誤り・複合名だったと判明した場合、正しい各 entity の"
        "replaces に置換前の name を入れる。例: A-B が別々の A と B だと判明したら、A と B の"
        "両方に replaces=[\"A-B\"] を付ける。単なる再言及では replaces を空にする。\n"
        f"- 文書先頭からここまでの既知エンティティ: {json.dumps(known_entities, ensure_ascii=False)}\n"
        if known_entities else ""
    )
    body = (
        f"# 対象\n- 文書: {document}\n- ページ: {page_title}\n"
        f"- 節: {heading or '(導入)'}\n- 出力は{output_language}で書く。\n\n"
        f"# summary\n{SUMMARY_PROMPT}\n\n# keywords\n{KEYWORD_PROMPT}\n\n"
        "# entity\nこの節の主要なエンティティまたはトピックを1つ、本文の表記のまま短く書く。\n\n"
        "# entities\n他の文書や節にある同じ名前と照合してリンクを作るための名前である。"
        "この節に登場する固有の名前（人物、組織、役割、製品、API、関数、パラメータ、エラーコード、"
        "文書名、規則名、手順名、概念、場所）のうち、重要なものを最大15個列挙する。\n"
        "- name は本文に書かれている表記をそのまま写す。\n"
        "- この文書を離れても同じ対象を指す名前だけを選ぶ。文書の中でしか指す先が決まらない"
        "呼び方や、どの文書にも現れる一般的な語は、照合すると無関係な文書を結び付けるので含めない。\n"
        "- kind は名前の種類（関数、パラメータ、エラーコード、製品、組織、手順、概念など）を短く必ず書く。\n"
        "- role は、この節がその名前を定義・仕様説明していて、その名前を調べる読者が読むべき節なら "
        "defines、単に使用・言及しているだけなら uses。\n"
        f"{registry}\n# behaviours\n"
        "この節で「誰が／何が、何をしているか」のうち重要なものを最大10個列挙する。subject と object は entities の"
        "name と一致させる。object が無い場合は空文字。action は短い動詞句。\n\n"
        "以下の4項目は検索専用である。リンク判定には使わない。\n\n"
        "# kind（節全体）\nこの節が「どんな種類の情報」を含むかを1文で書く。対象名だけでなく、含まれる情報の種類を並べる。"
        "例: 「mpf_mfs_open」ではなく「mpf_mfs_open の API リファレンス、引数一覧、戻り値、使用例」。\n\n"
        "# points\nこの節が扱う内容を箇条書きで漏れなく列挙する。値や細部ではなく「何が書かれているか」を一般的な言葉で書く"
        "（例: 「オープンモードの種類」「エラー時の戻り値」）。本文が少し改訂されても正しいままの書き方にする。\n\n"
        "# search_terms\n検索用の語句を上限なしで漏れなく列挙する。本文に出てくる関数名・API名・パラメータ名・引数名・"
        "定数・マクロ・エラーコード・設定項目・コマンド・ファイル名・画面名・製品名・値を、表や箇条書きの中のものも含めて"
        "本文の表記のまま全て入れる。さらに、読者がこの節を探すときに入力しそうな言い換え・同義語・日本語と英語の両方の"
        "呼び方も加える。重複は入れない。\n\n"
        "# claims\n本文が直接裏付ける短い事実を1文1事実で漏れなく列挙する。値・条件・例外・手順・対象・結果・順序を含め、"
        "本文に無い推論は書かない。\n\n"
        "# bridge_probe\nこの節が関係しうる、より広い分野・概念・仕組み（例: 排他制御、資源の競合、初期化順序）を1〜2文で書く。"
        "本文の言い換えではなく、つながる文脈を書く。確信が無ければ本文の主題を短く書く。\n\n"
        f"--- 本文 ---\n{text}"
    )
    schema = ChunkMeta.model_json_schema()
    schema["properties"].pop("role_judge", None)
    return Prompt(
        kind="chunk_meta",
        version=CHUNK_META_VERSION,
        system=f"あなたはWiki横断リンク用のチャンク記述者である。与えられたWikiページの一節を読み、検索とリンク判定に必要な情報だけを構造化して返す。\n{COMMON_RULES}\n{_language_rule(output_language)}\nJSON形式:\n{json.dumps(schema, ensure_ascii=False)}",
        body=body,
    )


def edge_tiebreak_messages(target: dict[str, Any], candidate: dict[str, Any]) -> list[Any]:
    """Yes/no second opinion on a link Jev was unsure about; same criterion as its verify step."""
    def section(item: dict[str, Any]) -> str:
        return f"ページ: {item['title']}\n節: {item['heading'] or '(導入)'}\n{item['text']}"
    return [
        SystemMessage(content="あなたはWikiの編集者である。2つの節を読み、対象の節から候補の節へリンクを張るべきかを判断する。"
                              "最後の行に「はい」か「いいえ」だけを書く。"),
        HumanMessage(content=f"--- 対象の節 ---\n{section(target)}\n\n--- 候補の節 ---\n{section(candidate)}\n\n"
                             + JEV_VERIFY_QUESTION),
    ]


def legacy_edge_messages(target: dict[str, Any], candidates: list[dict[str, Any]], *, output_language: str = "") -> list[Any]:
    payload = {"new_node": target, "candidates": candidates}
    system = EDGE_PROMPT + READER_SUMMARY_RULES + (f"\n- summary は{output_language}で書く。" if output_language else "")
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, ensure_ascii=False))]


def neo_edge_messages(target: dict[str, Any], candidates: list[dict[str, Any]], *, output_language: str = "") -> list[Any]:
    payload = {"target": target, "candidates": candidates}
    system = NEO_EDGE_PROMPT + (f"\n- summary は{output_language}で書く。" if output_language else "")
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, ensure_ascii=False))]


def reference_plan_messages(
    *, page: dict[str, Any], current: list[dict[str, Any]], candidates: list[dict[str, Any]],
    inline_limit: int, footer_limit: int, output_language: str = "Japanese (日本語)",
    behaviour_only: bool = False,
) -> list[Any]:
    placement_rule = (
        "- 候補は振る舞い上の関連だけである。原則 footer にし、本文中の既存フレーズからリンク先の"
        "振る舞いを直接かつ強く予測できる場合だけ inline にする。\n"
        if behaviour_only else ""
    )
    system = f"""あなたはWikiの編集者である。既存の参照と新しい候補を一緒に比較し、読者に残す参照を選ぶ。
重要度は機械的な関係ラベルではなく、対象ページを理解・操作する読者にとっての具体的な有用性で判断する。

ルール:
- 出力には候補にある edge_id だけを使う。既存参照も、より有用な新規候補があれば外してよい。
- 単なる語句の類似、同じ分類、曖昧な関連は採用しない。一方、完璧な候補だけに絞りすぎない。「必須」級がなくても、理解に具体的な補助となる候補が複数あれば、その中の上位数件は残す。
- 上限は本文内 {inline_limit} 件、末尾の関連資料 {footer_limit} 件。これは目標件数ではない。通常は少数の有用な参照で十分である。
- 本文を読む流れの中で自然に参照すべきものは inline、それ以外の補足資料は footer にする。
- inline の anchor は対象ページ本文に実在する、意味の明確な語句をそのまま返す。適切な語句がなければ footer にする。
{placement_rule}\
- 同じリンク先は1件だけ選ぶ。
- summary はリンク先で何を確認できるかを、読者向けの自然な日本語1文で書く。英語の関係ラベル、内部ID、『ノード』『チャンク』などの内部用語は書かない。
- 出力は {output_language} とする。候補が読者に何も追加しない場合に限り0件でよい。"""
    payload = {"page": page, "current_references": current, "candidates": candidates}
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, ensure_ascii=False))]


__all__ = [
    "CHUNK_META_VERSION", "EDGE_VERSION_JEV", "EDGE_VERSION_LEGACY", "EDGE_VERSION_NEO", "NEO_EDGE_PROMPT", "READER_SUMMARY_RULES",
    "JEV_ALIAS_QUESTION", "JEV_CURATE_QUESTION", "JEV_DOCUMENT_RELATION_QUESTION", "JEV_MAIN_DEFINITION_QUESTION",
    "JEV_ROLE_QUESTION", "JEV_SCREEN_QUESTION", "JEV_VERIFY_QUESTION",
    "REFERENCE_PLAN_VERSION", "chunk_meta_prompt", "legacy_edge_messages", "neo_edge_messages",
    "reference_plan_messages",
]
