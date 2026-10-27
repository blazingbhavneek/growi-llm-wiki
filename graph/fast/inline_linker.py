"""Standalone, reversible linker for already-built fast wiki pages.

This is intentionally not an alternate entry into :mod:`graph.linker`.  It owns a
small content-addressed extraction cache and a manifest of the exact Markdown edits it
made.  The only reader-visible mutation is wrapping text that is already present:

    Entity name -> [Entity name](relative/page.md)

The manifest lets ``sync --fast --link-reset`` undo those edits without touching
source-authored links or the wiki writer's previous/next navigation.  The same reset drops
the cached entity/behaviour facts, so the following ``--link`` re-runs the model pass
instead of replaying them.

Cross-document discovery includes every built document in the selected project,
regardless of its folder; candidate counts and Jev verification still bound the links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.storage import read_json, sha256_text, write_json_atomic, write_text_atomic
from graph.clients.chat import extract_json_from_text

log = logging.getLogger(__name__)

VERSION = "fast-inline-links-v2"
DISCOVERY_VERSION = "project-wide-v1"
PAGE_CHARS = 14_000
FUZZY_THRESHOLD = 90.0
JEV_CHECKPOINT_BATCH = 128
EXTRACTION_ATTEMPTS = 3
# A decision state has to fit inside the scoring model's context, and only the server knows
# its real size: the same hosted endpoint measures 0.67 tokens/character on prose and 1.4 on
# table soup, and answers HTTP 422 "exceeds model context" past 4096 tokens.  So a state is
# sent whole and halved whenever the server rejects it, which scores the text completely
# without ever cutting it; the strongest part of an item wins.
JEV_SPLIT_ROUNDS = 12  # every retry halves a state, so 12 rounds reach a single character
ROLE_QUESTION = (
    "この節は「{name}」そのものを定義・仕様説明していますか？ "
    "単に使用または言及しているだけなら、いいえと答えてください。\n選択肢: はい / いいえ"
)
ENTITY_LINK_QUESTION = (
    "使用側の「{name}」は、定義側で説明されている同じ対象を指していますか？ "
    "名前が似ているだけ、別バージョン、別機能、別組織なら、いいえと答えてください。\n"
    "選択肢: はい / いいえ"
)
BEHAVIOUR_LINK_QUESTION = (
    "対象側の既存語句「{anchor}」から候補ページへリンクすると、対象側の読者が動作・手順・"
    "制約・結果を具体的に理解する助けになりますか？ 単に同じ名前が現れるだけなら、"
    "いいえと答えてください。\n選択肢: はい / いいえ"
)


class FastEntity(BaseModel):
    name: str = ""
    kind: str = ""
    role: Literal["defines", "uses"] = "uses"


class FastBehaviour(BaseModel):
    subject: str = ""
    action: str = ""
    object: str = ""


class FastSectionFacts(BaseModel):
    section: str
    entities: list[FastEntity] = Field(default_factory=list)
    behaviours: list[FastBehaviour] = Field(default_factory=list)


class FastPageFacts(BaseModel):
    sections: list[FastSectionFacts] = Field(default_factory=list)


@dataclass(frozen=True)
class Segment:
    key: str
    heading: str
    text: str


@dataclass
class Page:
    rel: str
    document: str
    folder: str
    title: str
    path: Path
    text: str
    segments: list[Segment]


@dataclass
class EntityOccurrence:
    key: str
    page: Page
    segment: Segment
    name: str
    kind: str
    role: str
    usable: bool = True


@dataclass(frozen=True)
class BehaviourOccurrence:
    key: str
    page: Page
    segment: Segment
    subject: str
    action: str
    object: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name in (self.subject, self.object) if name)


@dataclass(frozen=True)
class Candidate:
    group: str
    source_page: Page
    target_page: Page
    source_text: str
    target_text: str
    anchor: str
    kind: Literal["entity", "behaviour"]
    lexical_score: float


@dataclass(frozen=True)
class PlannedLink:
    source_page: Page
    target_page: Page
    anchor: str
    kind: str
    probability: float


@dataclass
class Extraction:
    entities: list[EntityOccurrence] = field(default_factory=list)
    behaviours: list[BehaviourOccurrence] = field(default_factory=list)
    calls: int = 0
    failures: list[str] = field(default_factory=list)


def _root(project: Any) -> Path:
    return Path(project.metadata) / "cache" / "fast-inline-links"


def _manifest_path(project: Any) -> Path:
    # Ownership and completion must travel with accepted wiki pages through Git candidates.
    return Path(project.metadata) / "fast-links.json"


def _manifest(project: Any) -> dict[str, Any]:
    path = _manifest_path(project)
    if not path.exists():
        path = _root(project) / "manifest.json"  # read old manifests without modifying live state
    return read_json(path, default={})


def _run_path(project: Any) -> Path:
    return _root(project) / "run.json"


def _cache_path(project: Any, key: str) -> Path:
    return _root(project) / "pages" / f"{key}.json"


def _decision_path(project: Any, kind: str, key: str) -> Path:
    return _root(project) / "decisions" / f"{kind}-{key}.json"


def _normal_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(
        char for char in value
        if not char.isspace() and not unicodedata.category(char).startswith(("P", "S"))
    )


def _fuzzy_ratio(a: str, b: str) -> float:
    aa, bb = _normal_name(a), _normal_name(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 100.0
    # FuzzyWuzzy's ratio is SequenceMatcher-based.  Short names are too collision-prone
    # for approximate linking, so they require an exact normalized match above.
    if min(len(aa), len(bb)) < 5:
        return 0.0
    return SequenceMatcher(None, aa, bb, autojunk=False).ratio() * 100.0


def _fence_flags(lines: list[str]) -> list[bool]:
    flags: list[bool] = []
    marker = ""
    for line in lines:
        stripped = line.lstrip()
        opening = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            flags.append(True)
            if stripped.startswith(marker):
                marker = ""
            continue
        flags.append(False)
        if opening:
            marker = opening.group(1)
            flags[-1] = True
    return flags


def _split_large(text: str, limit: int = PAGE_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    paragraphs = re.split(r"(\n[ \t]*\n)", text)
    out: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(paragraph[offset : offset + limit] for offset in range(0, len(paragraph), limit))
        elif current and len(current) + len(paragraph) > limit:
            out.append(current)
            current = paragraph
        else:
            current += paragraph
    if current:
        out.append(current)
    return [part for part in out if part]


def _segments(text: str) -> list[Segment]:
    lines = text.splitlines()
    flags = _fence_flags(lines)
    starts = [index for index, line in enumerate(lines) if not flags[index] and line.startswith("## ")]
    bounds = [0, *starts, len(lines)]
    raw: list[tuple[str, str]] = []
    for start, end in zip(bounds, bounds[1:]):
        body = "\n".join(lines[start:end]).strip("\n")
        if not body.strip():
            continue
        heading = lines[start][3:].strip() if start in starts else ""
        raw.append((heading, body))
    if not raw and text.strip():
        raw = [("", text)]
    result: list[Segment] = []
    for heading, body in raw:
        for part in _split_large(body):
            result.append(Segment(f"S{len(result) + 1}", heading, part))
    return result


def _title(text: str, fallback: str) -> str:
    return next(
        (line[2:].strip() for line in text.splitlines() if line.startswith("# ") and line[2:].strip()),
        fallback,
    )


def _undo_edits(text: str, edits: list[dict[str, Any]]) -> tuple[str, str]:
    trailing = text.endswith("\n")
    lines = text.splitlines()
    for edit in reversed(edits):
        index = int(edit.get("line", -1))
        before = str(edit.get("before", ""))
        after = str(edit.get("after", ""))
        anchor = str(edit.get("anchor", ""))
        url = str(edit.get("url", ""))
        token = f"[{anchor}]({url})"
        if 0 <= index < len(lines) and lines[index] == after:
            lines[index] = before
            continue
        matches = [number for number, line in enumerate(lines) if line == after]
        if len(matches) == 1:
            lines[matches[0]] = before
            continue
        # Preserve unrelated edits made after linking when the marked link itself is
        # still uniquely identifiable on its original line.
        if 0 <= index < len(lines) and token and lines[index].count(token) == 1:
            lines[index] = lines[index].replace(token, anchor, 1)
            continue
        token_lines = [number for number, line in enumerate(lines) if token and token in line]
        if len(token_lines) == 1 and lines[token_lines[0]].count(token) == 1:
            number = token_lines[0]
            lines[number] = lines[number].replace(token, anchor, 1)
            continue
        if before in lines:
            continue
        return text, f"could not locate generated link [{anchor}]({url})"
    cleaned = "\n".join(lines) + ("\n" if trailing else "")
    return cleaned, ""


def _undo_manifest_row(text: str, row: dict[str, Any]) -> tuple[str, str]:
    """Undo an applied row or either side of an interrupted replacement."""

    if row.get("state") != "replacing":
        return _undo_edits(text, list(row.get("edits", [])))
    previous = row.get("previous") if isinstance(row.get("previous"), dict) else None
    digest = sha256_text(text)
    if digest == str(row.get("linked_sha256", "")):
        return _undo_edits(text, list(row.get("edits", [])))
    if previous is not None and digest == str(previous.get("linked_sha256", "")):
        return _undo_manifest_row(text, previous)
    current_edits = list(row.get("edits", []))

    def owns_visible_link(edits: list[dict[str, Any]]) -> bool:
        return any(
            f"[{edit.get('anchor', '')}]({edit.get('url', '')})" in text
            for edit in edits if edit.get("anchor") and edit.get("url")
        )

    if owns_visible_link(current_edits):
        return _undo_edits(text, current_edits)
    # A human edit may change the page hash. Try the new edit identity first, then the
    # previous journal recursively. Repeated interruptions can legitimately leave more
    # than one nested replacement, so recovery must not assume a single prior row.
    cleaned, error = _undo_edits(text, current_edits)
    if not error and (cleaned != text or previous is None):
        return cleaned, ""
    if previous is not None:
        return _undo_manifest_row(text, previous)
    return text, error


def _purge_extractions(project: Any) -> int:
    """Drop the extracted entity/behaviour facts so the next run re-runs the model pass."""

    folder = _root(project) / "pages"
    if not folder.is_dir():
        return 0
    paths = [path for path in folder.glob("*.json") if path.is_file()]
    for path in paths:
        path.unlink()
    return len(paths)


def _clean_existing(project: Any) -> tuple[dict[str, str], list[str]]:
    manifest = _manifest(project)
    cleaned: dict[str, str] = {}
    failures: list[str] = []
    for rel, row in sorted(dict(manifest.get("pages", {})).items()):
        path = Path(project.wiki) / rel
        if not path.exists():
            continue
        text, error = _undo_manifest_row(path.read_text(encoding="utf-8"), row)
        if error:
            failures.append(f"{rel}: {error}")
        else:
            cleaned[rel] = text
    return cleaned, failures


def _inventory(project: Any, cleaned: dict[str, str]) -> list[Page]:
    from graph.config import sanitize_text

    pages: list[Page] = []
    wiki = Path(project.wiki)
    for originals in sorted(wiki.rglob("_planning/pages")):
        document_dir = originals.parent.parent
        document = document_dir.relative_to(wiki).as_posix()
        folder = PurePosixPath(document).parent.as_posix()
        for original in sorted(originals.glob("*.md")):
            live = document_dir / original.name
            if not live.exists():
                continue
            rel = live.relative_to(wiki).as_posix()
            text = cleaned.get(rel, live.read_text(encoding="utf-8"))
            prompt_text = sanitize_text(text)
            pages.append(
                Page(
                    rel,
                    document,
                    folder,
                    _title(prompt_text, live.stem),
                    live,
                    text,
                    _segments(prompt_text),
                )
            )
    return pages


def _groups(page: Page) -> list[list[Segment]]:
    groups: list[list[Segment]] = []
    size = 0
    for segment in page.segments:
        if not groups or size + len(segment.text) > PAGE_CHARS:
            groups.append([])
            size = 0
        groups[-1].append(segment)
        size += len(segment.text)
    return groups


def _messages(page: Page, group: list[Segment], language: str) -> list[Any]:
    schema = FastPageFacts.model_json_schema()
    body = "\n\n".join(
        f"### [{segment.key}] {segment.heading or '(導入)'}\n{segment.text}" for segment in group
    )
    system = (
        "あなたは高速Wikiリンク用の抽出器である。各節を独立に読み、本文中に実在する固有の"
        "エンティティと重要な振る舞いだけをJSONで返す。推論過程や説明文は出力しない。\n"
        "- section は入力のS番号をそのまま返す。\n"
        "- entity.name は本文の表記をそのまま写す。一般語や、その文書内だけで意味が決まる曖昧語は除く。\n"
        "- role は、その節が対象自体を定義・仕様説明するなら defines、使用・言及だけなら uses。\n"
        "- behaviour の subject/object は同じ節の entity.name と完全一致させ、action は短い動詞句にする。\n"
        f"- 出力言語は{language}。\nJSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    human = f"文書: {page.document}\nページ: {page.title}\n\n{body}"
    return [SystemMessage(content=system), HumanMessage(content=human)]


def _retry_messages(
    page: Page,
    group: list[Segment],
    language: str,
    previous_response: str,
    error: str,
) -> list[Any]:
    messages = _messages(page, group, language)
    if previous_response:
        messages.append(AIMessage(content=previous_response))
    messages.append(HumanMessage(content=(
        "前回の出力は無効なJSONでした。以下のエラーを修正し、JSON Schemaに合う"
        "JSONオブジェクトだけを返してください。説明文やMarkdownフェンスは不要です。"
        "打ち切りを防ぐため、不確実な項目を捨てて短くし、各節のentitiesは最大10件、"
        "behavioursは最大8件にしてください。\n"
        f"エラー: {error[:1000]}"
    )))
    return messages


def _validated_facts(raw: Any, page: Page, group: list[Segment]) -> FastPageFacts:
    parsed = raw if isinstance(raw, FastPageFacts) else FastPageFacts.model_validate(raw)
    allowed = {segment.key: segment for segment in group}
    sections: list[FastSectionFacts] = []
    for facts in parsed.sections:
        segment = allowed.get(facts.section)
        if segment is None:
            continue
        entities: list[FastEntity] = []
        names: dict[str, str] = {}
        for entity in facts.entities[:20]:
            name = " ".join(entity.name.split()).strip()
            norm = _normal_name(name)
            if len(name) < 2 or not norm or name not in segment.text or norm in names:
                continue
            names[norm] = name
            entities.append(FastEntity(name=name, kind=" ".join(entity.kind.split())[:80], role=entity.role))
        behaviours: list[FastBehaviour] = []
        seen: set[tuple[str, str, str]] = set()
        for behaviour in facts.behaviours[:15]:
            subject = names.get(_normal_name(behaviour.subject), "")
            obj = names.get(_normal_name(behaviour.object), "") if behaviour.object else ""
            action = " ".join(behaviour.action.split())[:100]
            key = (_normal_name(subject), _normal_name(action), _normal_name(obj))
            if not subject or not action or key in seen:
                continue
            seen.add(key)
            behaviours.append(FastBehaviour(subject=subject, action=action, object=obj))
        sections.append(FastSectionFacts(section=facts.section, entities=entities, behaviours=behaviours))
    return FastPageFacts(sections=sections)


async def _extract(
    project: Any, pages: list[Page], model: Any, settings: Any, *, force: bool,
    on_progress: Any = None,
    refresh_documents: set[str] | None = None,
) -> Extraction:
    from graph.wiki.model import writer_model

    jobs: list[tuple[Page, list[Segment], str, Path]] = []
    facts_by_job: dict[str, FastPageFacts] = {}
    extractor = writer_model(model)
    for page in pages:
        for group in _groups(page):
            identity = VERSION + "\0" + page.document + "\0" + "\0".join(
                segment.key + "\0" + segment.text for segment in group
            )
            key = sha256_text(identity)
            path = _cache_path(project, key)
            if not force and page.document not in (refresh_documents or ()):
                try:
                    cached = read_json(path)
                    facts_by_job[key] = _validated_facts(cached.get("facts", {}), page, group)
                    continue
                except (FileNotFoundError, TypeError, ValueError):
                    pass
            jobs.append((page, group, key, path))

    semaphore = asyncio.Semaphore(max(1, int(getattr(settings, "wiki_linker_concurrency", 0)
                                             or getattr(settings, "concurrency", 1))))
    completed = 0

    async def ask(page: Page, group: list[Segment], key: str, path: Path):
        nonlocal completed
        language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
        facts: FastPageFacts | None = None
        error = ""
        raw = ""
        attempts = 0
        for attempt in range(1, EXTRACTION_ATTEMPTS + 1):
            attempts = attempt
            messages = (
                _messages(page, group, language)
                if not error
                else _retry_messages(page, group, language, raw, error)
            )
            try:
                async with semaphore:
                    raw = await extractor.text(
                        messages,
                        max_output_tokens=8000,
                        temperature=0.5,
                    )
                facts = _validated_facts(extract_json_from_text(raw), page, group)
                error = ""
                break
            except Exception as exc:  # noqa: BLE001 - bounded retry with model feedback
                error = f"{type(exc).__name__}: {exc}"
                if attempt == EXTRACTION_ATTEMPTS:
                    log.exception("ファストリンクのメタデータ生成に失敗: %s", page.rel)
                if attempt < EXTRACTION_ATTEMPTS and on_progress:
                    on_progress({
                        "stage": "fast-link", "step": "page_retry", "page": page.rel,
                        "attempt": attempt, "max_attempts": EXTRACTION_ATTEMPTS,
                        "error": error[:500],
                    })
        completed += 1
        if on_progress:
            on_progress({"stage": "fast-link", "step": "page_described", "page": page.rel,
                         "current": completed, "total": len(jobs)})
            if facts is None:
                on_progress({
                    "stage": "fast-link", "step": "page_skipped", "page": page.rel,
                    "attempts": attempts, "error": error[:500],
                })
        failure = f"{page.rel}: metadata skipped after {attempts} attempts: {error}" if facts is None else ""
        return key, path, facts, failure, attempts

    failures: list[str] = []
    calls = 0
    tasks = [asyncio.create_task(ask(page, group, key, path)) for page, group, key, path in jobs]
    # Calls remain concurrent, but completed results are checkpointed one at a time. A
    # Ctrl-C therefore loses at most the calls still in flight, not the whole page pass.
    for task in asyncio.as_completed(tasks):
        key, path, facts, error, attempts = await task
        calls += attempts
        if error:
            failures.append(error)
            log.warning("%s", error)
            continue
        facts_by_job[key] = facts
        write_json_atomic(path, {"version": VERSION, "facts": facts})

    extraction = Extraction(calls=calls, failures=failures)
    segment_lookup = {(page.rel, segment.key): segment for page in pages for segment in page.segments}
    page_lookup = {page.rel: page for page in pages}
    entity_number = behaviour_number = 0
    for page in pages:
        page_facts: list[FastSectionFacts] = []
        for group in _groups(page):
            identity = VERSION + "\0" + page.document + "\0" + "\0".join(
                segment.key + "\0" + segment.text for segment in group
            )
            cached = facts_by_job.get(sha256_text(identity))
            if cached:
                page_facts.extend(cached.sections)
        for facts in page_facts:
            segment = segment_lookup.get((page.rel, facts.section))
            if segment is None:
                continue
            for entity in facts.entities:
                entity_number += 1
                extraction.entities.append(EntityOccurrence(
                    f"entity-{entity_number}", page_lookup[page.rel], segment,
                    entity.name, entity.kind, entity.role,
                ))
            for behaviour in facts.behaviours:
                behaviour_number += 1
                extraction.behaviours.append(BehaviourOccurrence(
                    f"behaviour-{behaviour_number}", page_lookup[page.rel], segment,
                    behaviour.subject, behaviour.action, behaviour.object,
                ))
    return extraction


def _role_decision_key(item: EntityOccurrence) -> str:
    return sha256_text("\0".join((
        VERSION, "role", item.page.rel, item.segment.heading, item.segment.text, item.name,
    )))


def _too_long(error: BaseException) -> bool:
    """The server's own "this state is too big" answer, in its several spellings."""

    from jev import JevInputTooLong

    return isinstance(error, JevInputTooLong) or "exceeds model context" in str(error)


def _halve_request(request: Any) -> list[Any]:
    """Split the longest state text in two, keeping the question and the other side."""

    from jev import JevRequest

    keys = ["section"] if "section" in request.state else ["target", "candidate"]
    longest = max(keys, key=lambda key: len(request.state[key]["text"]))
    text = request.state[longest]["text"]
    if len(text) < 2:
        return []
    middle = len(text) // 2
    return [
        JevRequest(
            {key: (dict(value, text=part) if key == longest else value)
             for key, value in request.state.items()},
            request.question,
        )
        for part in (text[:middle], text[middle:])
    ]


async def _score_parts(engine: Any, requests: list[list[Any]]) -> tuple[list[list[Any]], int]:
    """Score every request; a state the server calls too long is halved and scored again.

    Results come back grouped per item, exceptions included, so an item with a failing part
    stays undecided instead of answering from the half of the text that happened to fit.
    """

    grouped: list[list[Any]] = [[] for _ in requests]
    pending = [(number, request) for number, group in enumerate(requests) for request in group]
    calls = len(pending)
    for _round in range(JEV_SPLIT_ROUNDS):
        if not pending:
            break
        results = await engine.adecide_batch(
            [request for _number, request in pending], return_exceptions=True,
        )
        retry: list[tuple[int, Any]] = []
        for (number, request), result in zip(pending, results):
            halves = (_halve_request(request)
                      if isinstance(result, BaseException) and _too_long(result) else [])
            if halves:
                retry.extend((number, half) for half in halves)
            else:
                grouped[number].append(result)
        calls += len(retry)
        pending = retry
    return grouped, calls


async def _check_roles(
    project: Any, engine: Any, entities: list[EntityOccurrence], threshold: float,
    *, force: bool, on_progress: Any = None,
    refresh_documents: set[str] | None = None,
) -> tuple[int, int]:
    """Decide the role of every entity; oversized sections score part by part."""

    from jev import JevQuestion, JevRequest

    pending: list[tuple[EntityOccurrence, str]] = []
    requests: list[list[Any]] = []
    for item in entities:
        key = _role_decision_key(item)
        if not force and item.page.document not in (refresh_documents or ()):
            cached = read_json(_decision_path(project, "role", key), default={})
            if cached.get("version") == VERSION and isinstance(cached.get("p_yes"), (int, float)):
                item.role = "defines" if float(cached["p_yes"]) >= threshold else "uses"
                continue
        pending.append((item, key))
        question = JevQuestion(ROLE_QUESTION.format(name=item.name), key=item.key)
        requests.append([
            JevRequest(
                {"section": {"page": item.page.title, "heading": item.segment.heading,
                             "text": item.segment.text}},
                question,
            )
        ])

    failures = 0
    completed = 0
    calls = 0
    for offset in range(0, len(pending), JEV_CHECKPOINT_BATCH):
        batch = pending[offset : offset + JEV_CHECKPOINT_BATCH]
        scored, batch_calls = await _score_parts(engine, requests[offset : offset + JEV_CHECKPOINT_BATCH])
        calls += batch_calls
        for (item, key), results in zip(batch, scored):
            completed += 1
            probabilities = [float(result.p_yes) for result in results
                             if not isinstance(result, BaseException)]
            if len(probabilities) != len(results) or not probabilities:
                item.usable = False
                failures += 1
                continue
            probability = max(probabilities)
            item.role = "defines" if probability >= threshold else "uses"
            write_json_atomic(_decision_path(project, "role", key), {
                "version": VERSION, "p_yes": probability,
            })
        if on_progress:
            on_progress({"stage": "fast-link", "step": "roles_checked",
                         "current": completed, "total": len(pending)})
    return failures, calls


def _same_kind(a: EntityOccurrence, b: EntityOccurrence) -> bool:
    if not a.kind or not b.kind:
        return True
    return _fuzzy_ratio(a.kind, b.kind) >= 80 or _normal_name(a.kind) == _normal_name(b.kind)


def _entity_candidates(entities: list[EntityOccurrence]) -> list[Candidate]:
    definitions = [item for item in entities if item.usable and item.role == "defines"]
    candidates: list[Candidate] = []
    for use in (item for item in entities if item.usable and item.role == "uses"):
        pools = (
            [item for item in definitions if item.page.document == use.page.document and item.page.rel != use.page.rel],
            [item for item in definitions if item.page.document != use.page.document],
        )
        ranked: list[tuple[float, EntityOccurrence]] = []
        for pool in pools:
            ranked = sorted(
                ((score, definition) for definition in pool
                 if _same_kind(use, definition)
                 for score in [_fuzzy_ratio(use.name, definition.name)]
                 if score >= FUZZY_THRESHOLD),
                key=lambda pair: (-pair[0], pair[1].page.rel, pair[1].key),
            )[:3]
            if ranked:
                break
        for score, definition in ranked:
            candidates.append(Candidate(
                use.key, use.page, definition.page, use.segment.text, definition.segment.text,
                use.name, "entity", score,
            ))
    return candidates


def _behaviour_candidates(behaviours: list[BehaviourOccurrence]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for source in behaviours:
        pools = (
            [item for item in behaviours if item.page.document == source.page.document and item.page.rel != source.page.rel],
            [item for item in behaviours if item.page.document != source.page.document],
        )
        ranked: list[tuple[float, BehaviourOccurrence, str]] = []
        for pool in pools:
            for target in pool:
                if _normal_name(source.action) == _normal_name(target.action):
                    continue
                matches = [
                    (score, left)
                    for left in source.names for right in target.names
                    for score in [_fuzzy_ratio(left, right)] if score >= FUZZY_THRESHOLD
                ]
                if matches:
                    score, anchor = max(matches)
                    ranked.append((score, target, anchor))
            ranked.sort(key=lambda item: (-item[0], item[1].page.rel, item[1].key))
            ranked = ranked[:2]
            if ranked:
                break
        for score, target, anchor in ranked:
            candidates.append(Candidate(
                source.key, source.page, target.page, source.segment.text, target.segment.text,
                anchor, "behaviour", score,
            ))
    return candidates


def _candidate_decision_key(candidate: Candidate) -> str:
    return sha256_text("\0".join((
        VERSION, "candidate", candidate.kind, candidate.source_page.rel,
        candidate.target_page.rel, candidate.anchor, candidate.source_text,
        candidate.target_text,
    )))


async def _judge_candidates(
    project: Any, engine: Any, candidates: list[Candidate], threshold: float,
    *, force: bool, on_progress: Any = None,
) -> tuple[list[PlannedLink], int, int]:
    """Judge every candidate; both sides are scored whole, one part pair per request."""

    from jev import JevQuestion, JevRequest

    probabilities: dict[str, float] = {}
    pending: list[tuple[Candidate, str]] = []
    requests: list[list[Any]] = []
    for number, candidate in enumerate(candidates):
        key = _candidate_decision_key(candidate)
        if not force:
            cached = read_json(_decision_path(project, "edge", key), default={})
            if cached.get("version") == VERSION and isinstance(cached.get("p_yes"), (int, float)):
                probabilities[key] = float(cached["p_yes"])
                continue
        wording = ENTITY_LINK_QUESTION if candidate.kind == "entity" else BEHAVIOUR_LINK_QUESTION
        question = JevQuestion(
            wording.format(name=candidate.anchor, anchor=candidate.anchor), key=str(number),
        )
        pending.append((candidate, key))
        requests.append([
            JevRequest(
                {"target": {"page": candidate.source_page.title, "text": candidate.source_text},
                 "candidate": {"page": candidate.target_page.title,
                               "text": candidate.target_text}},
                question,
            )
        ])

    best: dict[str, tuple[float, Candidate]] = {}
    failures = 0
    completed = 0
    calls = 0
    for offset in range(0, len(pending), JEV_CHECKPOINT_BATCH):
        batch = pending[offset : offset + JEV_CHECKPOINT_BATCH]
        scored, batch_calls = await _score_parts(engine, requests[offset : offset + JEV_CHECKPOINT_BATCH])
        calls += batch_calls
        for (candidate, key), results in zip(batch, scored):
            completed += 1
            scored_parts = [float(result.p_yes) for result in results
                            if not isinstance(result, BaseException)]
            if len(scored_parts) != len(results) or not scored_parts:
                failures += 1
                continue
            probability = max(scored_parts)
            probabilities[key] = probability
            write_json_atomic(_decision_path(project, "edge", key), {
                "version": VERSION, "p_yes": probability,
            })
        if on_progress:
            on_progress({"stage": "fast-link", "step": "candidates_checked",
                         "current": completed, "total": len(pending)})

    for candidate in candidates:
        probability = probabilities.get(_candidate_decision_key(candidate))
        if probability is None:
            continue
        if probability < threshold:
            continue
        previous = best.get(candidate.group)
        if previous is None or (probability, candidate.lexical_score) > (previous[0], previous[1].lexical_score):
            best[candidate.group] = (probability, candidate)
    plans = [
        PlannedLink(candidate.source_page, candidate.target_page, candidate.anchor,
                    candidate.kind, probability)
        for probability, candidate in best.values()
    ]
    return plans, failures, calls


_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\([^)]+\)")
_INLINE_CODE = re.compile(r"`[^`]*`")


def _ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _wrap_once(text: str, anchor: str, url: str) -> tuple[str, dict[str, Any] | None]:
    if len(anchor) < 2 or any(char in anchor for char in "[]\n"):
        return text, None
    trailing = text.endswith("\n")
    lines = text.splitlines()
    flags = _fence_flags(lines)
    for number, line in enumerate(lines):
        stripped = line.lstrip()
        if (
            flags[number]
            or stripped.startswith(("#", "|", "<", "[[NEO-IMAGE", "!["))
            or "前のページ:" in line
            or "次のページ:" in line
        ):
            continue
        blocked = [match.span() for regex in (_MARKDOWN_LINK, _INLINE_CODE) for match in regex.finditer(line)]
        at = line.find(anchor)
        while at >= 0:
            end = at + len(anchor)
            left_ok = not _ascii_word(anchor[0]) or at == 0 or not _ascii_word(line[at - 1])
            right_ok = not _ascii_word(anchor[-1]) or end == len(line) or not _ascii_word(line[end])
            if left_ok and right_ok and not any(start <= at < stop for start, stop in blocked):
                revised = line[:at] + f"[{anchor}]({url})" + line[end:]
                lines[number] = revised
                output = "\n".join(lines) + ("\n" if trailing else "")
                return output, {"line": number, "before": line, "after": revised,
                                "anchor": anchor, "url": url}
            at = line.find(anchor, at + len(anchor))
    return text, None


def _apply_links(project: Any, pages: list[Page], plans: list[PlannedLink], settings: Any) -> dict[str, Any]:
    by_page: dict[str, list[PlannedLink]] = {}
    seen: set[tuple[str, str, str]] = set()
    for plan in sorted(plans, key=lambda item: (
        item.source_page.rel, 0 if item.kind == "entity" else 1,
        -item.probability, -len(item.anchor), item.target_page.rel,
    )):
        key = (plan.source_page.rel, plan.target_page.rel, _normal_name(plan.anchor))
        if key in seen:
            continue
        seen.add(key)
        by_page.setdefault(plan.source_page.rel, []).append(plan)

    limit = int(getattr(settings, "wiki_linker_inline_max_neo", 3))
    manifest = _manifest(project)
    manifest_pages: dict[str, Any] = dict(manifest.get("pages", {}))
    writes = 0
    links = 0
    for page in pages:
        text = page.text
        edits: list[dict[str, Any]] = []
        for plan in by_page.get(page.rel, [])[:limit]:
            url = posixpath.relpath(plan.target_page.rel, posixpath.dirname(page.rel) or ".")
            text, edit = _wrap_once(text, plan.anchor, url)
            if edit is not None:
                edit["kind"] = plan.kind
                edit["target"] = plan.target_page.rel
                edit["probability"] = plan.probability
                edits.append(edit)
                links += 1
        # ``page.text`` has the previous fast-link manifest removed in memory.  Write
        # it even when this run selected no replacement links, otherwise links from the
        # preceding run would remain visible after their manifest entry was dropped.
        current = page.path.read_text(encoding="utf-8")
        previous = manifest_pages.get(page.rel)
        new_row = (
            {
                "base_sha256": sha256_text(page.text),
                "linked_sha256": sha256_text(text),
                "edits": edits,
            }
            if edits else None
        )
        if current != text:
            # Write-ahead ownership: whether interruption happens immediately before or
            # after the page write, the next --continue or --link-reset can identify and
            # undo the correct side of the replacement.
            manifest_pages[page.rel] = {
                "state": "replacing",
                "base_sha256": sha256_text(page.text),
                "linked_sha256": sha256_text(text),
                "edits": edits,
                **({"previous": previous} if isinstance(previous, dict) else {}),
            }
            write_json_atomic(_manifest_path(project), {
                "schema_version": 1, "version": VERSION,
                "status": "applying", "pages": manifest_pages,
                "documents": manifest.get("documents", {}),
            })
            write_text_atomic(page.path, text)
            writes += 1
        if new_row is not None:
            manifest_pages[page.rel] = new_row
        else:
            manifest_pages.pop(page.rel, None)
        if current != text or previous is not None or new_row is not None:
            write_json_atomic(_manifest_path(project), {
                "schema_version": 1, "version": VERSION,
                "status": "applying", "pages": manifest_pages,
                "documents": manifest.get("documents", {}),
            })

    write_json_atomic(_manifest_path(project), {
        "schema_version": 1,
        "version": VERSION,
        "status": "complete",
        "pages": manifest_pages,
        "documents": manifest.get("documents", {}),
    })
    return {"pages": writes, "links": links}


def _checkpoint(project: Any, status: str, **details: Any) -> None:
    write_json_atomic(_run_path(project), {
        **read_json(_run_path(project), default={}),
        "schema_version": 1,
        "version": VERSION,
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **details,
    })


async def _link_async(
    project: Any, settings: Any, model: Any, engine: Any, *, force: bool,
    continue_run: bool = False, on_progress: Any = None,
    output_project: Any = None, only: list[str] | None = None,
) -> dict[str, Any]:
    previous_run = read_json(_run_path(project), default={})
    resumed = bool(
        continue_run
        and previous_run.get("version") == VERSION
        and previous_run.get("status") not in {None, "complete"}
    )
    _checkpoint(project, "inventory", continued=resumed)
    cleaned, cleanup_failures = _clean_existing(project)
    if cleanup_failures:
        _checkpoint(project, "failed", continued=resumed, failures=cleanup_failures)
        return {"done": [], "failures": cleanup_failures}
    pages = _inventory(project, cleaned)
    if not pages:
        _checkpoint(project, "complete", continued=resumed, pages=0, links=0)
        return {"done": [{"status": "up-to-date", "pages": 0, "links": 0}], "failures": []}
    from publisher.ledger import load_ledger
    from publisher.pipeline import _content_hash

    manifest = _manifest(project)
    documents = {page.document for page in pages}
    scope = documents if only is None else {
        project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only
    } & documents
    fingerprints = {doc: _content_hash(Path(project.wiki) / doc) for doc in documents}
    completed = dict(manifest.get("documents", {}))
    # Adopt already-published legacy links only when their recorded bytes still match.
    ledger = load_ledger(Path(project.metadata) / "pipeline.json")
    legacy_docs = {str(Path(rel).parent.as_posix()) for rel in manifest.get("pages", {})}
    if manifest.get("version") == VERSION and manifest.get("status") == "complete":
        for doc in (legacy_docs & documents) - set(completed):
            rows = {rel: row for rel, row in manifest["pages"].items() if str(Path(rel).parent.as_posix()) == doc}
            if (ledger.published_documents.get(doc, {}).get("content_sha256") == fingerprints[doc]
                    and all((Path(project.wiki) / rel).is_file()
                            and sha256_text((Path(project.wiki) / rel).read_text(encoding="utf-8")) == row.get("linked_sha256")
                            for rel, row in rows.items())):
                completed[doc] = {"version": VERSION, "status": "complete", "content_sha256": fingerprints[doc]}
    pending = {doc for doc in scope if force or completed.get(doc, {}).get("version") != VERSION
               or completed.get(doc, {}).get("discovery_version") != DISCOVERY_VERSION
               or completed.get(doc, {}).get("content_sha256") != fingerprints[doc]}
    if not pending:
        if completed != manifest.get("documents", {}):
            write_json_atomic(_manifest_path(output_project or project), {**manifest, "documents": completed})
        return {"done": [{"status": "up-to-date"}], "failures": [], "linked_documents": sorted(scope)}
    # Cache keys include the passage text, so normal runs only analyse changed inputs.
    # Explicit --force still refreshes extraction, roles, and link decisions.
    _checkpoint(project, "extracting", continued=resumed, pages=len(pages),
                failures=[],
                documents={doc: {"base_sha256": fingerprints[doc], "status": "pending"} for doc in pending})
    extraction = await _extract(project, pages, model, settings, force=force,
                                on_progress=on_progress)
    skipped_extractions = len(extraction.failures)
    if extraction.failures:
        _checkpoint(project, "failed", failures=extraction.failures)
        return {"done": [], "failures": extraction.failures}
    _checkpoint(
        project, "checking_roles", continued=resumed,
        entities=len(extraction.entities), skipped_extractions=skipped_extractions,
    )
    role_failures, role_calls = await _check_roles(
        project, engine, extraction.entities,
        float(getattr(settings, "wiki_linker_role_threshold", 0.5)),
        force=force, on_progress=on_progress,
    )
    if role_failures:
        failures = [f"ファストリンク: エンティティのチェックに失敗（{role_failures}件）"]
        _checkpoint(project, "failed", failures=failures)
        return {"done": [], "failures": failures}
    candidates = [item for item in _entity_candidates(extraction.entities) + _behaviour_candidates(extraction.behaviours)
                  if item.source_page.document in pending]
    _checkpoint(project, "checking_candidates", continued=resumed, candidates=len(candidates))
    plans, judge_failures, judge_calls = await _judge_candidates(
        project, engine, candidates,
        float(getattr(settings, "wiki_linker_verify_threshold", 0.7)),
        force=force, on_progress=on_progress,
    )
    if judge_failures:
        failures = [f"ファストリンク: リンク候補のチェックに失敗（{judge_failures}件）"]
        _checkpoint(project, "failed", failures=failures)
        return {"done": [], "failures": failures}
    _checkpoint(project, "applying", continued=resumed, planned_links=len(plans))
    output = output_project or project
    manifest["documents"] = completed
    write_json_atomic(_manifest_path(output), manifest)
    output_pages = [replace(page, path=Path(output.wiki) / page.rel) for page in pages if page.document in pending]
    changed = _apply_links(output, output_pages, plans, settings)
    accepted = _manifest(output)
    accepted["documents"] = {doc: row for doc, row in completed.items() if doc in documents}
    for doc in pending:
        accepted["documents"][doc] = {"version": VERSION, "status": "complete",
                                      "discovery_version": DISCOVERY_VERSION,
                                      "content_sha256": _content_hash(Path(output.wiki) / doc)}
    write_json_atomic(_manifest_path(output), accepted)
    _checkpoint(
        project, "prepared" if output_project is not None else "complete", continued=resumed,
        skipped_extractions=skipped_extractions, **changed,
    )
    return {
        "done": [{"status": "fast-linked", **changed, "documents": len(pending),
                  "continued": resumed, "llm_calls": extraction.calls,
                  "skipped_extractions": skipped_extractions,
                  "jev_requests": role_calls + judge_calls,
                  "jev_failures": role_failures + judge_failures}],
        "failures": [],
        "linked_documents": sorted(scope),
    }


def link_project(
    settings: Any, *, force: bool = False, continue_run: bool = False,
    on_progress: Any = None,
    output_project: Any = None, only: list[str] | None = None,
) -> dict[str, Any]:
    """Add reversible inline links to the current local wiki tree; never publish."""

    from graph.common.async_tools import run_async_blocking
    from common.policy import policy_of
    from graph.workspace.project import open_project
    from graph.workspace.writer import wiki_config
    from jev import get_engine_for

    project = open_project(settings)
    from publisher.pipeline import _content_hash

    documents = {path.parent.parent.relative_to(project.wiki).as_posix()
                 for path in project.wiki.rglob("_planning/pages")
                 if any(path.parent.parent.glob("*.md"))}
    if only is not None:
        documents &= {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
    completed = _manifest(project).get("documents", {})
    if not force and all(completed.get(doc, {}).get("version") == VERSION
                         and completed[doc].get("discovery_version") == DISCOVERY_VERSION
                         and completed[doc].get("content_sha256") == _content_hash(project.wiki / doc)
                         for doc in documents):
        return {"done": [{"status": "up-to-date"}], "failures": [], "linked_documents": sorted(documents)}
    config = wiki_config(settings, run_dir=_root(project) / "work").model_copy(
        update={"text_thinking": False, "temperature": 0.5},
    )
    model = policy_of(config).model_port(config)
    try:
        engine = get_engine_for(settings)
    except Exception as exc:
        log.exception("ファストリンクのJevチェックに失敗")
        return {"done": [], "failures": [f"fast link: Jev unavailable: {type(exc).__name__}: {exc}"]}
    return run_async_blocking(
        _link_async(
            project, settings, model, engine, force=force,
            continue_run=continue_run, on_progress=on_progress,
            output_project=output_project, only=only,
        )
    )


def reset_project(settings: Any) -> dict[str, Any]:
    """Undo recorded links and drop the facts they were extracted from; never touch GROWI."""

    from graph.workspace.project import open_project

    project = open_project(settings)
    purged = _purge_extractions(project)
    manifest = _manifest(project)
    pages = dict(manifest.get("pages", {}))
    if not pages:
        if manifest.get("documents"):
            write_json_atomic(_manifest_path(project), {**manifest, "documents": {}})
        return {"done": [{"status": "up-to-date", "pages": 0, "links_removed": 0,
                          "extractions_removed": purged}], "failures": []}
    writes: list[tuple[Path, str]] = []
    failures: list[str] = []
    removed = 0
    unresolved: dict[str, Any] = {}
    for rel, row in sorted(pages.items()):
        path = Path(project.wiki) / rel
        if not path.exists():
            continue
        text, error = _undo_manifest_row(path.read_text(encoding="utf-8"), row)
        if error:
            failures.append(f"{rel}: {error}")
            unresolved[rel] = row
            continue
        writes.append((path, text))
        removed += len(row.get("edits", []))
    for path, text in writes:
        write_text_atomic(path, text)
    write_json_atomic(_manifest_path(project), {
        "schema_version": 1,
        "version": VERSION,
        "status": "complete" if not unresolved else "reset-partial",
        "pages": unresolved,
    })
    return {
        "done": [{"status": "fast-links-reset", "pages": len(writes), "links_removed": removed,
                  "extractions_removed": purged}],
        "failures": failures,
    }


__all__ = ["link_project", "reset_project"]
