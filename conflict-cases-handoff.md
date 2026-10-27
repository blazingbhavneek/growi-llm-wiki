# Conflict cases handoff — bi-directional human sync, stage 2+3 merged run

Written 2026-10-20. Read `docs/handoff-human-sync-e2e-4cases.md` for the QA plan this run
executes, and `codex-human-conflicts-continue.md` for the shorter session handoff.

**Anonymisation.** No customer document names, page titles, headings, prose, hostnames,
tokens or document keys appear here. Documents are **A** and **B**; pages are addressed as
`A-002`, `B-025`, etc. (zero-padded page number = the published page file's number). Where
a value is quoted it is a generic symbol (`○ △ ×`) or a number with a unit. Our own product
strings (the conflict-note text, the Appendix heading) are kept verbatim because the fix
must match them byte for byte.

## 1. Setup

- Two source documents, 28 wiki pages each, **56 published pages** in the ledger.
- `human_sync_mode = apply` — permanent, always on. Captures, guidance, Appendix and
  conflict notes are all live in this mode (`graph/config.py:53-54`).
- Ownership marker format `bot-ref-1`; every published page carries an ownership stamp
  `llm-wiki-bot-ref:<marker_id>` that must survive every write.
- Human edits are made in the GROWI UI by a second person; mount edits are new `.docx`
  versions dropped into the mount folder. One `sync --continue` per stage.
- Test instance: internal GROWI test host, `/test/` tree. (The GROWI MCP endpoint points at
  production and 403s — unusable for this test.)

### Revision map (the data repo is its own git repo)

| Rev | What it is |
|---|---|
| `89504fe` | Stage-1 publish — wiki state after the UI-only stage, before any mount edit |
| `338991c` | Pre-mount build — last run against the *old* `.docx` versions |
| `df406b4` | Pull — local copies of what the human had in GROWI (her edits land here) |
| `cd21aed`, `b6bc9fb` | The two build commits of this run |
| `cbadc0e` | Fast-link (inline linker) pass |
| `e5d2a3b` | Publish — HEAD and `refs/llm-wiki/last-good` |

Two gotchas for whoever reproduces this: use `.venv/bin/python` (system `python3` is 3.6),
and always pass `git -c core.quotepath=false` — Japanese paths come back octal-escaped and
`git show <rev>:<path>` then silently returns an empty string, which makes any comparison
script report "no differences".

## 2. What was in the mount documents before the edits

The 13 locations this run touched, with their original source values:

| Site | Original source content |
|---|---|
| `A-001` | Device/operation table, no "additional" equipment row |
| `A-002` | Permissibility table; one memo-code row's verdict cell = `×` |
| `A-004` | Definition paragraph for the individual-operation feature; one sentence, no abnormality clause |
| `A-007` | A mark-legend section containing a short subsection (bold heading + one sentence) describing one mark |
| `A-019` | A precondition bullet with a timeout constant of **10 s** |
| `A-028` | A bullet stating that release is performed one section at a time |
| `B-004` | Two operation/error tables; one group had `rowspan="17"`, no rows 69/70; a trailing prose summary paragraph |
| `B-022` | Section order: a footnote subsection first, the "messages" subsection after it |
| `B-025` | Section heading (no "revised" suffix) + a subsection listing isolation candidates |
| `B-027` | Section heading (no "reviewed" suffix) |
| `B-028` | Timeout constant **110 s**; two numbered sub-blocks in order (2) then (1) |

## 3. What we changed in the mount documents (13 edits)

**Document A — 6 one-line edits.**
1. `A-001` new table row added to the device/operation table.
2. `A-002` verdict cell flipped `×` → `○`.
3. `A-004` a clause appended to the definition paragraph (device-abnormality → operation not possible, with a source reference id).
4. `A-007` the mark-legend sentence reworded, adding "judged by OR of ..." plus a reference id.
5. `A-019` timeout constant 10 s → 20 s.
6. `A-028` the one-section-at-a-time bullet deleted.

**Document B — 7 structural edits.**
7. `B-004` error column widened, two new numbered rows added, group `rowspan` 17 → 19.
8. `B-022` the "messages" subsection moved earlier in the section.
9. `B-025` section heading given a "revised" suffix.
10. `B-025` the isolation-candidate-list subsection **deleted**.
11. `B-027` section heading given a "reviewed" suffix.
12. `B-028` timeout constant 110 s → 120 s.
13. `B-028` one numbered sub-block moved earlier.

## 4. What was changed in the UI (9 pages)

All of this is verified by diffing `89504fe..df406b4` (published → pulled-from-GROWI), not
from notes. Each edit carries a marker (`QA-2a` … `QA-3f`) so it can be traced after the run.

| Site | Human edit |
|---|---|
| `A-001` | Added a synthetic table row carrying `QA-2c` |
| `A-002` | Verdict cell `×` → `△` **and** appended a note in the adjacent remarks cell (`QA-2a`) — same row as mount edit 2 |
| `A-004` | Appended an operational sentence to the definition paragraph (`QA-2b`) — same paragraph as mount edit 3 |
| `A-007` | **Deleted** the whole mark subsection (bold heading + sentence) that mount edit 4 reworded |
| `A-028` | Deleted the same bullet mount edit 6 deleted (both sides deleted it) |
| `B-004` | Added a synthetic table row carrying `QA-3d`; kept the trailing prose summary paragraph |
| `B-022` | Appended an operator memo to a paragraph (`QA-3c`) |
| `B-025` | Appended a scope note to the intro (`QA-3b`) and a "needs field confirmation" memo inside the subsection mount edit 10 deletes (`QA-3a`) |
| `B-028` | Changed the timeout constant in the body to **150 s** (`QA-3f`) — so wiki 150, old source 110, new source 120 |

Three deliberate collision shapes: same table row (`A-002`), same paragraph (`A-004`), and
human deletion versus source edit/deletion (`A-007`, `B-025`).

## 5. What we expected

1. No human text lost; no source fact lost.
2. On a true collision: human value stays live, new source value recorded as a
   `（元文書の更新: …）` note next to it.
3. Non-colliding source edits applied silently, no note.
4. A source rename of a page's own heading renames the page (title + file + GROWI page).
5. A source deletion of a section holding human text keeps her text and records the deletion
   in an Appendix page.
6. Reword/format-class human edits produce a `guidance` entry that reaches the writer prompt.
7. Only the pages involved change; everything else is byte-identical.

## 6. What happened — verdict

**The merge decisions were correct.** All 13 mount edits are accounted for, no human text
lost, no source fact silently dropped. Evidence: 25 of 56 page files changed, 4 conflict
notes (exactly the 4 collision sites), all 8 markers present in `wiki/` and in the journal's
`current/` with `pure=0`, `failed_docs.md` 0 bytes, 0 index errors.

| Collision | Old / Human / New | Result | Verdict |
|---|---|---|---|
| `A-002` cell | `×` / `△` / `○` | kept `△`, note records `○` | correct |
| `A-004` paragraph | source inserted a clause, human appended a sentence | both present, no note | correct |
| `A-007` subsection | source reworded, human deleted | deletion respected, reworded text only inside a note, nothing resurrected | correct decision, broken note |
| `B-025` subsection | source deleted it, it held her memo | text kept, deletion recorded as `（元文書の更新: ---）`, no Appendix | correct decision, broken bookkeeping |
| `B-028` constant | 110 / 150 / 120 | kept 150, note `120秒` | correct, and the model note to copy |
| `A-019` constant | 10 / 20 / 20 | page already said 20 from Stage 1; source converged, no stale note | correct |
| `B-004` table | rows 69/70 + `rowspan` 17→19 | applied, and a now-contradictory generated summary paragraph dropped | correct per rule, see §8.4 |

**Four defects.** Three were already known from the previous handoff; one is new.

### 6.1 The page title was dropped (new, worst)

`B-025` line 1 used to be a level-1 heading. After the run it is a level-2 heading carrying
the renamed source heading. It is the only page of 56 without a level-1 heading. The file
name and the GROWI page title still hold the old title, so title, filename and body
disagree three ways.

Chain of cause:
1. **Rename detection never ran.** `decide_update()` only looks for title changes if
   `old_shape is not None` *and* the stored plan equals the deterministic heading planner's
   shape (`graph/wiki/incremental.py:342`). These titles are writer-synthesised, so the
   branch is skipped silently. The log proves it: `"tier": 2, "retitle_pages": 0` at 17:54
   while the source heading genuinely changed.
2. **The only code that rewrites `# old` → `# new` is `_retitle()`**
   (`graph/wiki/incremental.py:386-417`), reached only when `decision.retitle` is non-empty.
3. So the heading landed in the **patch** set, which is an LLM find/replace fed the raw
   source hunk. Its prompt says only: apply the required edits, change nothing else
   (`graph/wiki/prompts.py:567`). Nothing says line 1 is protected, so the model copied the
   source line verbatim — including the source's heading level.
4. **No validator catches it.** The patch path checks patch ids, `before` uniqueness,
   overlapping ranges, image placeholders, per-image counts, verbatim fences/tables and
   identifier tokens (`graph/workspace/writer.py:19-251`) — never heading level or H1
   presence. Note the *regenerate* path does enforce it (`graph/fast/repair.py:306`), and
   the writer prompt for that path opens with "line 1 must be `# <Wiki title>`". Page `B-025`
   was patched, not regenerated. Its earlier regenerate output is still on disk with a
   correct `#` title — proof the model is not at fault here.

Why the sibling rename survived: `B-027`'s renamed heading is a mid-page sub-heading, so
copying the source level is harmless. Only the page-title line is fragile.

### 6.2 `（元文書の更新: ---）` — a note whose payload is a horizontal rule

The merge is an LLM call with find/replace edits (`publisher/human_changes.py:310-339`,
prompt `_MERGE` at `:239-255`). Its rules cover: both sides changed a fact, wording-only
differences, an existing note, human-added text, and Appendix "only if NEW no longer
contains the text the human modified" — while simultaneously demanding "keep every fact in
NEW". **There is no rule for "the source deleted a block that contains human text."** With
the block gone from NEW, the model (a) kept her text in the page — the outcome we want — and
(b) still tried to satisfy the note clause by quoting NEW's "value" for that spot. The only
text at the window boundary was the decorative `---` above the footer nav.

Two safety nets then missed it by construction:
- `_VERIFY` (`:256-263`) only answers `human_kept` and `source_kept` over RESULT+APPENDIX.
  Her text in the page satisfies the first; a deletion leaves no NEW facts to preserve, so
  the second is vacuously true. An empty Appendix passes.
- The deterministic Appendix writer `to_appendix()` (`:469`, inside `rebase()`) never fired
  for this window, and `_finish()` (`:802`) only creates the Appendix page when entries
  exist → `appendix: null`, no Appendix page.

### 6.3 Page renames are a landmine, and `retitle_pages: 0` is currently the safe behaviour

`_retitle()` does not rename anything — there is no rename/move anywhere in
`graph/wiki/incremental.py`, despite the `apply_update` docstring claiming it renames
retitled pages. Everything downstream is keyed by the **page filename**:

- GROWI path and URL segment derive from the filename (`graph/growi/client.py:484-493`,
  `:842-855`, `:857-873`), as does the ownership marker id (`sha256(seed)[:12]`, `:853-855`).
- The existing-page lookup is `expected_pages[local_path]` → `get_page(page_id=…)` else
  `get_page(path=…)` (`graph/growi/client.py:720-721`). A changed filename matches neither →
  **`create_page` makes a brand-new GROWI page** (`:768-772`) and the ledger refresh wipes
  the old document-prefix rows (`publisher/pipeline.py:681-685`) → the old page is orphaned:
  not updated, not trashed, no error. Siblings keep hard permalinks to the orphan.
- Human-sync per-page data is filename-keyed: `pure`/`current` are `{filename: text}`
  (`publisher/human_changes.py:734-735`), capture records store `"page": <filename>` (`:909`),
  `guidance` is keyed by page name (`:921-924`), and `_commit(..., sweep=True)` **deletes**
  `pure`/`current` entries whose name disappeared (`:851-861`). `captures/*.json` are never
  migrated, so a captured edit can only land in the Appendix afterwards (`:601-603`).
  `pages/<marker_id>.json` is keyed by the filename-derived marker id → orphaned.
- Content-based `rebase()` anchoring (`:443-449`, `:610-620`) is the only mitigation; it
  re-locates human text by content, and pushes unlocatable text to the Appendix. But
  guidance lookups use the **new** name (`:585`), so guidance silently stops applying.

And unlike destructive ops elsewhere, there is **no guard** here equivalent to
`assert_deletable` (`publisher/human_changes.py:785-800`) that refuses when human
information exists. Making retitles work is a real project (identity-preserving republish +
page-key migration), not a 5-line patch.

### 6.4 Notes are placed and phrased without any validation

`（元文書の更新: …）` notes in apply mode are **written by the model** inside its `replace`
strings (`:246`, spliced at `:328-330`). The code-built note at `:369`
(`f"{head}（{NOTE}: {body}）\n"`, blockquote form `:370-372`) is fallback-only — used when
there is no model, the window exceeds `LIMIT`, or both attempts failed. There is **no check
at all** on the note: not non-empty, not single-line, not markdown-free, not "outside a
table". Observed:
- `A-002` — the note sits inside `<tr>`, between `</td>` and `<td>`. Browsers foster-parent
  text in `<tr>` out of the table, so on GROWI it renders detached from the row it annotates.
- `A-007` — the note swallows two whole blocks: an opening note, a bold heading, a blank
  line, a paragraph, then the closing parenthesis. Multi-line, raw markdown inside, not
  readable inline.
- `B-025` — a note whose payload is a horizontal rule, see §6.2.

Retry behaviour worth knowing before you add checks: the merge window runs exactly 2
attempts (`for attempt in (1, 2)`, `:348`) feeding the exception text back as `FAILURE`; on
exhaustion `rebase` substitutes `_fallback` (`:590-592`) — human text plus a code-built note —
so a page never keeps stale text. The patch path gives 5 attempts
(`attempts = max(5, settings.wiki_write_attempts)`, `graph/workspace/writer.py:127`) and on
exhaustion the page is dropped and the document escalates to tier 2 `"patch-escalated"`
(`graph/workspace/writer.py:671-677`). Neither writes to `logs/<project>/failed_docs.md`,
which only comes from `publisher/failure_logs.py:225`.

### 6.5 On "blank pages" — not reproduced here

No page in `wiki/` was empty or near-empty after this run; the smallest page is ~800 bytes
and every one has body text. So "blank page" is **not** reproduced by this fixture, and it
should not be assumed to be the same bug as §6.1. Three things in this run could read as a
blank page from the browser, and none of them is a write-time truncation:

- **A stranded GROWI page.** If a page's identity ever changes, the publisher creates a new
  page and abandons the old one with its content intact and its siblings' permalinks still
  pointing at it (§6.3). The old page then looks frozen forever, and the new one looks thin
  because the `pure`/`current` rows under the old name were deleted rather than migrated.
- **An expected page that was never created.** The Appendix page should exist after a
  source deletion of a section holding human text; `appendix: null` means it does not (§6.2).
  A missing page in a listing can be mistaken for an empty one.
- **A page that opens with no title.** §6.1 leaves the page without a level-1 heading, so the
  rendered page starts with a smaller heading and the GROWI title still shows the old text —
  which reads as an untitled, half-empty page.

If a genuinely blank page is seen, capture its ledger key, page id and the publishing
revision at that moment. The mechanisms above do not produce one, so a real blank page is a
separate failure and needs its own evidence.

## 7. New finding: cross-page links are lost on the GROWI round-trip

Across exactly the 9 human-edited pages, **19 of 39 relative cross-page links are gone in
the pulled copy** (published → pulled). A page nobody edited kept 6/6.

| Page | published | pulled | lost |
|---|---|---|---|
| `A-001` | 4 | 1 | 3 |
| `A-002` | 4 | 3 | 1 |
| `A-004` | 5 | 2 | 3 |
| `A-007` | 3 | 3 | 0 |
| `A-028` | 6 | 3 | 3 |
| `B-004` | 6 | 3 | 3 |
| `B-022` | 4 | 2 | 2 |
| `B-025` | 5 | 2 | 3 |
| `B-028` | 2 | 1 | 1 |

She did not delete links; the round-trip did. This explains most of the "link churn" in this
run: 13 of the 25 changed files are **link-only** differences, because the next inline-linker
pass re-adds links that the round-trip lost — and not always to the same place. Two
regressions were visible: `A-022`'s system-diagram link moved from the correct page to the
individual-operation page, and `B-012`'s warning link moved from the warning-list page to
the runtime-check page. `A-028` also gained a link on an ordinary menu-bar noun pointing at
the work-section page.

**Not established** whether GROWI's stored body loses them or our pull fails to map them
back; no permalinks of either style were present in either copy, so the mapping path needs
reading against a live page body. This is the cheapest remaining win: fix the round-trip and
13 files of churn per run disappear.

## 8. Secondary issues (real, lower priority)

1. **Invented structure.** `A-002` grew a new sub-heading, its single table was split in
   two, and a legend line was added that exists nowhere in Document A's source. That is
   writer embellishment on a page whose only real change was one cell conflicting with a
   human edit. The prompt forbids it (`graph/wiki/prompts.py:575`, "no facts not in the
   source"; and the no-new-headings rule) — so it is an enforcement gap, not a rule gap.
2. **Normalisation churn on human-adjacent text.** A 25-hunk sweep flipped full-width to
   half-width across `A-002` and in the other direction on `A-004`, reverting the half-width
   form of her own appended paragraph's neighbours. No information lost, but it buries the 4
   lines that actually matter in a 68-line diff.
3. **Ordering/numbering half-applied.** `B-028` correctly followed the source's own
   out-of-order numbering, but kept the `(1)` label on one sub-block and dropped the `(2)`
   label on the other.
4. **Generated prose deleted from a page the human last touched.** `B-004`'s trailing
   summary paragraph existed in her copy and is gone. Correct by the "text identical in OLD
   and HUMAN is old source text: NEW wins, no note" rule, and the new rows do contradict
   that summary — but a reviewer will see content vanish from a page she edited, with no
   note. Decide explicitly whether generated prose a human has had in front of them is
   droppable without a note.
5. **`guidance: {}` on both documents** even though reword/format-class edits ran, so
   nothing reached the writer prompt. `_CLASSIFY` (`:230-236`) produces structure
   instructions and `doc["guidance"]` is written at `:921-924`, but it stayed empty — the
   plumbing exists and is not being fed. This is expectation 6 and it is still unproven.
6. **Performance and observability** (unchanged from the previous handoff, listed so the
   next reader does not re-derive them): observe cache keyed on window content/line numbers
   so any deletion re-inventories the document (26 of 28 windows re-ran, ~100 s each at
   `concurrency = 2`); no per-model-call telemetry in the writer; no wiki-stage timeout; the
   judge returned score 100 with `approved: false` then 100 with `approved: true`, so score
   does not gate approval.

## 9. The structural problem this exposes

We are treating page-identity changes as text patches. They are not text patches:

- `A-007` human deleted a section the source reworded → needs a *decision*, not an edit.
- `B-025` source renamed the page's own heading → needs a title/identity decision.
- `B-025` source deleted a section holding human text → needs an Appendix record.
- 9 of 56 pages needed a rewrite only because the round-trip ate their links.

The tier ladder (`0` unchanged, `1` patch only, `2` some pages regenerated, `3` full rebuild,
`graph/wiki/incremental.py:167-181`) has no notion of "rewrite this page but preserve its
identity". That is the missing tier, and it is why correct merge decisions were produced by a
code path that has no page-level invariants.

## 10. Fix options

### Option A — stop the bleeding (~12 lines, 3 files, low risk)

1. **Protect the title.** One sentence in the incremental edit prompt
   (`graph/wiki/prompts.py:567`): never change line 1 or its heading level. Plus one check
   appended to the existing `errors` list (`graph/workspace/writer.py:245`): *if line 1
   started with `# `, the result's line 1 must still start with `# `.* Self-limiting, so it
   cannot fire on a page shape we did not think of; every assembly site writes a level-1
   heading (`graph/wiki/pipeline.py:1632`, `graph/fast/writer.py:415`,
   `graph/formats/tabular.py:459`, `publisher/human_changes.py:807`); house precedent at
   `graph/fast/repair.py:306`; failure is safe (5 attempts → regenerate, never a lost page).
2. **One `_MERGE` clause.** If the source deleted a block containing human text: put her
   changed lines in `appendix`, add no note. Appendix machinery is already complete
   (`to_appendix` `:469`, `_finish` `:802`) and publishing enumerates the folder generically
   (`publisher/queue.py:943`, `publisher/pipeline.py:152`) — no publisher work.
3. **Two rejects in `_attempt`** (`publisher/human_changes.py:310-339`): a note whose payload
   is blank or pure markup (`---`, `**`, whitespace), and a note line sitting between `<tr>`
   and `</tr>`. Reuses the existing `FAILURE` retry; `_fallback` remains the safety net.

**Acceptance for Option A** — re-run the stage 2+3 fixture and assert: no page loses its
level-1 heading; no note with a markup-only payload; no note line inside a table row; the
Appendix page appears when a section holding human text is deleted and `doc["appendix"]`
points at it; all 8 markers still present with `pure=0`; the 4 collisions unchanged in
outcome; `failed_docs.md` still 0 bytes.

**Known trade-off of Option A.** `B-025` keeps its old title and filename and the source's
"revised" rename appears nowhere. Correct title, no orphaned GROWI page. If the rename must
be visible, put it in the body as a sub-heading or a note — never the page title, until
Option B exists.

### Option B — the proper fix (what this really needs)

1. **Identity-preserving page rewrite tier.** Rewrite a page's body while pinning its
   filename, page id, marker id, ledger row and human-sync keys. Republish must update in
   place — never `create_page`. Until that exists, retitles must stay off.
2. **Page-key migration** for `pure`/`current`/`captures`/`guidance`/`pages/<marker_id>` when
   a page's identity legitimately changes, plus an `assert_deletable`-equivalent guard that
   refuses any identity change while human information exists.
3. **Round-trip link preservation** (§7) — map GROWI permalinks back to relative links
   losslessly, or store the local link structure so a pull cannot degrade it. This is what
   makes "only the pages that changed differ" achievable.
4. **Merge rule set for source-deleted / human-owned regions**, and a verifier that actually
   tests the Appendix requirement instead of `human_kept` + vacuous `source_kept`.
5. **Feed `guidance`** (§8.5) so reword/format edits reach the writer prompt, then prove it
   by byte-comparing the writer prompt across runs.
6. Decide the §8.4 policy (droppable generated prose) and write it into `_MERGE`.
7. Prompt cache discipline: prompts are cached with `REWRITE_PROMPT_VERSION`
   (`graph/wiki/prompts.py:557`). Bump it with any prompt change or the new wording may not
   take effect.

## 11. Caveats

- **There is no test suite** for any of this: the only test file in the tree is
  `jev/test_jev.py`, and `DEV_README.md:662` documents a `tests/` folder that does not exist
  on this branch. Nothing asserts the prompt constants, so Option A breaks no tests — and
  nothing would have caught Option A breaking anything either. The live fixture re-run is the
  only verification, which makes the acceptance list above mandatory, not optional.
- Live GROWI rendering was **not** inspected (no network to the test instance). The
  render-consequence claims about notes inside `<tr>` come from HTML parsing rules, not from
  seeing the page.
- Which of the two `_structural_shape` skip conditions fired (planner returned `None`, versus
  stored shape mismatch) is not distinguished — both skip the same retitle branch.
- Nothing in the repository was modified by the investigation that produced this document;
  the working tree, `data/test` HEAD `e5d2a3b` and `last-good` are as the run left them.

---

# §12 Addendum, same day — a second fixture reproduced all of it and added three more

Reproduced on a **second, single-document fixture** (Doc A only, 28 pages, `human_sync_mode = apply`)
with a deliberately small source edit: five single-symbol verdict-cell flips, two numeric constants,
three added table rows, four deleted list items, one reworded sentence, one moved paragraph, one
swapped adjacent pair, one new sub-item, one footnote reference. Round 1 (UI-only, 11 pages edited, 9
captured, 2 controls correctly ignored) was **clean, zero notes** — the same clean result this document
recorded for stage 1. Round 2 (source-only) is where it broke.

Code-level detail, file:line and the ranked fix order are in **`conflicts-failure.md`**. This section
records only what changed in *this* document's conclusions.

## 12.1 Every defect in §6 is still unfixed

| § | defect | status on the new fixture |
|---|---|---|
| 6.1 | page title dropped / title-filename-body disagree | **reproduced, 4 pages at once** instead of 1; `retitle_pages: 0` again |
| 6.2 | note whose payload is a horizontal rule | not reproduced (no section deletion this round), but the payload rules are still prompt-only |
| 6.3 | Appendix not created when it should have been | not reproduced — because the pendulum swung; see 12.3 |
| 6.4 | correct merge decision produced by a path with no page-level invariants | **reproduced and widened**; see 12.2 |

Option A items 1-3 (protect line 1, Appendix rule, two rejects in `_attempt`) are **not implemented**.
`_VERIFY` still answers only `human_kept` / `source_kept`, so an empty Appendix is still vacuous, and
`LIVE_CASES` in `publisher/live_verification.py:21` still names `split_combine_rename_reorder`,
`human_section_placement`, `markdown_integrity` **with no implementation**. Option B items
(identity-preserving tier, page-key migration, link round-trip preservation, real Appendix verifier,
feeding `guidance`) are all still open. §11's caveat stands: there is still **no test suite**, so the
live fixture re-run remains the only verification.

## 12.2 New failure mode A — a prompt-version bump silently became a full rebuild

The run's real trigger was not the source edit. `REWRITE_PROMPT_VERSION` had been bumped
(`wiki-sections-ja-5` → `wiki-sections-ja-6`, **uncommitted**), which invalidated every stored page
checkpoint; the pages and their sidecars were **deleted**, and the one page that failed patching
escalated into a rebuild that had nothing left to resume. Result: 28/28 pages regenerated and 29 pages
published, while the log still said `"rebuild": "incremental", "tier": 2`, and `decide_update` had
correctly asked for `tier 1, reason "patch", patch_pages 9, regenerate_pages 0`.

Consequences this document did not previously anticipate:
- `plan.json` is **re-stamped** with the model's new titles, so title drift becomes the new baseline
  and is self-reinforcing (after the run, 0/28 pages mismatch `plan.title` against body line 1).
- The unchanged→skip publish gate is real and was *not* bypassed — every body genuinely differed. The
  defect is upstream (blast radius), not in the publisher.
- The asymmetry is precise and is the bug: the **patch** path already hard-errors when body line 1
  changes; the **regeneration** path has no equivalent check.

## 12.3 New failure mode B — the Appendix now fires **without any deletion**

6.3 recorded the Appendix failing to appear when it should have. On this fixture it appeared when
nothing had been deleted: a new `<last page number + 1>-付録.md` was created and published, holding
four human blocks lifted off their own pages, each stamped
`> 元の文書では、この部分は削除されました（元ページ: …）`. Ground truth: one section had merely been
renamed, one table still had all of its rows, and one fact was present with a changed value. None was
a deletion.

Two structural reasons, both new to this document:
1. **There is no deletion predicate on this path.** `rebase()` receives three *wiki bodies* — previous
   export, merged-with-human, new export — and never the source document. Its deterministic tests are
   byte-equality and global line uniqueness; when anchors fail it silently widens the window to the
   whole page and hands the "deleted or merely reworded?" question to the model. The footer text
   "削除されました" is stamped **unconditionally** for every entry regardless of how it got there, and
   the heading it cites is read from the *old* page, so the Appendix can cite headings that no longer
   exist.
2. **§10 Option A item 2 prescribed the clause now in the code.** The clause exists at
   `publisher/human_changes.py:252-255`; the previous run had the opposite failure. So the Appendix
   component now fails in **both** directions, which means the input it needs (positive evidence of
   deletion) is missing, not that the prompt wording needs more tuning.

Additional shapes seen for the first time: a **table fragment** (`<td>` runs plus a stray `</tr>` /
`<tr>`) was placed outside any table, and a **conflict note** reached the Appendix through the model's
free-text `appendix` field, leaving the page whose fact it described with **no note at all** — so the
source value went unannounced while the human value survived. The fast linker then injected relative
links *inside* the Appendix's `元ページ:` provenance path, corrupting it.

## 12.4 New failure mode C — notes on a one-sided round, and notes never expire

Three notes appeared on a round that logged `取り込み=0` (no UI capture), where the expectation is zero.
Findings that go beyond §6.2 and §8:

- Reaching `window5` (the three-way-different window branch) **is** the whole note predicate; the
  deterministic fallback emits a note unconditionally. The real same-fact collision test — per-side
  spans, drop source edits inside human-deleted text, `absorbed`, overlap-or-same-atom → `conflict`,
  with tables/code kept atomic — exists and is only ever called from the **pull** path.
- The two paths disagree about what "base" means. On the generate path the human delta is re-derived
  from `previous export → merged` on **every** round, so a captured decision that was never reconciled
  becomes a permanent collision. Reproduced in read-only in-memory replay of the real artifacts with
  no model: one page `{window5:1, fallback:1}`, another `{window5:2, fallback:2}`.
- ⚠️ **Do not "fix the base" alone.** Replaying with base = last-published body gives an empty stats
  set — and overwrites the human value with the source value, and silently resurrects a human-deleted
  block. It must ship together with a tombstone/decision ledger, which does not exist yet: a human
  deletion is represented only as "lines missing from the merged body".
- **Notes are permanent and self-perpetuating.** A bot-written note carries no authorship marker, so
  from the next round it *is* human text; the source-vs-previous branch then re-publishes it byte for
  byte. There is no counter, cap, expiry, or "the source adopted the human value, so drop the note"
  rule. Clearing exists only as a human deleting it in the UI. Practical effect on the test plan: any
  Round 3 expectation of "exactly one note per collision site" is unmeasurable while stale notes ride
  along, and because *deleting a note is the documented resolution gesture*, false notes train testers
  to delete blindly and each deletion is recorded as a decision.
- For a fact the human **deleted**, the prompt plus the verifier's "every fact in NEW must appear in
  RESULT, as text or inside a note" **forces** a note. That is the wrong-note-on-a-deletion case, and it
  is not a model error.

## 12.5 New failure mode D — link loss happens at capture, and is self-reinforcing

§9's "9 of 56 pages needed a rewrite only because the round-trip ate their links" is confirmed and
localised:
- The inbound restore is followed by a step that collects "links present in the previous body but
  absent from the current one" and strips **every occurrence on the whole page** of each — no line
  concept. So a link on a human-edited line is stripped from *both* base and human, the capture still
  records (marker only), and because the merged body now permanently lacks the link, every later round
  treats it as linker output. Proven for one page by recomputing its link set from the live store.
- The link regex forbids `)` in the target, but real filenames contain `(1-2)`. Reproduced exactly:
  a link to such a page degrades to text plus a `…(1-2).md)` residue, and that residue is present in
  stored capture blobs. The same regex flaw exists in the second link-stripping helper.
- The fast linker is undo-then-re-decide, its page set is "every page of every document", it has **no**
  protected-line concept (the only `protected` list in the tree is fenced code), and rows for
  human-changed pages are *deliberately* dropped so the next run rewrites them from scratch. Measured:
  8 of 9 human-edited pages carried a linker manifest row; all 29 pages were described.

## 12.6 Invariants to add before the next stage

Cross-cutting, one line each, all violated by this run (full list and where each belongs:
`conflicts-failure.md` §9): a round that captured no UI edits may emit **no** notes; a capture's human
marker must stay on its own page; a `tier<=2` round may change no more pages than
`patch_pages + regenerate_pages`; `plan.title` = body line 1 = path segment; link count on a
human-edited page must never regress; `</tr>`/`<tr>` balanced and no bare `<td>` outside a `<table>`;
an empty Appendix is not proof of success.

And the two report fields that QA is told to trust — `"touched": []` and `"human_edits_overwritten":
[]` — are **hardcoded constants**; the honest value is computed and discarded nearby. The `stats`
counter that records which window branch and which Appendix path fired is never passed out of the
merge, which is why the log cannot say why a note or an Appendix entry appeared. Fix the telemetry
before fixing anything that needs measuring.
