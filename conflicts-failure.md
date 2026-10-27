# conflicts-failure.md — failure modes from the `test_conf` Round 2 run (2026-10-20)

Read-only investigation. Four code-reading agents plus the run owner traced the code; **nothing in
`publisher/`, `graph/`, `data/` or GROWI was modified** to produce this document. Line numbers are
from the working tree at the time of writing.

Page numbers (`002`, `019`…) refer to `data/test_conf/wiki/(社外秘)制御機能仕様書【3章】.docx/`.
`QA-rN*` strings are human test markers, not customer text.

## 0. The run, in one paragraph

Round 1 = 11 pages edited in the GROWI UI (9 captured, 2 controls correctly ignored): **clean, zero
notes**. Round 2 = source-only change (small: 5 single-character verdict flips, 2 constants, 3 added
rows, 4 deleted bullets, 1 reword, 1 moved paragraph, 1 swapped pair, 1 sub-item, 1 footnote ref),
with `対象ページ=0 取り込み=0` for the UI side. Expected: silent landing, zero notes, ~9 pages
touched. Actual: **all 28 pages deleted and regenerated, 29 pages published, a new `029-付録.md`
created, 4 human blocks displaced off their pages, 4 pages' line 1 rewritten, 3 conflict notes on a
one-sided round, and the run's own report said `touched: [], human_edits_overwritten: []`.**

## 1. TRIGGER (fix this first — it is not a merge bug)

`REWRITE_PROMPT_VERSION` is `wiki-sections-ja-6` in the working tree, **uncommitted**, mtime 10:49
(`graph/wiki/config.py:14`, `git diff` shows `ja-5 → ja-6`).

Chain: the pre-run state stored `rewrite_version: "wiki-sections-ja-5"` on all 28 pages with matching
`content_sha256` (`git show aaab8a1:metadata/state/…/state/pages/*.json`) → the checkpoint test at
`graph/wiki/pipeline.py:1198` failed for **every** page → `graph/wiki/pipeline.py:1701` **deleted**
each page and its sidecar → `SEED_PLAN_VERSION` was untouched, so `seed resumed` succeeded and the
run still *looked* incremental → one page failed patching (`graph/workspace/writer.py:681`
`patch-escalated`) → `build_wiki_output(resume=True, require_resume=True)`
(`graph/workspace/writer.py:687`) had nothing resumable and regenerated 28/28 → `rebuild` is derived
only from `decision.tier` (`graph/workspace/writer.py:715`), so the log said `"rebuild":
"incremental", "tier": 2` for a de-facto full rebuild.

`decide_update` itself behaved: `tier 1, reason "patch", hunks 22, patch_pages 9, regenerate_pages 0`.
Real thresholds are `PATCH_MAX_CHURN` (`graph/wiki/incremental.py:33`), `FULL_MIN_REGEN_SHARE` (:34),
`MAX_PAGE_GROWTH` (:35) — none of them was crossed by this edit.

**F1 — blast radius is unbounded and unaudited.** A prompt-version bump silently converts a 9-page
patch round into a 28-page full rebuild plus a 29-page publish, mid human-sync.
- Fix (rank 1 overall): after a tier≤2 rebuild, fail before returning if
  `len(changed_pages) >> decision.patch_pages + decision.regenerate_pages`
  (compute at `graph/workspace/writer.py:715`, raise before `:753`). Needs a `--force` escape hatch.
- Plus a preflight: refuse tier 1/2 when `REWRITE_PROMPT_VERSION` / `SEED_PLAN_VERSION` / policy
  differ from the stored run's, and demand an explicit full rebuild in its own run.
- Note: prompt text is cached under `REWRITE_PROMPT_VERSION` (`graph/wiki/prompts.py:557`) — bumping
  it is a **destructive** act against an existing corpus, and QA must be told which run pays for it.

## 2. Line 1 / title churn (F2) — real, but P3

H1 is model-chosen: `candidate_title` (`graph/fast/writer.py:222`) → `best_title` (:278) →
`page.title = outcome.title` (:406) → `markdown = f"# {page.title}\n…"` (:415). The plan title is
only the model's starting point. GROWI path is filename-only (`graph/growi/client.py:862-864`) and
the page marker hash also keys on the filename (`:853`), so a title change can never rename.

Four pages got new H1s with unchanged paths; two lost their `（1/2）`/`（2/2）` sibling
disambiguator, one gained a model-invented prefix, and one title now appears on two pages.
`_retitle()` (`graph/wiki/incremental.py:386`) never ran because `decision.retitle` is only filled
under `if old_shape is not None and [(title,path)] == stored` (`graph/wiki/incremental.py:342`) —
stored titles already carried model drift the deterministic planner never emits (`retitle_pages: 0`).
The patch path already hard-errors on a changed line 1 (`graph/workspace/writer.py:53`); the
regeneration path has no equivalent — **that asymmetry is the bug**.
`plan.json` was re-stamped with the new titles (`graph/wiki/pipeline.py:2000-2012`), so drift is now
the baseline (0/28 pages mismatch `plan.title` vs line 1) → self-reinforcing.
Collateral: `publisher/index.py:86` builds card titles from the first `# ` line, so index/nav text
drifted too. Identity, markers and capture all survived → not the cause of any text loss.

Fix: pin line 1 to the plan title unless `decision.retitle` names the page
(`graph/fast/writer.py:406,415`; standard path `graph/wiki/pipeline.py:1632`).
Assert after any tier≤2 run: `plan.title == line 1 == path segment`, and all titles unique.

## 3. False Appendix — human text exiled with no deletion (F3) — worst outcome

`029-付録.md` was created (`_finish` `publisher/human_changes.py:812-822`, name from
`_appendix_name` `:664` = max page number + 1) and published (`_document_pages`
`graph/growi/client.py:857-872` globs `folder.glob("*.md")`, no allowlist; `029-付録.md` is not even
in `plan.json`). It holds four blocks, each stamped
`> 元の文書では、この部分は削除されました（元ページ: …）` — and **none of them was deleted**:

| displaced block | truth in the new source |
|---|---|
| whole page-004 human paste | section still exists, only renamed/reworded |
| page-014 added `<table>` row, emitted as bare `<td>` + stray `</tr><tr>` | table still has all 18 rows |
| page-019 human sub-item (`…１３５秒`) | fact present with a changed value (`…１１５秒`) |
| a conflict note about the page-002 verdict | not a deletion at all — see F4 |

**There is no "the source deleted this" predicate.** `rebase()` (`publisher/human_changes.py:453`)
sees only `pure` / `current` / `new` — three *wiki bodies*, never the `.docx` — and its deterministic
tests are byte-equality only: the old line block must appear verbatim in `new`
(`publisher/human_changes.py:517`) and anchors must be line-identical and globally unique in both
(`:493-494`). When anchors can't be found, `:507-509` **silently widens the window to the whole
page**, and `:500-501` (anchors out of order) / `:505` (page renamed) both mean `lost` → Appendix.
`to_appendix` (`:479-488`) then stamps the deletion sentence **unconditionally** (`:486`) using a
heading taken from the **old** page (`:484`) — so the Appendix even cites headings that no longer
exist. Everything else is delegated to a model prompt clause:
`publisher/human_changes.py:252-255`, mirrored by `_VERIFY` `:263-268`.

Docs history: `DEV_README.md:552-553` states the contract; `conflict-cases-handoff.md:163-176`
already said *"there is no rule for 'the source deleted a block that contains human text'"* and that
`_VERIFY`'s `human_kept`+`source_kept` makes an empty Appendix vacuous; `:339-343` **prescribed the
very `_MERGE` clause now in the code**, and the previous run had the opposite failure
(`human-conflicts-four-stages.md:259-262`, `codex-human-conflicts-continue.md:93`). The pendulum went
from never-Appendix to false-Appendix.

Fixes, ranked: (a) require positive deletion evidence before appendizing — bail when the change's own
lines or a similarity check still match the same page of `new` (`:479`, reuse `_Index.find` `:444`);
(b) refuse Appendix entries whose body is a table fragment (`<td>`, unbalanced `<tr>`) or a note
(`（元文書の更新…`); (c) make `_VERIFY` answer `deleted: bool` per entry and reject the merge when
false — cost: 2 extra model calls/window and `_fallback` pastes the whole human page plus a
`（元文書の更新: <entire new page>` blob; (d) write entries to a sidecar ledger instead of a published
page (breaks `DEV_README.md:552-553`, and makes displaced text invisible to humans).

## 4. Conflict notes on a one-sided round, and notes are permanent (F4)

Notes appeared at pages 007 and 019, and a third reached page 002's fact but was written onto the
Appendix page. Root causes, all confirmed:

- **Reaching `window5` is the entire predicate.** The only note-emitting branch is the `else: window5`
  at `publisher/human_changes.py:593`; `_fallback` (`:372`, `:379-380`) notes unconditionally when the
  merge returns `None`. In-memory replay of the real triples: 007 → `{window5:1, fallback:1}`,
  019 → `{window5:2, fallback:2}`. No deterministic branch can silence them.
- **The real collision test exists and is never used here.** `merge()` (`:164`) does per-side spans,
  drops source edits inside human-deleted text (`:174`), `absorbed` (`:175-176`) and
  `if _overlaps(left,right) or same_atom: return human, "conflict"` (`:183-184`) with `_atomic_ranges`
  keeping tables/code atomic (`:177`). Its only caller is the pull path (`graph/growi/client.py:1301`).
- **The two paths disagree on what "base" means.** Generate path: `rebase(pure, current, new)`
  (`:969`) re-derives the human delta from `pure → current` **every round**, so a captured edit that
  was never reconciled is a permanent collision (019 `α`: base `…１０秒` → human `…１５秒` →
  `pure` `…２０秒` → three distinct values → note forever).
  ⚠️ Do **not** "fix" this by passing `reconciled` as `old` alone. Proven by replay: `stats={}` and
  the human's `αは１５秒` gets **overwritten** by the source value, and the human-deleted 007 entry is
  silently resurrected. It must ship together with a tombstone/decision ledger.
- **Notes self-perpetuate.** A bot note has no authorship marker, so from the next round it *is* human
  text (`hlines` `:463`); `pure==new` then hits `window2` and re-publishes it byte-for-byte (`:578`).
  Nothing clears a note: no counter, no cap, no expiry, no "source adopted the human value" rule.
  Clearing exists only as a human deleting it in GROWI (`:924`).
- **No tombstones for human deletions.** A human deletion is just "lines missing from `current`". The
  only generate-path rule for it is prompt prose (`:256-257`) plus `_VERIFY`'s *"every fact in NEW is
  in RESULT, as text or inside a note"* (`:268`) — which **forces** a note for a fact the human
  deliberately deleted. That is the 007 case exactly.
- **Placement is unvalidated.** Target page comes from a cross-page unique-line index (`q = pb[0]`,
  `:502`); the splice `nlines[e.page][e.lo:e.hi] = e.lines` (`:627`) has **no** atomicity guard
  (unlike `merge()`), so writes can land inside `<table>`. And `edit.entries.append((got[1], …))`
  (`:605-606`) takes the model's free-text `appendix` field verbatim — that is how a page-002 note
  ended up on `029-付録.md` while page 002 keeps the human value and carries **no** note, so the
  source value goes unannounced.
- **Payload checks: zero hard checks.** `_attempt` (`:326-349`) checks only: `find` unique, no
  overlapping edits, human-typed digits survive (`:341-343`), size limit, then an LLM verdict. Empty
  payload, `---`-only payload, multi-block note, note between `<tr>`/`</tr>`, relative link inside a
  note, duplicate note — all prompt-only (`:247`, `:251`, `:270-271`).

Fixes, ranked: (a) **tombstone/decision ledger** — record `(page, block hash, human value, source value
at capture)` in `accept`/`_apply` (`:915-918`) and consult it before `window5` (`:593-595`): a hit
means apply silently or Appendix, never note, and at most one note per `(human value, source value)`
pair; (b) note placement + payload validator in `_attempt` (`:326-335`) and on the `appendix` channel
(`:605-606`), plus `pb[0] == c.page` (`:500`) and an atomic guard before `:627`; (c) note expiry —
lift existing notes out of `cur_w` before dispatch and drop one whose payload no longer differs
(`:571-573`, `:578`); (d) reconcile the base **with** (a).

One-line cross-cutting invariant that catches this whole class:
**if a round logs `取り込み=0`, no output page may contain `（元文書の更新`.**

## 5. Human edits re-anchor by cross-page text match (F5)

Filenames are minted from position + title slug (`graph/wiki/pipeline.py:246-248`) — so an inserted or
renamed page renumbers and renames siblings, `marker_id = "b"+sha256(…/<filename>)[:12]`
(`graph/growi/client.py:846`) is re-minted, and the human baseline is orphaned. Document identity is a
stable UUID (`publisher/human_changes.py:723`); page identity is not.
`rebase()` never matches page X→X: `_Index` indexes **every line of every page** (`:437`) and an exact
whole-block hit lands wherever it is found (`:516`) — that is the wrong-sibling mechanism; otherwise
`window()` (`:490`) walks to a line unique in both old and new and takes the page from the anchor
(`q = pb[0]`, `:502`). No title, no stamp, no fuzzy match. The stable `llm-wiki-bot-ref:<page_id>`
stamp (`graph/growi/client.py:604`) exists only in GROWI bodies and is used for ownership only
(`:550`, `:565`) — never for re-anchoring.
This run: `event=reapplied` touched 001, 002, 003, 007, 019, 023, 028, **029-付録** — 004 and 014 are
absent, i.e. their human text landed nowhere on its own page; page 023's human bullet exists nowhere
and only its marker survived, grafted onto different generated prose.
Fallback order when re-anchoring is unsure — none of them fails the run: exact match anywhere across
pages → two-anchor window → one-anchor window → fold into overlapping window → overlap means `lost` →
window ladder 1-4 → model merge, 2 attempts → `_fallback` (human text + note, on possibly-foreign
page) → Appendix → first-wins for overlapping edits (`:516`, `:501`, `:505`, `:526`, `:559`, `:574`,
`:360`, `:383`, `:479`, `:616`).

Fixes, ranked: (a) strip only links the linker owns — replace the "string not in `current`" heuristic
with the page's `fast-links.json` row (`publisher/human_changes.py:901-908`, `unlinked` `:192` made
per-line); (b) post-round invariant gate that **fails** instead of publishing (see §7); (c) human-
touched lines as verbatim-preserve islands the linker may not rewrite (`graph/fast/inline_linker.py:880,911`,
`graph/linker/render.py:125`, `graph/wiki/page.py:503`); (d) identity by immutable stamp — emit and keep
`llm-wiki-bot-ref:` locally, map old→new by stamp first, name second, text last
(mint `graph/wiki/pipeline.py:246`; consume `publisher/human_changes.py:490-512`). (d) is the biggest
correctness win and the biggest regression risk: a stale/duplicated stamp anchors an edit to the wrong
page **with high confidence**, which is worse than today's `None`. Land it inside (b)'s net.

## 6. Relative links destroyed at capture, not at link time (F6)

- `graph/growi/client.py:1329` → `publisher/human_changes.py:901` builds
  `links = {links in previous but not in current}` and calls `unlinked(previous, links)` /
  `unlinked(editable(canonical), links)`. `unlinked` (`:192`) rewrites **every occurrence on the whole
  page** of each such `[t](u)` → `t`. No line concept. Proven for page 023 by recomputing its `links`
  set from the live store: exactly the one link that disappeared. Because `current` now permanently
  lacks it, every later round classifies it as linker output — **self-reinforcing loss**.
- `_LINK = r"(?<!!)\[[^\]\n]*\]\([^)\n]*\)"` (`:37`) forbids `)` in the target, but real filenames are
  `…(1-9)(1-2).md`. Reproduced: `[ダイアログ](008-…(1-9)(1-2).md)` → `ダイアログ(1-2).md)` residue, which
  is present in the stored capture blobs. Same flaw in `MARKDOWN_LINK_RE` (`graph/wiki/page.py:35`).
- Independently, the fast linker is undo-then-re-decide: `_clean_existing` (`graph/fast/inline_linker.py:357`)
  → `_undo_manifest_row` (`:313`) → `_undo_edits` (`:279`), and on a human-since-edited line it falls to
  `lines[index].replace(token, anchor, 1)` (`:297`, `:303`) — link dropped, text kept. `_apply_links`
  (`:911`) re-adds only what the model re-proposes, under a per-page cap (`:933`); if nothing, the
  manifest row is dropped while the page is still written (`:944`). Neither 019 nor 023 has a row now.
- There is **no protected-line/region concept** for the linker. Only `protected` list in the tree is
  fenced-code lines (`graph/fast/repair.py:1189`). The linker's page set is "everything"
  (`_inventory` `:373`), and `_drop_inline_rows` (`publisher/human_changes.py:884`) *deliberately*
  deletes manifest rows for human-changed pages so the next run rewrites them from scratch. Measured:
  8 of 9 human-edited pages carried a manifest row; 29/29 pages were described.
- The Appendix is a linker **input** because `_commit` writes every `current` page into
  `_planning/pages/` (`:830`). Result, straight from the manifest: the linker injected
  `[個別操作](…)` **inside** the `元ページ:` filename in `029-付録.md:24` (breaking the provenance path)
  and a second self-referential link inside `（元文書の更新: …）` at `:46`.
  Cheapest fix in the file: extend the existing skip list in `_wrap_once`
  (`graph/fast/inline_linker.py:890-897`), which already skips `前のページ:` / `次のページ:`, to also skip
  `>` blockquote/provenance/note lines.

## 7. No completeness gate on the fast path; nothing can fail (F7)

`check_section` (`graph/wiki/page.py:414`, row message `:450`) **works** — it caught the model dropping
table rows on page 002 in this very run. But it runs only in the patch path
(`graph/workspace/writer.py:239`) and the standard writer (`graph/wiki/pipeline.py:1334,1387`) — never
in fast regeneration, whose only gate is the LLM judge (`approved and coverage_score >= 80 and not
_issues`, `graph/fast/repair.py:346`). If the judge never approves, `rewrite_pending_pages` still
publishes the best-ranked candidate (`graph/fast/writer.py:449-459`), recording `defects` /
`missing_important_information` without failing.
Consequence in this run: page 019's `Ａ：…秒` bullets were re-rendered as a Markdown **table** — the
values survived (my initial "the time values vanished" report was wrong: they are at
`019-制御項目詳細.md:42-44`, full-width digits), but the human's `…１３５秒` bullet lost its anchor line
and was appendized. Unrequested structural rewrite breaks anchoring and makes diffs unreadable.
Fix: call `check_section` on the owned block before `write_text_atomic` (`graph/fast/writer.py:410`) —
hard-error on lost rows/cells, warn on list→table shape change.

## 8. Telemetry that lies (F8) — cheapest fix in the file

- `touched=[]` is **hardcoded** at `graph/workspace/writer.py:756`; only `linked`/`deleted` rows carry
  real values. It proves nothing.
- `human_edits_overwritten` is declared at `:299`, **hardcoded `[]`** at `:762`, read at
  `publisher/pipeline.py:1055`. The honest signal is already computed and discarded: the `human` list
  from `apply_update`/`drop_pages`/`_human_edited` (`:650`, `:669`, `:704`) are dead locals. A future
  real overwrite would also log `[]`.
- `rebase()`'s `stats` Counter (`window1..5`, `fallback`, `appendix`, `no_location`) is never passed by
  `generate()` (`publisher/human_changes.py:969`) — so **nobody on the run could say why a note or an
  Appendix entry appeared**. `grep merge_rejected logs/test_conf/debug.log` is empty for the same reason.
- `ページ数=8` in `event=reapplied` counts files differing from `before` (`:852-853`, logged at `:1256`)
  — it includes the newly created Appendix, so it over-reports "human edits re-applied".
- Publish scope: the 29-page sweep came from `publish_only` (`pub-` run id,
  `publisher/pipeline.py:2004` ← `link_pending_isolated` → `:1856` `publish_scope` (whole-document
  `_content_hash`) → `:1946`), which never passes `only_pages`, so `publish_documents`
  (`graph/growi/client.py:884`) enumerates every `*.md`. The unchanged→skip gate (`:792-800`) is real
  and was **not** bypassed — all 29 bodies genuinely differed, because of F1. Fix = thread `only_pages`
  through `:1856`/`:1946`, and stop emitting constant fields.

## 9. Invariants a pre-publish gate must assert (each one was violated in this run)

1. `取り込み=0` ⇒ no page contains `（元文書の更新`.
2. A capture's human marker stays on **its own** page; `no_location == 0`, `appendix == 0`.
3. If a round reports `tier<=2`, `len(changed pages) <= patch_pages + regenerate_pages` (+small slack).
4. `plan.title == line 1 == path segment` for every page; all titles unique.
5. Link count on a human-edited page never regresses; no `…(1-2).md)` residue anywhere.
6. `</tr>`/`<tr>` balanced and no bare `<td>` outside a `<table>` in any published page.
7. `pages created == pages the source/plan justifies` ⇒ an empty Appendix never implies success.
8. `human_edits_overwritten` non-empty whenever `drop_pages` reports a human-edited page.
9. Table geometry (effective columns per row, row counts) unchanged for tables the round did not edit.

`publisher/live_verification.py:21` already *names* `split_combine_rename_reorder`,
`human_section_placement`, `markdown_integrity` in `LIVE_CASES` — **with no implementation**. That is
where 1-9 belong.

## 10. Suggested fix order for the fixing agent

1. `REWRITE_PROMPT_VERSION` preflight + blast-radius gate (§1 F1). Nothing else can be trusted until a
   small source edit stays small.
2. Honest telemetry + `stats` logging + `only_pages` publish (§8 F8). Makes every later fix measurable.
3. Pre-publish invariant gate (§9), failing the run.
4. `check_section` on the fast path (§7 F7).
5. `unlinked()` per-line + `_LINK` regex fix (§6 F6) — ~10 lines, proven root cause of link loss.
6. `_wrap_once` skip list for provenance/note lines (§6) — one condition.
7. Note payload + placement validation, and reject notes arriving via the `appendix` channel (§4).
8. Tombstone/decision ledger; only then reconcile the base (§4). Never ship the base change alone.
9. Appendix: positive deletion evidence, table-fragment/note rejection, then sidecar ledger (§3).
10. Line 1 pinned to plan title (§2 F2).
11. Identity by immutable stamp, last, under the gate's net (§5).

## 11. Verification recipe (there is **no test suite**)

`pyproject.toml` has no pytest config; the only test file is `jev/test_jev.py`; `DEV_README.md:662`
documents a `tests/` folder that does not exist on this branch. `publisher/human_changes.py` has zero
tests, and `conflict-cases-handoff.md:386-391` says the same. So: the live fixture re-run is the only
verification, and the acceptance list is mandatory.

Re-run: fixture state is in git under `data/test_conf` (`aaab8a1` = pre-run publish, `1f9167c` build op,
`168d0fa` fast link, `de75547` publish). `mount/`, `mount1/`, `mount2/` are **untracked** — the old
source `.docx` is preserved at `/tmp/qa2/orig.docx`, the pre-Round-2 wiki at `/tmp/qa/before/wiki/`,
and Round-1 captures under `data/test_conf/metadata/human-sync/doc/<doc-id>/captures/*.json`.
Read-only replay of `rebase()` in memory with `model=None` reproduces both notes deterministically
(`{window5:1, fallback:1}` / `{window5:2, fallback:2}`) — that is the cheapest regression harness, and
`_fallback` (no model) is a legitimate stand-in for "no LLM available".

## 12. State of the world right now (not repaired)

- `data/test_conf/wiki` is the post-Round-2 state: 29 pages including `029-付録.md`; 4 pages carry
  H1s that disagree with their paths; `plan.json` re-stamped; 3 notes live (007, 019, 029).
- GROWI `/test_conf` mirrors it — 29 published pages including the Appendix, which the linker keeps
  consuming.
- `mount/(社外秘)制御機能仕様書【3章】.docx` = the Round-2 source (`ca742bb4ffe1eb1f`); `mount1/` is the
  pristine baseline and `mount2/` currently equals `mount/`.
- `graph/wiki/config.py` still carries the uncommitted `ja-5 → ja-6` bump. Until it is either reverted
  or deliberately landed, **every** future round regenerates all pages.
- Nothing here was modified by this investigation.
