# Human-sync glue plan: minimal changes so mount and UI changes reconcile

Written 2026-10-20 against `HEAD = cba2de2` (branch `neo-hardcoded`). This is a read-only analysis. Nothing in the
code, `data/` or GROWI was changed to produce it.

How it was produced:
- Five read-only agents read the code, the handoffs, git history, and the deleted tests and design doc.
- The coordinator re-checked every load-bearing claim against the code.
- The coordinator also checked against the real human-sync state left in `data/hc-e2e-fast/`, a synthetic canary
  document (HX-300) with its own git history.
- One agent re-traced the canary's merge rounds with a stdlib `difflib` re-implementation of the locate/branch logic.
  It reproduced the shipped artifacts exactly.

Line numbers are `HEAD` line numbers. Tags used throughout:
- **[V]** the coordinator read it in the code.
- **[A]** an agent reported it with file:line, and it is consistent with the surrounding code.
- **[F]** reproduced on the `data/hc-e2e-fast` fixture. Those builds ran 2026-10-12. Since then `rebase()` is
  byte-identical; only the `_MERGE`/`_VERIFY` prompt text changed.
- **[DC]** a claim in a handoff document.

---

## 0. The answer in one screen

**The design is right, and roughly 90% of it is already built.** Both of the owner's principles already match the
shape of the code:

- **Principle 1: a big mount change gets a full rewrite, then every human fact is merged back.** That is tier 3:
  `write_wiki_pages` → `build_wiki_output` → `publish_output` → `apply_generated` → `rebase(pure, current, new)`
  (`graph/workspace/writer.py:698-736` [V]).
- **Principle 2: UI changes are reflected locally and respected later.** That is pull → `accept` → `current/`.
  - The durable human decision is simply `pure → current`.
  - `pure` is overwritten with the fresh generator output on every generate (`publisher/human_changes.py:972`,
    `_commit(..., pure=new)` [V]). So `pure` already is "what the source said last time".
- **Tier 2 already is "rewrite a page but keep its identity".** `plan.json` is kept, so the file name, marker id,
  GROWI page and human-sync keys stay put (`graph/workspace/writer.py:686-694` [V]).

So there is **no need for a new tier, a decision ledger, a tombstone store, or an identity service**. The handoffs
proposed all four, and each becomes unnecessary once the glue below is in place.

**What fails is the glue between the pieces.** There are ten gaps, and each one is small:

| # | Gap | Effect seen live or on the fixture |
|---|---|---|
| G1 | Avoidable rewrites. A prompt-version bump discards every page checkpoint, the fast writer adopts a model-chosen H1, and cost-based escalation re-plans. | Line anchors die, so every human edit goes through the model path (F1, F2). |
| G2 | Page identity is the file name, and a tier-3 re-plan re-mints file names. | False "deleted" Appendix entries, orphaned guidance, GROWI pages trashed and re-created (F5). |
| G3 | Bot-written `（元文書の更新: …）` notes are indistinguishable from human text. | Notes stack 0→1→2 [F]; a source catch-up never clears its note; a note gets appendized as "deleted" (F4). |
| G4 | Placement needs lines that are byte-identical and unique across **all** pages. | Windows widen to whole sections or pages, `lost` → Appendix (F3), and generic headings land on the wrong page [F] (F5). |
| G5 | Disjoint edits inside one window always go to the model. The existing deterministic 3-way `merge()` is only used on pull. | False notes where nothing collided [F]; model calls that weren't needed. |
| G6 | `_fallback` keeps `cur_w` whole and quotes the **whole** `new_w`. | 14-line section quotes [F]; source-deleted text resurrected [F]. |
| G7 | Model merge output is checked only for unique `find`, no overlap, and human digits. | Notes inside `<tr>`, `---` payloads, notes in the Appendix, stale source text kept, false "deleted" (F3, F4). |
| G8 | Link regexes stop at the first `)`. The linker writes into notes and provenance lines. | `…(1-2).md)` residue; corrupted `元ページ:` paths (F6). |
| G9 | Deleting or moving a page in GROWI raises and blocks; it is never reflected locally. | The page, and every publish batch containing it, stays blocked until a human restores it. This is a principle-2 gap. |
| G10 | No telemetry. `rebase` stats are never logged, and `human_edits_overwritten` / `touched` are hard-coded `[]`. | Nobody could attribute a note or Appendix entry to a branch (F8). |

**Size.**
- **Core:** about **170 changed lines in 6 files** (steps 1-11 of Appendix B). After it, a big mount rewrite cannot
  silently lose information from either side.
- **Complete Tier A:** about **225 lines in 8 files**. It also makes the result clean.
- **Tier B:** about 150 lines. **Tier C:** about 60 lines, after the D1 decision.
- Every change is local and can be reverted on its own.

**Two things to do first** (§3):
1. **Prompt version.** The `REWRITE_PROMPT_VERSION` `ja-5 → ja-6` bump that caused Round 2's silent 28/28
   regeneration is now **committed** (`graph/wiki/config.py:14`). Land **M1** before the next live round. Otherwise
   every document whose next update needs even one page regenerated will regenerate *all* of its pages.
2. **Token.** `conf-test-1-agg.md:4` commits a live GROWI token, and `configs/test*.ini` holds one too [DC]. Rotate it.

---

## 1. How it works today (verified)

### 1.1 Pull (UI → local): `graph/growi/client.py:1152-1423` [V]

```
GROWI page (fetched by page_id from the ledger)
 ├─ page None / status deleted → raise "remote_deleted" → block_page         (:1196-1199)
 ├─ path changed                → raise "remote_moved"   → block_page         (:1202-1203)
 ├─ ownership stamp missing     → raise                  → block_page         (:1204-1206)
 ├─ revision unchanged          → skip
 └─ transport merge(previous_remote, remote, previous_local)                   (:1301)
      └─ "conflict" → rebase(..., model) + Appendix-style entries in the body  (:1302-1326)
    restore_page_links → separate(): strip links "in previous, not in current" (:1327-1329; hc:901-907)
    base != human → HumanStore.accept → classify (guidance) → capture record
                 → rebase({page: base}, {page: human}, current) → _commit       (hc:909-933)
```

### 1.2 Generate (mount → local) [V]

- `decide_update` picks a tier. Thresholds: `PATCH_MAX_CHURN = 0.20`, `FULL_MIN_REGEN_SHARE = 0.50`,
  `MAX_PAGE_GROWTH = 2.0` (`graph/wiki/incremental.py:33-35`).
- **Tier 1.** An LLM find/replace patches the page; the rest stays byte-identical (`graph/workspace/writer.py:672-676`).
- **Tier 2.** Some pages are regenerated and `plan.json` is kept (`:686-694`).
- **Tier 3.** `rmtree(state_root)` runs, then a re-plan and a full rewrite (`:698-712`).
  - `_plan_pages` mints `f"{number:03d}-{slug(title)}.md"` (`graph/wiki/pipeline.py:239-265`).
  - So any plan change renumbers every later sibling, and any title change renames that page.
- **After every tier:** `publish_output` (`:720`) → `apply_generated` (`:735`) → `HumanStore.generate` → `rebase(pure,
  current_without_appendix, new)` → `_finish` (Appendix) → `_commit(pure=new, sweep=True)` (`hc:956-974`).

### 1.3 `rebase(old, human, new)`: `publisher/human_changes.py:453-630` [V]

1. **Changes.** A per-page line diff of old against human. Each non-equal opcode is one change, `B → A`.
2. **Locate.**
   - **Exact match.** `B` must be unique (raw text, line-aligned, across all pages) in both old and new. It lands
     on whichever page holds the hit, with **no same-page check** (`:517`).
   - **Window.** Otherwise, take the nearest human-untouched line above and below that is unique across all pages
     of old and of new (`:493-494`).
     - Both anchors on one new page, in order: the window lies between them.
     - One or no anchor: the window extends to the page edge, so with no anchor it is the **whole page** (`:503-509`).
     - Anchors on different pages, out of order, or the file name has vanished: **`lost`** (`:500-501`, `:505`).
3. **Group.**
   - A window absorbs every change whose old lines fall inside it, even ones located exactly (`:528-545`).
   - Windows that overlap in new are unioned (`:546-557`).
   - Windows that overlap in **old** coordinates are both sent to `lost` (`:558-564`) [V].
4. **Decide** (`:574-597`). The branches are tried in order:
   - window1: `new_w == cur_w`.
   - window2: `new_w == old_w` → `cur_w`, verbatim, including any old notes.
   - window3: each `B` occurs once in both. Any pure insertion disqualifies it.
   - window4: `new_w` is blank → Appendix.
   - window_space.
   - window_edge.
   - window5 → `_merge_window`: merge plus verify, two attempts. On failure, `_fallback` = the **whole `cur_w`**
     plus the **whole `new_w`** as a note (`:372-381`, `:601-603`).
5. **Appendix.** `to_appendix` always stamps `元の文書では、この部分は削除されました` (`:486`), whether the entry came
   from window4, `lost` (`:612-614`), an overlap (`:619-620`), or the model's unvalidated free-text `appendix` (`:605`).

### 1.4 State [V]

`metadata/human-sync/doc/<sha256(source_id)>/` holds:
- `pure/` — the last generator output.
- `current/` — `pure` plus human changes, plus the Appendix.
- `captures/` — base/human blobs plus guidance. Read only by `audit` and by rollback `replay_captured`.
- `doc.json` — `{appendix, guidance{page: [...]}}`.

The key comes from the ledger `source_id`, so it is stable across rebuilds (`hc:728-736`). Guidance and captures are
keyed by **page file name**.

---

## 2. What is really broken, and what is not

### 2.1 Root causes, with evidence

**G1: avoidable rewrites feed the merge with noise.**
- **Round 2.** `decide_update` correctly said tier 1 with 9 patch pages. One patch failed, so the run escalated to
  tier 2. `_rewrite_all` → `_resume_rewritten_page` then rejected **every** page because the stored `rewrite_version`
  was ja-5 (`graph/wiki/pipeline.py:1199` [V]). The pages were unlinked (`:1701-1702` [V]) and all 28 regenerated.
- **Model titles.** The fast writer adopts the model's title: `page.title = outcome.title` (`graph/fast/writer.py:402-408`
  [V]), and line 1 is written from it (`:415` [V]). `plan.json` is then re-stamped with the drift
  (`graph/wiki/pipeline.py:2031-2048` [A]).
- **Cost escalations re-plan a valid plan.** Both `most-pages-changed` (`incremental.py:331` [V]) and
  `escalated-most-pages` (`writer.py:684` [V]) go to tier 3 even though the plan still holds.
- **Why it matters.** `rebase` places human edits deterministically only when most generated lines are byte-identical.
  Every avoidable regeneration turns deterministic placements into model decisions, or into `lost`.

**G2: identity is the file name.**
- Tier 3 re-mints file names from position plus slug (`pipeline.py:244-252` [V]).
- `rebase` can follow content to another page when it finds an exact match or two anchors. But the fallback requires
  `c.page in new` (`hc:505`), so after a rename the change goes to `lost` and then to the Appendix as "deleted".
- [F] On the canary, page `004-第4章-参考データ.md` became `004-第4章-参考データ-〜-第5章-運用記録.md` when chapters 4
  and 5 merged:
  - its guidance in `doc.json` is still keyed to the old name and will never reach the writer again;
  - at the rename build, the old page's standing change went to `lost`.
- The old GROWI page is **trashed** and a new one created; the default route runs `_trash_under`
  (`client.py:1023-1028` [A], fixture [F]). `conflict-cases-handoff.md` §6.3 says it is "orphaned", which is wrong.

**G3: bot notes are human text.** Nothing parses a note. Once written into `current/`, a note is part of the human
delta for every later round.
- [F] On canary page `001-…概要.md` the note count went 0 → 1 (build `9f561bb`) → 2 (build `f2fb63b`):
  - the human had deleted the `床荷重` line and the source changed it;
  - `---` repeats on every page, so it can't be an anchor;
  - the window therefore ran from `相対湿度` to `次のページ` and covered the whole `作業手順` section;
  - the result was window5 → `_fallback` → a 14-line quote;
  - at `f2fb63b` the first quote sat inside `cur_w` as "human text", and a second quote was appended under it.
- Consequences, traced in code [V]:
  - When the source changes the value the note quotes (S → S2), the notes stack.
  - A catch-up never fires window1, because `cur_w` still contains the note.
  - In the sequence "human deleted X, source reworded it to X', then the source deleted X'", the **note** goes to the
    Appendix as "deleted human text" (window4, `hc:582-585`).
  - Notes count as human information (`hc:768-779`).

**G4: placement is too strict, and once too loose.**
- Anchors must be byte-identical and unique across all pages. Several kinds of line never qualify:
  - repeated bullets (12 identical bullets per section on the canary [F]);
  - `---` and the `前のページ` / `次のページ` lines;
  - headings repeated across sibling pages;
  - headings the generator re-levels between runs (`## 構成` → `### 構成` at `f2fb63b` [F]).
- The result is whole-section or whole-page windows. Pages of roughly 7k characters already exceed `LIMIT=30000` for
  `old_w + cur_w + new_w` (`:355`), which means a whole-page quote fallback.
- The opposite bug [F]: the human's `## 作業手順` → `### 手順` on page 001 was an **exact hit on page 003**, which
  carries the same generic heading. That heading was unique in old and in new because 001's copy had been re-levelled.
  Only a host window absorbing that change kept it from renaming page 003's section (F5).

**G5: disjoint edits in one window go to the model.**
- [F] Canary page 003, build `f2fb63b`: the human removed one comma and inserted a section, and the source deleted a
  duplicated block elsewhere in the window.
  - window3 failed because a pure insertion disqualifies it (`:638-640`), so the window went to window5. The merge
    failed and fell back.
  - The note's payload was the **unchanged** source line, a false collision.
- Emulating the transport `merge()` on that window [F] gives the human hunks (replace, insert) and the source hunks
  (two deletes). No span overlaps, so the result is a clean merge with **no note**.

**G6: `_fallback` both over-quotes and resurrects.**
- It returns all of `cur_w` plus a quote of all of `new_w` (`hc:372-381` [V]).
- `cur_w` includes source text the human never touched. So when the source deleted or changed that text, the
  fallback brings the old version back into the body. On canary page 003 it resurrected the source-deleted `作業手順`
  block [F].

**G7: model output is barely checked.**
- `_attempt` (`hc:320-350` [V]) checks only: each `find` is unique, edits don't overlap, human digits survive, size, and
  then a model verifier. Everything else is prompt prose.
- Each failure below is real:
  - a note inside `<tr>` (A-002 [DC]);
  - a `（元文書の更新: ---）` note (B-025 [DC]);
  - a correct note routed into the Appendix (R2 page 002 [DC]);
  - a value collision turned into an Appendix entry (R2 019 `Ａ` [DC]);
  - a table fragment in the Appendix (R2 page 014 [DC]).
- [F] Canary page 004, build `c9a3d67`: the model kept the **old** intro (which the human never touched), added a note
  with the new intro, and put the old intro into `appendix`. The verifier passed it. That stale text has been a
  standing "human change" ever since.

**G8: links.**
- The same `)` bug in three regexes: `_LINK` (`hc:37` [V]), `_MARKDOWN_LINK` (`graph/fast/inline_linker.py:872` [V]) and
  `MARKDOWN_LINK_RE` (`graph/wiki/page.py:35` [V]). Each ends the target at the first `)`, but real file names contain
  `(1-2)`.
- The linker's skip list (`inline_linker.py:886-893` [V]) skips headings, tables, HTML and nav lines. It does not skip
  `>` quotes, `元ページ:` lines or `元文書の更新` lines.
- The 19/39 link loss is **by design**, not GROWI [A].
  - A capture rewrites the live page from `current`, which holds no linker links, and drops the manifest row.
  - The relink is an LLM re-decision capped at 3 links per page (`graph/config.py:239`; `inline_linker.py:924`,
    `:932` [A]).
  - The losses are exactly 3 on five of the nine pages.

**G9: UI page operations block.**
- `remote_deleted` and `remote_moved` raise and block (`client.py:1196-1203` [V]), and the publish preflight is
  all-or-nothing [A].
- A select-all replacement removes the bottom `<!-- llm-wiki-bot-ref:… -->` stamp. The pull then raises "GROWI
  ownership markers were removed" (`:1204-1206` [V]).

**G10: telemetry.**
- `generate()` never passes `stats` to `rebase` (`hc:969` [V]).
- `WriteResult(touched=[], human_edits_overwritten=[])` are constants (`writer.py:756`, `:762` [V]).
- Merge rejections and classify failures are logged at DEBUG only (`hc:365`, `:391`).

**Latent: the pull side can produce false notes.**
- A transport conflict runs a full `rebase(..., model=model)` (`client.py:1302-1314` [V]), with the **local spelling**
  of the same generation acting as "NEW".
- So on a UI-only round, a human edit on a line whose GROWI spelling differs from the local one (a link, an escape)
  can produce a `（元文書の更新: <local spelling>）` note. This changed silently in `3870336` [A]; it used to block.

### 2.2 Things the handoffs got wrong (do not "fix" these)

1. **Round 2 was not mount-only.**
   - Its mount edits hit facts that Round 1's human had already changed:
     - 002 `ソ（送電禁止）`;
     - 019 `α` (human 10→15, source 10→20);
     - 019 `Ａ` (human 125→135, source 125→115);
     - 007, the legend entry the human deleted and the source reworded.
   - Rule 4 and case 43 say notes there are **correct**. `取り込み=0` only means no *new* capture; the standing
     human delta in `pure → current` is re-applied every round. The invariant "`取り込み=0` ⇒ no note"
     (`conflicts-failure.md` §4, §9) is wrong; §7.3 gives the replacement.
   - The real R2 bugs were these:
     - the rewrite (G1);
     - the 019 `Ａ` collision becoming an Appendix entry instead of a note (G1, G4);
     - the 002 note going through the model's `appendix` channel (G7);
     - the false Appendix entries (G4, G7);
     - title drift (G1).
2. **No decision ledger or tombstones are needed.**
   - `pure := new` every round already makes `pure → current` the durable decision.
   - A human deletion persists as lines absent from `current`, and a dismissed note persists the same way.
   - What is missing is a note recogniser (G3) and better placement (G4).
   - `conflicts-failure.md` §4's "never pass reconciled as `old`" is moot: `old` already is the reconciled base.
3. **Notes are supposed to be permanent while the collision persists.**
   - window2, exact and window3 *must* keep them. A deleted test asserts exactly that:
     `test_rule_4_conflict_is_a_note_and_repeating_the_same_source_adds_none` [A].
   - The bugs are stacking, stale payloads and the missing catch-up, not permanence. `conflicts-failure.md`'s "note
     expiry at :578" would break the repeat test.
   - Also, a modification plus its note hits `exact` (`:517`) first, not window2.
4. **Guidance works when the file name is stable.**
   - The canary stored two guidance items, and the writer prompt contained them (`data/hc-e2e-fast` build `c9a3d67`,
     `work/page-004/rewrite-01-prompt.md` [A]).
   - The live `guidance: {}` is expected: no work-PC round ever made a **format-only** UI edit [A].
5. **The "missing identity-preserving rewrite tier" exists.** It is tier 2. What is missing is identity across a
   re-plan (G2).
6. **A pull merge conflict does not block**, although the e2e handoff §3.4 says it does. It rebases [V].
7. **Marker counts double.** A `grep` over `wiki/` also counts the `_planning/pages/` mirror (`hc:854` [V]).
8. **`conflicts-failure.md` §12, "every future round regenerates all pages", is overstated.** `test_conf` already paid
   that cost during R2. It is still true for every other corpus, on its first update that regenerates anything.
9. **`conflict-cases-handoff.md` §12.1 says Option A item 1 is not implemented. It is.** The patch-path line-1 check is in
   HEAD (`graph/workspace/writer.py:52-53` [V]).

---

## 3. Phase 0 — before any code

- **P0.1 Prompt version.**
  - Land M1 before the next live round.
  - Do not revert ja-6 to get the same effect. Prompt caches are keyed by `REWRITE_PROMPT_VERSION`
    (`graph/wiki/prompts.py:557` [DC]), so reverting the string while keeping the new prompt text mixes cache
    generations.
  - Do not bump `REWRITE_PROMPT_VERSION`, `SEED_PLAN_VERSION` or `FAST.version` again before M1 and M3 land.
- **P0.2 Token.** Rotate the GROWI token committed in `conf-test-1-agg.md:4`, and move it to `.env` (`GROWI_TOKEN`).
- **P0.3 Restore the tests.** `git checkout 3870336^ -- llm-wiki-air/tests` restores the 11 test files that the 10-19
  sync deleted. `.gitignore` still whitelists them [A], so the deletion looks accidental.
  - Why they matter: `test_human_changes.py` (1440 lines) and `test_human_sync_rollout.py` are pure Python with fakes.
    They assert:
    - `rebase(x,x,z)==z` and `rebase(x,y,x)==y` over 500 random trials;
    - moved sections are followed;
    - a repeated line never carries a change to another section;
    - the Appendix entry appears once;
    - notes are idempotent on a repeat;
    - a deleted note stays deleted;
    - the merge guards;
    - state survives 4 updates.

    These are exactly the functions Tier A touches.
  - Expected drift [A]:
    - `test_sync_fallback`'s CLI-default test, because `sync` now defaults to fast;
    - environment-variable renames;
    - `ParserFallbackTest`.
  - Watch item: `_overlaps` was loosened from `<=` to `<` in `3870336` (`hc:116-125`).

---

## 4. The changes

Tier A is the glue. Tier B is hardening, to land after one clean live round. Tier C needs an owner decision (§8).
Each item gives where, what, why, size, risk and how to verify it. Appendix B gives the commit order.

### Tier A: the glue

#### H8. Say why (G10). Land first.

- **Where:** `generate()` (`hc:969`); the pull transport `rebase` (`client.py:1308`); `_merge_window` (`hc:365`); and
  `classify` (`hc:390-392`).
- **What:**
  - Create `stats = Counter()`, pass `stats=stats`, and log
    `human_sync event=rebase document=%s stats=%s` at INFO.
  - Log `merge_rejected` reasons at INFO.
  - Log classify failures at WARNING.
- **Size / risk:** ~8 LOC, no risk.
- **Why first:** these stats are what make every later round explainable. Every placement shows up as one of
  exact / window1-5 / edge / space / merge / fallback / appendix / no_location. The Appendix pendulum swung
  (`a9a7dcf` → `human_semantic` removal → `1255f0f` → `cba2de2` → R2 [A]) precisely because no change was attributable.

#### M1. Tier-2 updates never re-write untouched pages because of a prompt-version change (G1, F1)

- **Where:**
  - `graph/wiki/pipeline.py:1182-1201`, `_resume_rewritten_page`;
  - its call in `_rewrite_all` (`:1685-1691`);
  - `_load_seed_plan` (`:886`, with the version check at `:904`) and its resume call in `run_pipeline`.
- **What:** add `accept_stale_version: bool = False`, and pass `config.require_resume`. That flag is `True` only for the
  tier-2 caller (`graph/workspace/writer.py:692` [V]).
  ```python
  # _resume_rewritten_page
  if (
      (state.get("rewrite_version") != rewrite_version and not accept_stale_version)
      or state.get("filename") != page.filename
      or state.get("content_sha256") != sha256_text(markdown)
  ):
      return None
  # _rewrite_all
  _resume_rewritten_page(output_path, state_path, page, rewrite_version=rewrite_version,
                         source_line_count=source_line_count,
                         accept_stale_version=config.require_resume)
  # _load_seed_plan: the same rule for raw["prompt_version"]
  ```
- **Semantics:**
  - An untouched page keeps its prompt version until its own source changes, or until a deliberate full rebuild
    (`--force`, tier 3).
  - Pages the update drops are missing on disk, so they still regenerate.
  - Patched pages also resume, because the patch path re-hashes their sidecar (`state["content_sha256"]`,
    `graph/workspace/writer.py:286` [V]). Only the dropped pages are rewritten.
- **Size / risk:** ~8 LOC. Low risk: the tier-3 and initial-build paths keep the strict check.
- **Do not** add a "raise after rebuild" gate instead (`conflicts-failure.md` §1). By then the LLM cost is spent and the
  files are gone.

#### M2. Line 1 is the plan's title, never the model's (G1, F2)

- **Where:** `graph/fast/writer.py`, right after `_writer(...)` returns in the candidate loop (`:222-233` [V]); and
  `:402-408` [V].
- **What:**
  - Set `candidate_title = page.title` immediately after the writer call, then delete `:402-408`.
  - Zero out title defects after `_judge` so that unfixable title complaints don't burn every attempt. The agent
    reported the field as `verdict.title_defects`; verify the name.
- **Why:**
  - In R2, 4 pages drifted.
  - The standard path already pins the title, and the patch path already hard-errors on a changed line 1
    (`writer.py:52-53` [V]).
  - The `plan.json` re-stamp (`pipeline.py:2031-2048` [A]) becomes a no-op.
- **Size / risk:** −7/+2 LOC, low risk.

#### M5. A rewrite may not drop source facts (promoted from Tier B; this is the Stage-3 loss)

- **Where:** `graph/fast/writer.py`, in the candidate loop right after the `_image_errors` block (`:246-256` [V]).
  `session.raw_source` and the verbatim fallback `_source_body` were reported by an agent [A]; verify both names.
- **What:**
  - Run `check_section(candidate_body, lines=lines, source_text=session.raw_source, block_ranges=(), placeholders=[],
    facts=[], check_identifiers=True, tokens=policy.code_tokens)`. It already exists (`graph/wiki/page.py:414` [V]).
    Do not pass `verbatim_blocks`, because fast rewrites tables as HTML.
  - Add a `_value_errors(source, draft)` helper (~12 LOC):
    - every **number with a unit** in the source (NFKC; `秒 分 時間 日 回 件 個 台 年 月 % ℃ °C kPa MPa kg g mm cm m V
      A W Hz ms s`) must still be in the draft. `code_tokens` skips bare numbers, and those are most of this corpus's
      facts;
    - the draft must have at least as many table rows (`<tr` plus `|` rows) as the source.
  - Errors become writer feedback, exactly like image errors.
  - If the best candidate still fails, publish the verbatim source body for that page. A plain page is better than a
    page that silently lost a value.
- **Why:**
  - In Stage 3 and Round 2 the *writer* changed content by itself: a dropped `(2)` label on B-028, bullets turned into
    a table on 019, an invented sub-heading and split table on A-002 [DC].
  - The merge cannot bring back what the writer dropped.
  - Today the only gate on the fast path is the LLM judge. `rewrite_pending_pages` publishes the best candidate even
    when the judge never approved one (`graph/fast/writer.py:449-459` [A]).
- **Limit:** a plain sentence with no number or ID that the writer drops is still caught only by the judge.
- **Size / risk:** ~25 LOC. Medium risk, because more retries mean more cost on big rewrites.

#### H1. Notes are bot text: a note lives exactly as long as the source still says what it quotes (G3)

- **Where:** in `publisher/human_changes.py`:
  - new helpers next to `_fallback` (`:372`);
  - one line in `generate()` (`:966-969`);
  - one line in `to_appendix` (`:479`);
  - one line in `_information` (`:775`).
- **What:**
  ```python
  _NOTE_END = re.compile(rf"（{NOTE}: (.*)）((?:\s*</t[dhr]>)*)\s*$")   # also the in-cell form `…（…）</td>`

  def _key(line: str) -> str:
      """Compare lines ignoring width, spacing, heading/quote markers and link targets. Never spliced."""
      text = unicodedata.normalize("NFKC", _LINK.sub(lambda m: m.group(0)[1:m.group(0).index("](")], line))
      return " ".join(text.lstrip("#> \t").split())

  def _current_notes(text: str, source: str) -> str:
      """``text`` without the source-update notes whose quoted text ``source`` no longer contains."""
      have = "\n".join(_key(l) for l in _lines(source))
      # A payload with no real text (e.g. B-025's `---`) is always stale: every page contains `---`.
      stale = lambda payload: (not any(_key(l).strip("-*_=~|# ") for l in _lines(payload))
                               or any(_key(l) and _key(l) not in have for l in _lines(payload)))
      lines, out, i = _lines(text), [], 0
      while i < len(lines):
          line = lines[i]
          if line.strip() == f"> **{NOTE}:**":                                   # blockquote form
              j = i + 1
              while j < len(lines) and lines[j].startswith(">"):
                  j += 1
              if not stale("".join(l[1:] for l in lines[i + 1:j])):
                  out.extend(lines[i:j])
              i = j
              continue
          m = _NOTE_END.search(line)                                             # paren form, own line or cell end
          if m and stale(m[1]):
              line = line[:m.start()] + m[2] + ("\n" if line.endswith("\n") else "")
              if not line.strip():
                  i += 1
                  continue
          out.append(line)
          i += 1
      return "".join(out)
  ```
  - In `generate()` before `rebase`, add
    `current = {p: _current_notes(t, new.get(p, "")) for p, t in current.items()}`. Keep the original `before` for
    `_commit`.
  - In `to_appendix`, add `text = _current_notes(text, "")`. With an empty source every note is stale, so notes are never
    appendized.
  - In `_information`, compare `pure` with `_current_notes(current, "")`.
- **Why it is enough.** The table traces each case against `rebase` [V]:

  | Round (human value H captured, note quotes S) | Today | After H1 |
  |---|---|---|
  | source unchanged (still S) | note kept (exact path) | note kept: its payload is still in `new` (the repeat test still holds) |
  | source S → S2 | window5; the merge or the fallback adds a **second** note | stale note removed → window5 → **one** note quoting S2 |
  | source catches up (S → H) | window1 misses because `cur_w` has the note → model or fallback | stale note removed → `new_w == cur_w` → window1, no note |
  | human deleted X, note quotes X', then the source deletes X' | window4 sends the **note** to the Appendix as "deleted" | note removed → `new_w == cur_w == ""` → window1, nothing |
  | human edits an unrelated line while a note shows | kept | kept (case 55) |
  | canary page 001 with two whole-section quotes [F] | both re-applied forever | the stale one (`…以上が必要`) is removed; the current one stays |

- **Size / risk:** ~30 LOC, low risk.
  - Only a note that ends its line, or ends just before closing table tags, is recognised. A human sentence after a
    note is never touched.
  - Notes are only *removed* when their quoted text is gone from the new generation.

#### H3. A deterministic 3-way merge before the model call (G5): reuses the existing `merge()`

- **Where:** `hc:593`, just before window5. New helper next to `merge()` (`:164`).
- **What:**
  ```python
  def _clean_merge(old_w: str, cur_w: str, new_w: str) -> str | None:
      """The transport three-way merge, accepted only when the two sides changed different text."""
      h, s = _changes(old_w, cur_w), _changes(old_w, new_w)
      # the exact test merge() uses at :174 to drop such edits; here it routes them to the model instead
      if any(not d[2] and d[0] <= e[0] < d[1] and d[0] < e[1] <= d[1] for d in h for e in s):
          return None          # the source edited text the human deleted: case 43, which needs a note (model)
      out, status = merge(old_w, cur_w, new_w)
      return out if status in ("active", "absorbed") else None
  # in rebase, before window5:
  elif (clean := _clean_merge(old_w, cur_w, new_w)) is not None:
      stats["window_merge"] += 1
      edits.append(_Edit2(g.q, g.lo, g.hi, _lines(clean), g.changes))
  ```
- **Why:**
  - `merge()` (`:164-189`) is a token-level diff3 that keeps tables, code and images atomic (`_atomic_ranges`). It is
    already tested by the transport tests, and is used on the pull path only.
  - Disjoint edits inside one window (rule 5) now resolve with no model call and no note. That is the page-003
    false-note case [F].
  - A true overlap, or a table touched by both sides, still goes to the model as today.
  - **Do not** use `merge()`'s `conflict` result: it returns the human text wholesale. Do not let it drop a source
    edit inside human-deleted text (`:174`); the guard above routes that case to the model.
- **Size / risk:** ~15 LOC. Low-medium risk.
  - Cost: `_changes` spawns `git diff` via `line_hunks` (`graph/wiki/incremental.py:78`), so this adds about four spawns
    per window5 window.
- **Verify:**
  - Canary `f2fb63b`, page 003, replayed with `model=None`: no note, and the source deletion is applied.
  - The rule-5 tests in the restored suite stay green.

#### H4. `_fallback` never resurrects and never over-quotes (G6)

- **Where:** `hc:372-381`. Also the merges tuple at `:596-603`, which must now carry `old_w`.
- **What:**
  ```python
  def _fallback(old_w: str, cur_w: str, new_w: str) -> str:
      """Human text (minus source text the human never touched and the source dropped), then the source lines
      it lacks as one note. Nothing is lost and nothing appears twice."""
      a, b = _lines(old_w), _lines(cur_w)
      now = "\n".join(_key(l) for l in _lines(new_w))
      head = [l for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
              for l in b[j1:j2] if tag != "equal" or not _key(l) or _key(l) in now]
      have = "\n".join(_key(l) for l in head)
      missing = [l for l in _lines(new_w) if _key(l) and _key(l) not in have]
      if not missing:
          return "".join(head)
      if any(l.lstrip().startswith(("<t", "</t", "|")) for l in missing):
          missing = _lines(new_w)          # never quote half a table
      body = "".join(missing).strip("\n")
      ...                                   # unchanged: paren form for one short line, else the > quote
  ```
- **Why:**
  - Untouched source text that the source has dropped no longer comes back (canary page 003's deleted block [F]).
  - The quote shrinks to the lines that actually differ. A link-only or width-only difference gives no note at all,
    because `_key` ignores both. When nothing is missing there is no note, so the fallback doubles as the catch-up rule.
  - Every `new_w` line is either in the body or in the note, so no source fact is lost.
- **Known limitation:** a source change to an untouched line in a *conflicted* window shows up in the note, not in the
  body. That is only on the model-failed path, and it is lossless. The exact fix is a line-level diff3 fallback (H11,
  Tier B).
- **Size / risk:** ~15 LOC, low risk.

#### H2. Placement: same-page-first, normalised, and never `lost` while the page exists (G4, G2 in part)

- **Where:** the locate loop (`hc:516-522`) and `window()` (`:490-512`). Normalised keys are used only to **find**
  places. Splicing always uses the raw lines.
- **What, in order of precedence:**
  1. **Raw exact match, guarded.** Keep today's rule. But if the hit is on another page while `c.page` still exists in
     `new`, require more evidence: either at least 3 non-blank lines or at least 60 characters, or the nearest non-blank
     neighbour line also matching at the hit. Otherwise fall through. This fixes the page-003 heading hit [F] and keeps
     the deleted "moved section" test green, because its neighbour `## Two` matches [A].
  2. **(Slim) "The source only reformatted this window" counts as unchanged.** This is 3 lines in the decide chain,
     right after window2, and it replaces a ~10-line `_local_exact`:
     `elif [k for k in map(_key, _lines(new_w)) if k] == [k for k in map(_key, _lines(old_w)) if k]:` apply `cur_w`.
     - It covers a width-only change (`１０秒` → `10秒`) on the line the human edited.
     - A re-levelled heading is handled by H3's `merge()`.
  3. **Global two-anchor window, as today.** This is how a change follows content that moved to another page.
  4. **Own-page normalised anchors.** Use the nearest human-untouched lines whose `_key` is unique on `c.page` in old and
     in new. If they come out of order, use the whole own page.
  5. Only a page that **vanished** gives `None`. H7 then tries the page it went to.

  ```python
  # 2 (locate loop, after the guarded raw-exact test):
  elif c.page in nlines and (at := _local_exact(c)) is not None:
      others.append((c, (c.page, at)))
  # 4 (window(), replacing the "lone/no anchor" and the two "return None" branches while c.page in nlines):
  n = nlines[c.page]
  ok_, nk = Counter(map(_key, a)), Counter(map(_key, n))
  at = {_key(l): i for i, l in enumerate(n)}
  good = lambda k: k in eq and _key(a[k]) and ok_[_key(a[k])] == 1 and nk[_key(a[k])] == 1
  b = next((k for k in range(c.i1 - 1, -1, -1) if good(k)), None)
  f = next((k for k in range(c.i2, len(a)) if good(k)), None)
  lo = at[_key(a[b])] + 1 if b is not None else 0
  hi = at[_key(a[f])] if f is not None else len(n)
  if lo > hi:
      b = f = None
      lo, hi = 0, len(n)
  span = (b + 1 if b is not None else 0, f if f is not None else len(a),
          eq[b] + 1 if b is not None else 0, eq[f] if f is not None else len(hlines[c.page]))
  return _Win(c.page, lo, hi, {c.page: span}, [c])
  ```
- **Why:**
  - Today a page that still exists sends changes to `lost` → the Appendix ("削除されました") in three situations: the
    anchors split across pages, the anchors are out of order, or a lone anchor sits on another page.
  - A missing anchor also widens the window to the page edge even when a page-unique line is two lines away.
  - With these changes, after a rewrite (principle 1) the human text lands on its own page, in a section-sized window.
- **Evidence [F]:** a normalised anchor re-trace of the canary builds:
  - page 001's window shrinks from 12 lines to 3;
  - page 004's window shrinks from 87 lines (the whole page) to 1 and 6 lines, which removes the stale-intro cause.
- **Size / risk:** ~32 LOC. Medium-low risk: it never anchors on a non-unique line, and it keeps human text on the page
  where the human wrote it.

#### H5. Deterministic checks on a model merge (G7)

- **Where:** `_attempt` (`hc:320-350`), after `result` is built. This replaces the digits check at `:341-343`.
- **Failure path:** every check raises a `ValueError`, which feeds the existing `FAILURE` retry. The second failure
  takes `_fallback`, which after H4 is bounded and lossless.
- **SLIM VERSION (decided 2026-10-20). This replaces the code block below; see "Slim H5" right after it.**
  - The model's `appendix` field is **ignored**: set `appendix = ""` at `:325`.
  - Human text must therefore stay in the page. Only fixed code writes Appendix entries: window4 (the whole part was
    deleted), `lost`, and overlaps.
  - Every false "deleted" entry seen so far came through the model's field.
  - The one behaviour difference: when the source deletes only *part* of a block a human edited, the human's lines stay
    on the page instead of going to the Appendix. No information is lost.
- **What:**
  ```python
  from graph.wiki.page import code_tokens      # NFKC identifier tokens; check_section already uses it

  def _facts(text: str) -> set[str]:
      norm = unicodedata.normalize("NFKC", text)
      return code_tokens(norm) | set(_DIGITS.findall(norm))   # code_tokens skips bare numbers (page.py:26-29)

  body = _current_notes(result, "")                            # the result without its notes
  keys = lambda t: {_key(l) for l in _lines(t) if _key(l)}
  o, h, n = _facts(old_w), _facts(cur_w), _facts(new_w)
  if (lost := ((h - o) | (n - (o - h))) - _facts(result + "\n" + appendix)):
      raise ValueError(f"values missing (human-added, or the source's current ones): {sorted(lost)[:20]}")
  flat = lambda t: " ".join(unicodedata.normalize("NFKC", t).split())
  def added(before: str, after: str) -> list[str]:    # text `after` added or changed vs `before`, 2+ chars each
      a, b = flat(before), flat(after)
      return [b[j1:j2].strip() for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
              if tag in ("replace", "insert") and len(b[j1:j2].strip()) >= 2]
  kept = flat(result + "\n" + appendix)
  if (gone := [s for s in added(old_w, cur_w) + added(old_w, new_w) if s not in kept]):
      raise ValueError(f"text the human or the source wrote is missing from the result: {gone[:5]}")
  if keys(body) & ((keys(old_w) & keys(cur_w)) - keys(new_w)):
      raise ValueError("the result keeps source text the source has changed or removed; use NEW's version")
  # Note checks judge only the notes this merge ADDED; a note already in cur_w (a legacy in-cell one,
  # or one still valid elsewhere on the page) must not make every later merge of this window fail.
  added = [p for p in _payloads(result) if p not in _payloads(cur_w)]
  if any(_current_notes(f"（{NOTE}: {p}）\n", new_w) == "" for p in added):
      raise ValueError("a source-update note must quote NEW's current text exactly, never empty or markup")
  if _in_table_count(result, NOTE) > _in_table_count(cur_w, NOTE):
      raise ValueError("put the source-update note after the table, never inside a table or row")
  if appendix.strip():
      if NOTE in appendix:
          raise ValueError("a source-update note belongs next to the human's value, not in the appendix")
      if re.search(r"</?t[dhr]\b", appendix, re.I) and not re.search(r"<table\b", appendix, re.I):
          raise ValueError("the appendix must not hold table fragments")
      if keys(appendix) - (keys(cur_w) - keys(old_w)) - {k for k in keys(appendix) if k.startswith("#")}:
          raise ValueError("the appendix may only hold lines the human changed")
      if _survives(old_w, new_w):
          raise ValueError("the passage still exists in NEW (reworded or moved): merge the human's text into it")
  ```
  Helpers, ~15 LOC:
  - `_in_table_count(text, needle)` tracks `<table>` depth and `|` rows, and counts the lines inside a table that
    contain `needle`.
  - `_payloads(text)` returns the payloads that `_current_notes` would see. It shares the scanner with H1.
  - The payload check reuses H1's rule: a note is valid only if `_current_notes` would keep it against `new_w`. That
    covers both a stale quote and an empty or markup payload.
  - `_survives(old_w, new_w)` is true when at least half of `old_w`'s non-blank lines have a counterpart in `new_w` with
    `SequenceMatcher(None, _key(x), _key(y)).ratio() >= 0.6`. This is the **positive deletion evidence**
    `conflicts-failure.md` §3 asked for.
- **The value check is the five merge rules at token level.** With O, H, N = token sets of old, human, new:
  - Everything the human added (H − O) must survive.
  - Every source token (N) must survive, as text or inside a note.
  - The exception is a token the human deliberately removed that the source left unchanged, (O − H) ∩ N.

  | Case | Required in the result |
  |---|---|
  | human 10→15, source 10→20 | {15, 20}: the human value plus a note |
  | human deletes X; the source keeps X and edits Y→Y2 in the same window | {Y2}: the deletion wins silently |
  | human deletes X; the source rewords X→X' | {X'}: a note |
  | source catches up to H | {H} |

- **What it catches.** Each of these is caught deterministically:
  - every R2 and Stage-2+3 bad shape — the note in `<tr>`, the `---` payload, the note in the Appendix, the `<td>`
    fragment, a reworded section declared deleted;
  - the canary page-004 stale intro [F];
  - any dropped number or identifier, on either side.

  The **span rule** makes the same promise for words. Every piece of text the human added or changed, and every
  piece the source added or changed, must still be in the result: in the page, in a note, or in the Appendix.
  - It holds for every designed outcome:
    - a conflict keeps the human text in the page and the source text in the note;
    - a source edit inside text the human deleted goes in a note;
    - source-deleted human text goes in the Appendix.
  - So it closes the "word-only facts are checked only by the model" gap. `_VERIFY` stays as a second opinion.
  - Cost: a character diff per window, which `LIMIT` bounds. **Implement it line-first.** Run `SequenceMatcher` over
    the lines, then character-diff only inside the replaced line pairs. A naive character diff over a whole-page window
    can take seconds, because `SequenceMatcher` is quadratic. Done line-first it takes milliseconds.
  - A model that reflows or paraphrases the human's text is rejected and falls back to H4, which keeps it verbatim.
- **Size / risk:** ~40 LOC. Medium risk: more attempts reach the fallback. Ship it after H3 and H4, and watch the
  `fallback` / `merge_ok_*` ratio in H8.

#### Slim H5: what to actually implement (~25 LOC, all in the Core)

It keeps only four rules. They replace the value rule, `_survives`, the appendix rules and the payload checks above:

```python
appendix = ""                                          # the merge may not move text out of the page
body = _current_notes(result, "")                      # the result without its notes
keys = lambda t: {_key(l) for l in _lines(t) if _key(l)}
flat_result = " ".join(unicodedata.normalize("NFKC", result).split())
gone = [s for s in _added(old_w, cur_w) + _added(old_w, new_w) if s not in flat_result]   # _added: line-first span diff
gone += sorted((keys(old_w) & keys(cur_w) & keys(new_w)) - keys(body))                    # lines neither side touched
if gone:
    raise ValueError(f"text the human or the source wrote is missing from the result: {gone[:5]}")
if keys(body) & ((keys(old_w) & keys(cur_w)) - keys(new_w)):
    raise ValueError("the result keeps source text the source has changed or removed; use NEW's version")
if _current_notes(result, new_w + "\n" + cur_w) != result:
    raise ValueError("a source-update note must quote NEW's current text, never empty or markup")
if _note_in_table(result) > _note_in_table(cur_w):        # count note lines inside <table> / | rows
    raise ValueError("put the source-update note after the table, never inside a table or row")
```

1. **Nothing disappears.** This covers both sides' added or changed text, and every line neither side touched. It is
   stronger than the old number/ID rule, because it covers words too.
2. **Nothing stale comes back.**
3. **Notes are real.** Each note quotes NEW's text, or was already in HUMAN. Empty or markup-only notes are refused.
4. **No new note inside a table.**

Prompt edits (~5 lines):
- `_MERGE`: appendix must always be `""`. Replace the "put the human's changed lines in appendix" clause with: "Text the
  human added or changed always stays in the result, where it was, even when the source deleted the text around it; add
  no note for that deletion."
- `_VERIFY`: `human_kept` means the human's text is in RESULT. Drop the APPENDIX sentences.

#### H6. An honest Appendix with a stable name (G3, G4 follow-up; F3)

- **Where:** `to_appendix` (`hc:479-488`) and its four callers (`:585`, `:614`, `:620`, `:624`); `_entries` (`:671`);
  `_finish` (`:820`).
- **What:**
  - **Reason-specific footer.** Add a `deleted: bool` argument.
    - True only for window4 (the source text between the anchors is gone). The model channel is removed by slim H5;
      delete the model-entry code at `:605-606` and `:623-624`.
    - False for `lost` and for overlaps. Those entries get `> 新しい版ではこの編集の位置を特定できませんでした（元ページ: …）`.
    - `_entries` counts both footers.
  - **Table fragments.** Wrap them in `<table>…</table>`.
  - **Stable name.** `_finish` reuses `doc["appendix"]` when that name is set and is not taken by a generated page. A
    change in page count then no longer trashes and re-creates the Appendix GROWI page.
- **Size / risk:** ~10 LOC, low risk.

#### H7. A vanished page's changes follow it to its new page; guidance is re-keyed (G2)

- **Where:** a helper computed once at the top of `rebase`; `window()` and `_local_exact()` look up `own =
  remap.get(c.page)` when `c.page not in nlines`; `generate()` re-keys `doc["guidance"]`.
- **What:** for each old page whose name vanished:
  - if exactly one new page has the same stem (name minus `^\d+-` and `.md`), map to it;
  - otherwise map to the new page sharing the most normalised non-blank lines, provided its overlap is at least 30% of
    the old page and there is no tie;
  - otherwise leave it unmapped. The change then stays `lost`, now with the honest footer (H6).
- **Why:** this is principle 1 when the source merges, splits or renames pages. [F] On the canary, the old page-004
  intro is located on the merged `004-…〜-第5章-運用記録.md` page instead of going to `lost`.
- **Size / risk:** ~18 LOC. Medium risk: never map by title or by an LLM, and keep the floor and the no-tie rule.

#### M3. A re-plan keeps file names by title slug (G2)

- **Where:**
  - The tier-3 `build_wiki_output` call, `graph/workspace/writer.py:707-712`. The old names are already in hand:
    `names = _plan_filenames(state_root)` at `:646` [V], before the `rmtree` at `:706`.
  - Plumb `identity_hints: list[str]` through `WikiConfig`, `wiki_config` (`writer.py:300`), `run_wiki` and
    `build_wiki_output` (`:396`).
  - Call `_plan_pages(plan, keep=config.identity_hints)` at its only call site (`pipeline.py:1930`).
- **What:** in `_plan_pages` (`pipeline.py:239`), take the previous name with the same stem, in occurrence order.
  Otherwise mint the name as today; the existing suffix dedupe handles collisions.
- **Why:**
  - An inserted or removed section no longer renumbers its siblings.
  - The human-sync keys, marker ids, GROWI pages and guidance all survive a full rewrite.
  - H2's own-page fallback then applies.
- **Side effect:** an inserted page's number prefix can be out of reading order. Navigation uses plan order, and the
  agent found nothing that depends on the prefix equalling the position [A].
- **Edge:** very long titles get a model rename (`pipeline.py:1938`). Their stems won't match, so they re-mint as
  today.
- **Size / risk:** ~28 LOC across 4 files. Medium-low risk.

#### U1. Link regexes accept one level of parentheses in the target (G8)

- **Where:** `hc:37`, `inline_linker.py:872`, `page.py:35`. Note that `page.py:35`'s lazy `[^\n]*?\)` stops at the first
  `)` too.
- **What:** use the target `\((?:[^()\n]|\([^()\n]*\))*\)` and keep each regex's own prefix.
- **Size / risk:** 3 LOC, very low risk.
- **Verify:** `[ダイアログ](008-…(1-9)(1-2).md)` unwraps to `ダイアログ` with no residue.

#### U2. The linker never writes inside notes or provenance (G8)

- **Where:** `inline_linker.py:886-893` [V].
- **What:** add `">"` to the `startswith` tuple, and add `or "元ページ:" in line or "元文書の更新" in line`.
- **Size / risk:** 2 LOC, very low risk.

#### U3. Pull transport conflicts never invent notes (latent G7)

- **Where:** `client.py:1308-1314`; the `rebase` signature and the merges loop (`hc:453`, `:601-603`).
- **What:**
  - Add `rebase(..., notes: bool = True)`. When `notes` is false, an unresolved window keeps `cur_w`, the human's GROWI
    text.
  - The pull path calls it with `model=None, notes=False`. `restore_page_links` (`:1327`) converts the links back.
- **Why:** on that path "NEW" is the local spelling of the same generation, so a note is always false.
- **Size / risk:** ~5 LOC, low risk.

**Tier A total: about 225 LOC in 8 files (slim version).**
- The **Core**, steps 1-11 of Appendix B, is about 170 LOC. It is what makes a big mount rewrite **safe**: no silent
  loss from either side.
- The rest of Tier A, about 80 LOC, makes the result **clean**.
- Tier B is about 150 LOC and Tier C about 60 LOC.

### Tier B: hardening (after one clean live round)

| ID | Change | Where | LOC | Fixes |
|---|---|---|---|---|
| H10 | **Union, don't drop.** When groups overlap in old coordinates (`hc:558-564`), union them instead of sending both to `lost`. | `hc:558-564` | ~10 | A latent `lost` path [V] (never observed firing) |
| H11 | **Line diff3 fallback.** Align `old_w↔cur_w` and `old_w↔new_w`. Human-only regions take human lines, source-only regions take source lines, and overlapping regions take human lines plus one note quoting just that region. | `hc:372` | ~40 | The exact rule-2 fallback (H4's limitation) |
| M4 | **Cost escalations keep the plan.** `most-pages-changed` and `escalated-most-pages` become tier 2 with every page regenerated. | `incremental.py:331`, `writer.py:684` | ~8 | Fewer re-plans |
| T1 | **Honest writer telemetry.** Report `pages_changed` and `pages_expected` (patch + regenerate + retitle), and WARN when they diverge. Fill `human_edits_overwritten` or remove it. | `writer.py:715-763`, `publisher/pipeline.py:1048` | ~12 | F8; a blast-radius alarm |
| T2 | **Post-generate lint, WARN then FAIL.** Check: no note inside a table; no note in the Appendix; balanced `<tr>`; no bare `<td>`; line 1 is `# {plan title}` and titles are unique; every note's payload occurs in `pure`; a "削除" Appendix entry comes only from window4 or the model. | end of `generate()` | ~25 | §2.1 invariants; `markdown_integrity` |
| U5 | **Per-document publish isolation.** Loop `publish_documents` per document inside `_publish_sweep`. Do not thread `only_pages` instead, because that skips `_trash_under`. | `publisher/pipeline.py:587-665` | ~15 | Limits the blast radius of G9 |
| U6 | **Ownership by page id when the stamp is gone.** If the page id and path match the ledger and the page has zero stamps, treat it as ours. Never decide on the path alone. | `client.py:565,729,1070,1113,1204` [A] | ~20 | A select-all replacement no longer blocks |
| U7 | **Carry links across a capture.** Keep the manifest row. Make `_undo_edits` skip instead of fail when neither the line nor the token exists (`inline_linker.py:306-308` [V]). Re-offer the old (anchor, url) pairs first. | `hc:874-880`, `inline_linker.py` | ~20 | Most of the 19/39 link churn |
| U9 | **Pass the full `page_paths` map to `pull_changes`**, not only the selected pages. | `publisher/pipeline.py:422-426`, `:521` [A] | ~6 | Permalinks captured as human links |

### Tier C: needs an owner decision (§8)

- **U4. A page deleted in GROWI becomes a human decision** (~35 LOC [A]).
  - At `client.py:1196-1199`, when a baseline exists and the mode captures:
    - read `raw_rel` first;
    - call `store.accept(..., base=<published page without linker links>, human="")`. `_commit` already does not
      publish a blank page (`hc:851`);
    - mark the ledger row so `assert_known_revisions` and `_trash_under` skip it.
  - Also decide what a later source change to that page does (D1). Today `rebase` would recreate it as a note-only
    page.
  - Recommended (~4 LOC in `generate()`): a page with blank `current[p]` and non-blank `pure[p]` stays blank. The
    source change is logged and listed in `human status`.
- **U8. A page moved in GROWI.** Either keep the block (isolated by U5), or capture its content and `rename_page` it
  back (~20 LOC [A]).

---

## 5. What the glue guarantees: the structure-reconciliation contract

The UI wins on structure and on facts. No source fact is dropped: each one is either in the text or in exactly one
note.

| Source (mount) does | Human (UI) did | After the glue | Mechanism |
|---|---|---|---|
| nothing | anything | the human version, byte for byte | exact / window2 (`rebase(x,y,x)==y`) |
| changes a fact | nothing there | the new value, no note | window2/3 |
| changes something else in the same window | edited nearby | both, no note | H3 `merge()` |
| changes fact S→S2 | set the same fact to H | H plus **one** note with S2. S2→S3 replaces the note; a catch-up removes it. | window5 + H1, H5 |
| only reformats (re-levels, re-spaces) | edited that line | the human version | H2 step 2 |
| rewords a block | deleted it | stays deleted, with one note showing the new wording (case 43) | window5, H5 token rule |
| deletes a block | deleted it too | nothing: no note, no Appendix | window1, H1 |
| deletes a block | edited or added inside it | the human lines in the Appendix, "削除されました", only with deletion evidence | window4 / model + H5 |
| rewrites the whole page (tier 2/3) | edited anywhere on it | same file name, a section-sized window on the same page, merge or a bounded fallback; never a false Appendix | M1, M3, H2-H5 |
| splits, merges or renames pages | edited an affected page | text follows content, its own page (H2), or the page it moved to (H7); otherwise the Appendix with "位置を特定できません", never "削除" | H2, H6, H7 |
| adds table rows | added a row | both rows, a valid table, no note inside the table | H3/window5 + H5 |
| anything | replaced the whole page body | captured (U6 covers the stamp loss); whole-page merge or bounded fallback | H4, U6 |
| anything | deleted the page in GROWI | blocked today; with U4 the page stays deleted (D1) | U4 |
| anything | made a format-only edit | guidance stored and fed to the writer; survives renames | classify (existing), H7 |
| anything | removed a bot link | the linker re-adds it (known limitation 9; U7 preserves the rest) | — |

---

## 6. Case coverage: what moves

These are the 62 `LIVE_CASES` (`publisher/live_verification.py:21` [V]). "Today" is the work-PC status [A]. Cases not
listed either pass already or are out of scope.

| Case | Today | After | By |
|---|---|---|---|
| 2 `normal_update` | FAIL: a 9-page patch became a 28-page rebuild | pass | M1, H8 |
| 9 `disjoint_edit` (in one window) | false note [F] | pass, no model call | H3 |
| 10 `contradiction` | PARTIAL: note in `<tr>`, `---` payload, multi-block note | pass | H5, H4 |
| 11-13, 51 resolve keep/accept/combine | untested | pass | H1 |
| 16 `tier_1` | PARTIAL: H1 dropped | pass | HEAD line-1 check, M2 |
| 17 `tier_2`, 60 `regeneration_without_fact_change` | FAIL | pass | M1, H2, H5 |
| 18 `tier_3` | FAIL | pass | M3, H2-H7 |
| 19 `split_combine_rename_reorder` | FAIL / PARTIAL | mostly pass | M3, H2, H7 |
| 20 `remote_move`, 21 `remote_delete` | blocks | isolated (U5); decided (U4/U8) | Tier C |
| 27 `markdown_integrity` | PARTIAL: link residue | pass | U1, H5, T2 |
| 43 `human_delete_source_modify` | PARTIAL: bad note | pass | H5, H1 |
| 44 `human_modify_source_delete` | FAIL both ways | pass: window4 for a fully deleted part, otherwise the text stays on the page | H2, slim H5, H6 |
| 55 `conflict_survives_unrelated_edit` | untested | pass | H1 |
| 56 `fast_policy_human_overlay` | FAIL: links | improved; pass with U7 | U1, U2, U7 |
| 58 `source_catches_up` (with a note present) | model luck | pass | H1, H4 |
| 59 `source_removes_then_restores` | BLOCKED: no valid Appendix ever | testable | H5, H6 |
| 61 `structure_only_edit` | unproven | provable with a format-only UI edit | H8, H7 |

---

## 7. Verification

### 7.1 Offline, on the work PC

1. Restore `tests/` (P0.3) and make it green before Tier A.
2. Add tests next to `test_human_changes.py`, reusing its `FakeModel` and `NoModel`:
   - **H1:** each row of the H1 table, plus the in-cell `…）</td>` form.
   - **H3:** a comma change plus an insertion against a source deletion elsewhere in the window gives no note and no
     model call. A source edit inside text the human deleted still goes to the model.
   - **H4:** the fallback drops an untouched line the source deleted. A link-only difference gives no note. Partial
     table rows quote the whole window.
   - **H2:**
     - a generic heading on two pages is not hit across pages;
     - `## X`→`### X` with a human rename gives the human version;
     - a fully reworded page plus one insertion gives a same-page window and no Appendix;
     - the deleted "moved section" test stays green.
   - **H5:** a fake model returns each bad shape (note inside `<tr>`, `---` payload, note in the appendix, `<td>`
     fragment, untouched old text kept, reworded passage declared deleted, value dropped). Also the four token-rule
     rows.
   - **H6:** the footer follows the reason, and `_entries` counts both footers.
   - **H7:** a merged page (004 + 005 → 004-…〜-005) maps; a 29% overlap does not.
   - **U1:** `(1-2).md` targets in all three regexes.
   - **M1:** a ja-5 sidecar resumes under `require_resume`.
   - **M2:** line 1 is the plan title.
   - **M3:** an inserted section keeps its siblings' names.

### 7.2 Replay harness from real data (no LLM: `model=None`)

- In `data/hc-e2e-fast`, every `build op-…` commit that touched `metadata/human-sync/doc/<key>/pure` is one generate
  round: `old = <c>^:pure`, `human = <c>^:current` (minus the Appendix), `new = <c>:pure`. List them with
  `git log -- metadata/human-sync/doc/*/pure`; for example `9f561bb`, `c289110`, `f2fb63b`, `c9a3d67`.
- Replay `rebase` before and after Tier A and assert:
  - no fact gets a second note;
  - no `元文書の更新` in the Appendix;
  - no note inside a table;
  - no Appendix entry from a page that still exists;
  - no `no_location` for pages present in `new`;
  - pages 001 and 003 behave as in §2.1 after the fix.
- Do the same with the `test_conf` triples on the work PC. `conflicts-failure.md` §11 already replays 007 and 019
  deterministically.

### 7.3 Live: the next round, with corrected expectations

- Run one mount-only round against sites **without** a standing human decision and one **with**. Write down the
  expected notes per site before running.
- Replace `conflicts-failure.md` §9 invariant 1 with two rules:
  - every note's quoted text occurs in that page's `pure`;
  - every note sits where `current` differs from `pure`.
- Keep invariants 2-9.
- Count markers in `current/` and in `wiki/*.md` only, not in `_planning/`.
- For case 61, make one **format-only** UI edit, then check `doc.json` and `work/page-NNN/rewrite-01-prompt.md`.
- Read the `human_sync event=rebase stats=…` line (H8) for every document. Every `no_location`, `fallback` or `appendix`
  count must be explainable from the round's edits.

---

## 8. Decisions only the owner can make

- **D1.** A page deleted in GROWI, then the source changes that page. Options:
  - (a) It stays deleted; the change is logged and listed in `human status`. **Recommended.**
  - (b) It comes back as a note-only page. This is what `rebase` does today once U4 captures the deletion.
  - (c) The change goes to the Appendix.
- **D2.** A page moved in GROWI: keep blocking (isolated by U5), or move it back (U8).
- **D3.** Generated prose the human had seen, which the source drops without a note (`conflict-cases-handoff.md` §8.4,
  B-004). Keep that, or require a note.
- **D4.** H4's fallback still quotes a wording-only difference. Accept that, or suppress quoted lines with no
  `_facts()` difference (+3 LOC).
- **D5.** The ja-6 prompt clauses. Keep them (with M1 they cost nothing), or rely on H5, which covers the same shapes
  deterministically.

---

- **Decided (2026-10-20):** the model may not move text into the Appendix (slim H5).

## 9. Don'ts (consolidated)

- Do not re-enable the model's `appendix` field without its old guards: deletion evidence, no notes, no table fragments, and only lines the human changed.

- **No decision ledger and no second copy of `pure`.**
  - Do not change `pure := new`.
  - Do not pass anything else as `old`: it breaks `rebase(x,y,x)==y` and resurrects deletions (`conflicts-failure.md`
    §4 replay).
- **Notes.**
  - Do not strip or expire notes in window2, exact or window3, or by age or counter. Do not strip them from `hlines`
    globally. A note lives while the source still says what it quotes (H1).
  - Do not use `取り込み=0 ⇒ 0 notes` as a gate.
- **Merging.**
  - Do not use `merge()`'s `conflict` output (it is the whole human text).
  - Do not let it silently drop a source edit inside human-deleted text.
  - Do not splice normalised text. Normalise only to locate.
  - Do not raise `LIMIT` or send whole pages to the model. Shrink the windows instead (H2).
- **Validation.**
  - Do not land H5 without H3 and H4. Validators push work to the fallback, so the fallback must be good first.
  - Do not add more `_VERIFY` calls; that doubles the cost per window. Add deterministic checks instead.
- **Identity and placement.**
  - Do not enable `retitle` (`incremental.py:342`) or rename files on retitle before M2 and M3.
  - Do not identify pages by fuzzy title or by an LLM match.
  - Do not use the `llm-wiki-bot-ref` stamp to anchor `rebase`; a stale stamp anchors on the wrong page with confidence.
  - Do not lower `PATCH_MAX_CHURN`. Rewording is what kills anchors.
- **Publishing and links.**
  - Do not thread `only_pages` through `publish_only` without keeping `_trash_under`.
  - Do not make `unlinked` line-number-based, and do not delete its "not in current" rule; it protects generator
    navigation links.
- **Process.**
  - Do not let `classify` influence merging (design §3.6).
  - Do not land Tier A without H8. Two simultaneous unattributed changes is how the Appendix pendulum swung.

---

## 10. Other findings (not part of the glue; worth a ticket)

- **An interrupted tier-1/2 update can publish stale pages** [A]; this was read in the code, not reproduced.
  - `apply_update` and the `old_source` write both run *before* the patches (`writer.py:669-670` [V]).
  - If the run is then cancelled and resumed, the next decision is tier 0.
  - Fix: write a pending-update marker before `:669`. Do not just move the `old_source` write; `apply_update` is not
    idempotent.
- **The `retitle` / `structure-changed` guard is dead for fast documents** [A]. Stored titles are policy-stripped and
  model-drifted, so `incremental.py:342` never matches. Re-evaluate after M2.
- **Tabular (xlsx/csv) builds ignore guidance** [A]. `build_wiki_output`'s tabular branch omits `human_guidance`.
- **Repetitive pages defeat find/replace merges.** On the canary, 12 identical bullets per section mean almost no
  `find` is unique, so merges fail twice [F]. H3 and H4 make this harmless.

---

## 11. Runtime impact

No change adds a new model call or a new retry loop. Pure-Python checks cost milliseconds.

| Effect | Changes | Why |
|---|---|---|
| **Saves time** | M1 | No accidental full rewrites. R2 rewrote 28 pages instead of patching 9. |
| | H3 | A clean 3-way merge skips the merge and verify calls, about 2 × 100 s per window on the local model (e2e handoff §4.2). |
| | H2 | Smaller windows mean smaller, faster calls and fewer `LIMIT` fallbacks. |
| | H1 | No stale-note conflicts in later rounds. |
| | U3 | No model calls on pull-side conflicts. |
| | M4, U7 (Tier B) | Fewer re-plans, fewer relink decisions. |
| **May add time, only when something was about to be lost** | M5 | Extra writer + judge attempts on a page whose rewrite dropped a value. Bounded by the existing attempt cap in the fast candidate loop. |
| | H5 | A second merge + verify attempt when the first merge lost text. Bounded by `_merge_window`'s existing 2 attempts. |
| **Negligible** | H3's pre-check | About 4 `git diff` spawns per conflicting window (`line_hunks`). |
| | H5's span rule | Milliseconds if implemented line-first. |

**Expected:**
- **Normal rounds:** same speed or faster.
- **A Stage-3-sized structural rewrite:** about ±10-15%, depending on how often the writer drops values.
- **An accidental full-rewrite round like R2:** much faster, because it no longer happens.

Confirm the numbers with H8's stats line: count `model_calls` and `merge_ok_*` per document, before and after.

## Appendix A: evidence index

| Claim | Where |
|---|---|
| `pure := new` every generate | `publisher/human_changes.py:972` |
| `_fallback`: whole `cur_w` + whole `new_w` | `publisher/human_changes.py:372-381` |
| `lost` / overlap → Appendix with an unconditional "削除" | `publisher/human_changes.py:479-488`, `:500-509`, `:558-564`, `:612-624` |
| exact match has no same-page check; anchors global-unique only | `publisher/human_changes.py:517`, `:493-494`, `:449-450` |
| `_attempt` checks | `publisher/human_changes.py:320-350` |
| `merge()` (token diff3, atomic ranges) | `publisher/human_changes.py:164-189`, `:128-161` |
| pull conflict → rebase with the model | `graph/growi/client.py:1301-1326` |
| remote delete / move / stamp → block | `graph/growi/client.py:1196-1206` |
| tier ladder, escalation, state wipe, `apply_generated` | `graph/workspace/writer.py:610-736` |
| checkpoint rejects a version mismatch | `graph/wiki/pipeline.py:1182-1206`, `:1676-1702`, `:904` |
| file names minted by position + slug | `graph/wiki/pipeline.py:239-265` |
| fast writer adopts the model's title | `graph/fast/writer.py:402-415` |
| linker skip list | `graph/fast/inline_linker.py:886-893` |
| `code_tokens` (NFKC; no bare numbers) | `graph/wiki/page.py:26-29`, `:142-162` |
| canary stacking 0→1→2; window `相対湿度`→`次のページ` | `data/hc-e2e-fast` builds `9f561bb`, `f2fb63b`, page `001-…概要.md` |
| canary cross-page heading hit; page-003 false note and resurrection | `data/hc-e2e-fast` build `f2fb63b` (agent re-trace) |
| canary page-004 stale intro via the model `appendix`; orphaned guidance | build `c9a3d67`; `…/doc/c560eb4b…/{current/005-付録.md, doc.json}` |
| deleted tests and design doc | `git show 3870336^:llm-wiki-air/tests/…`, `…/docs/handoff-conflicts.md` |

## Appendix B: implementation order (one PR, one commit per step)

Each step can be reverted on its own. Run the restored tests and the §7.2 replay after every commit.

**Core (~170 LOC):**
1. H8 telemetry
2. U1 + U2 link regexes and linker skip list
3. M1 stale prompt version
4. M2 title pin. **Live round 1 after this step**, to confirm the round stays small.
5. M5 rewrite fact check
6. H1 notes
7. H3 `merge()` pre-check
8. H4 fallback
9. H2 placement
10. H5 (slim): merge checks, no model Appendix
11. U3 pull-side notes. **Live round 2 after this step**: a Stage-3-sized structural rewrite.

**Complete Tier A (+~55 LOC):**

12. H6 honest Appendix
13. H7 page remap
14. M3 file-name pinning. **Live round 3** after the complete set.

# Implementer notes

The implementation followed Tier A and the P0 test-restoration work, but deviated from this plan in the following
ways:

- **The implementation was larger than the Appendix B estimate and was not split into one commit per step.** The
  implementation/config/test-extension diff was about `+732/-109`; restoring the plan-required historical tests
  added about 7,672 more lines. Work was kept uncommitted in the outer repository so the existing worktree was not
  rewritten. The larger production diff is concentrated in `publisher/human_changes.py`, where the real conflict
  cases required preserving structured regions rather than only adding the slim checks described above.
- **HTML tables are preserved atomically during deterministic fallback.** This is broader than H4/H5's prose and
  was added after a live whole-table rewrite left a naked `<td>` and after a delete-only human row change risked
  resurrection. The fallback now retains the complete human-edited table and places conflicting generated source
  material outside it.
- **Markdown table validation excludes separator rows.** The planned value/table gate was implemented, but its
  first version counted `|---|---|` as data. That rejected otherwise complete candidates from the real model, so
  divider rows are now ignored when comparing table row counts.
- **The fast writer's output budget is capped by `write_max_output_tokens` (default 16,000).** This was not in the
  plan. The configured endpoint advertises a 42,000-token context and rejected the existing hard-coded 64,000-token
  output request, so the cap was necessary to run the requested model at all.
- **Pull-side transport-conflict rebasing is deterministic and model-free.** This is consistent with U3's intended
  outcome but more explicit than its implementation notes. It avoids inventing human edits during a retry and keeps
  pull conflicts from adding an unplanned model call.
- **Link handling accepts nested parentheses.** U1/U2 required link robustness, but the nested-parenthesis case was
  added specifically because it appeared in an aggressive live human edit.
- **Tracked example/test credentials were scrubbed beyond the direct glue files.** This was outside the functional
  plan, but leaving exposed credentials in tracked configs and Markdown was an avoidable security risk. No new
  credential mechanism or dependency was introduced.
- **The live GROWI edits were made through its API, not browser clicks.** They exercised the real GROWI server,
  revision conflicts, ownership-marker damage, remote moves, delete blocking, and `off`/`observe`/`apply` behavior,
  but they do not satisfy any handoff requirement that specifically demands browser-originated human evidence.
- **One pre-existing canary remains blocked as `remote_deleted`.** It was intentionally not recreated because doing
  so would override the operator decision represented by a remote deletion. Other aggressive mount/UI combinations
  were tested on the disposable `hc-e2e-fast` project.
- **Tier B and Tier C were not implemented.** They remain deferred as the plan specifies; no new framework,
  dependency, retry loop, or unrelated feature was added.
