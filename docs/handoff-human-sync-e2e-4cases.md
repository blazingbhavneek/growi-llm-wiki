# Handoff: end-to-end QA of human-edit conflict handling with real company documents

Written 2026-10-19 on the work PC by the agent that did the design reading and the corpus
inspection. **This is a plan plus a briefing, not a result.** Nothing in parts III has been
executed yet.

Who this is for: an agent or QA operator who has never seen this feature, has access to this
repository, the test GROWI instance, and a human tester who can edit pages in the GROWI browser.

What you are verifying: when a source document in the mount folder and a human editing in GROWI
both change the same content, the pipeline must combine them without ever losing human
information, without silently replacing source truth, and without writing over a remote page it
did not inspect.

Companion documents (on `feature/growi-search-app`, not in this checkout):

- `docs/handoff-conflicts.md` — the design. Section 2 the rules, 3.2 `rebase()`, 3.3 pull,
  3.4 generate, 3.5 model calls, 3.6 Appendix, 3.7 sync order, 3.8 deletes/identity/rollback,
  3.9 fast path, 7 publication races, 8 activity, 11 known limitations, 13 the 62-case matrix.
- `docs/handoff-conflicts-tests.md` — the previous executable handoff, tool usage, fault
  injection, and the last audit (12 of 62 live cases, home PC, API edits; historical only).

Code under test:

- `publisher/human_changes.py` — `merge()` at 164, `rebase()` at 443, `HumanStore` at 665,
  `generate()` at 946, `apply_generated()` at 1237, the three model-call wrappers at 266-305.
- `publisher/live_verification.py:21` — `LIVE_CASES`, the 62 case IDs. Never rename or drop one.
- `publisher/pipeline.py`, `publisher/ahead.py` — candidate worktrees, publication, rollback.
- `graph/workspace/writer.py` (`publish_output()`, guidance injection),
  `graph/fast/repair.py`, `graph/fast/inline_linker.py`, `runner/cli.py` (the verbs).

## How to use this document

Read sections 1-4 once, skim 5, then work from 6-11 as reference and 12-16 as the script. Section
17 is what you hand back, 18 is what you consult when something is wrong, 19 is the coverage
contract, 20 tells you what state the machine is in right now, 21 lists what only the owner can
answer. If you only have time for one thing: sections 12, 13 and 14 are the test, everything else
supports them.

**Contents**

| Part | Sections | What is in it |
|---|---|---|
| I — Orientation | 1 safety rails · 2 glossary · 3 the mechanism in full · 4 known limitations · 5 code map | how the feature actually works, so a surprising result can be explained instead of guessed at |
| II — Environment and corpus | 6 project, config, clean baseline, logs, verbs · 7 Doc A in full with a 28-page map and the hazards · 8 Doc B · 9 corpus gaps and how to close them · 10 the `tools/hc_live` contract · 11 the offline gate to run first | everything you need before touching anything |
| III — Test plan | 12 round protocol · 13 the v1-v4 mount edits · 14 the four cases · 15 Case 5 (needs Doc B) · 16 optional ownership checks · 17 reporting · 18 triage · 19 all 62 cases mapped · 20 current machine state · 21 open questions | the executable script and its bookkeeping |

The five things to remember if you read nothing else:

1. Mode `off` and `observe` must **block**, not capture, with zero model calls.
2. A human edit must be typed and saved by a second person in the browser; an API edit never counts.
3. "Once" means `growi 1, current 1, live 1`, checked with a marker word — never by eye.
4. Every meaningful round is followed by an unchanged repeat round that must be a no-op.
5. On any sign of lost or duplicated human text: stop, keep everything, report.

---

# PART I — ORIENTATION

## 1. Safety rails, in order of how much damage they prevent

1. **Do not touch the production GROWI tree.** This project publishes to
   `http://10.160.152.38:3000/` under `/test/...`. Both source documents are 社外秘 and their
   page titles carry that word plus internal system and equipment names. Touch only pages this
   project created, confirmed by the ownership marker and `metadata/pipeline.json`.
2. **Never read or enumerate page bodies below a subtree that holds private content.** `count.py`
   and `edit.py` accept `--page`/`<local_path>`; always use them. Never run an inventory pull over
   `/test/` without explicit owner approval — inventory can fetch every owned page body.
3. **Never put private content in a report, a log excerpt, or a commit message.** No page bodies,
   no source text, no tokens, no authenticated URLs. Record hashes, page IDs, revision IDs,
   reason codes, and redacted paths instead.
4. **The live token in `configs/test.ini` is a mistake.** Move it to `.env` as `GROWI_TOKEN` or an
   approved secret location before this document or the INI is shared with the QA tester.
5. **Mode discipline.** Start at `off`, validate `observe`, then `apply` for the rounds. Only the
   rollout operator flips modes. A remote difference blocking in `off`/`observe` is expected
   behavior, not a failure.
6. **`human live-plan` is an operator gate, not a runtime interlock.** It writes a report and a
   confirmation code, but normal `sync`, `publish`, and `reset` do not consume that code. Before
   any write/delete/move check, verify the endpoint and the resolved boundary yourself.
7. **Stop immediately** on: lost human text, duplicated content, an unexpected overwrite, delete
   or move, a corrupt snapshot, an unexplained revision mismatch, a cursor gap, or a retry that is
   not idempotent. Keep the data root and the remote revisions for investigation. **Never
   `git reset`** the project data root; its Git history is your audit trail.
8. **Wait for the in-flight run to finish** (section 20) before starting anything, and never run
   two syncs against the same project at once.
9. Human edits must be saved by the **separate tester account in the GROWI browser editor**. An
   API write (`edit.py`, `human.py`) is a pre-check that saves the same way (`origin: editor`) but
   it never counts as real-user evidence and never turns a case into a pass.

## 2. Words used in this document

- **Source / mount** — the original files in the configured `source_mount` folder. Changing a file
  there is a "source change" (X).
- **Human edit** — someone saves a page in GROWI (Y).
- **`pure/`** — what the generator last produced, per page.
- **`current/`** — the same pages with every human change applied. Diffing `pure/` against
  `current/` recovers the human's intent at every sync.
- **Capture** — accepting a changed GROWI revision into `current/` plus a capture record.
- **Note** — `（元文書の更新: …）`, or a quoted `> **元文書の更新:**` block when the source text is
  long. It appears when human and source changed the same fact differently: human value stays,
  source value shown.
- **Appendix** — the extra last page `NNN-付録.md` holding human text whose source content was
  deleted.
- **Guidance** — a one-line "what the human wants this page to look like" instruction stored in
  `doc.json` and injected into the writer prompts for that page.
- **Anchor / window** — an *anchor* is a unique unchanged line above or below a human change used
  to relocate it; the *window* is the text strictly between the two anchors, which is the unit the
  model merge call works on.
- **Exact match** — a change whose old text is unique, line-aligned, and present in both old and
  new. Relocated mechanically, no model call.
- **Ownership marker** — an ID embedded per page identifying the page as ours; a damaged or
  duplicated marker blocks capture and adoption.
- **Transport rebase** — the 3-way merge (`previous_remote`, `remote`, `previous_local`) that
  separates pipeline re-formatting from real human edits, done before the human-change logic runs.
- **Candidate / last-good** — a sync runs in a candidate worktree; `refs/llm-wiki/last-good` is
  the last clean checkpoint. Durable paths are the tracked state folders.
- **Blocked** — a page or document the pipeline refuses to touch, with a reason. Re-checked on
  every pull, so it unblocks when the cause disappears.
- **Constrained** — the environment could not produce the condition (no permission, no fault
  injector). A constrained case is **not** a pass.

## 3. The mechanism, in full

### 3.1 Where the state lives

`wiki/<document>/` belongs to the writer and is deleted wholesale by `publish_output()` on every
export; `metadata/state/` is deleted on a full rebuild. Human state therefore lives below
`metadata/human-sync/`, which no writer touches and which the project Git already tracks:

```
metadata/human-sync/
  schema.json
  doc/<key>/                      key = sha256(source_id) of the document
    pure/<page>.md                generator output as of the last sync
    current/<page>.md             that page plus every human change (and the Appendix)
    doc.json                      {source_id, raw_rel, appendix: "<file>"|null, guidance: {page: [..]}}
    captures/<sha>.json           one record per accepted GROWI revision:
                                  {page, revision, base_blob, human_blob, guidance}
  pages/<marker_id>.json          per-page publication evidence: page_id, remote/local revisions
                                  and blobs, attempt history, blocked reason
  snapshots/<sha256>.md           content-addressed page bodies referenced by the records above
  documents/<key>.json            LEGACY journal; if present with a live edit, the document blocks
```

Two consequences the tests exploit: after a full regeneration the human state survives, and the
state is auditable by reading plain files.

### 3.2 The five merge rules

At each sync, X is what the source changed and Y is what humans changed:

1. X none, Y changed: keep Y.
2. X changed, Y none: apply X.
3. X and Y made the same change: keep it once, no note.
4. X and Y changed the same fact differently: keep Y, add X as `（元文書の更新: …）`.
5. X and Y changed different facts: both coexist.

Plus two designed behaviors:

- **Source deleted something a human changed** → the human's changed text moves to the Appendix
  page (last page number + 1), so the current structure is undisturbed and nothing is lost.
- **Structure-only human edits** (formatting, heading fixes, duplicates removed, reordering) add
  no information: they become writer guidance *and* are still merged like any other change, with
  no note for formatting differences. A wrong classification therefore cannot lose information.

A human change is re-applied on every later sync until the human reverts it or the source catches
up; then rule 2 applies again. Conflicts are resolved by editing the page in GROWI — there is no
CLI resolver.

### 3.3 `rebase(old, human, new)` — the deterministic core

Called by pull, by generate, and by rollback replay. Each argument maps page file names to text
for one document; it returns merged pages plus Appendix entries. Terms: *line-aligned* means
starting at a line start and covering whole lines; *unique in a map* means exactly one
line-aligned occurrence across all pages of that map; *blank* means whitespace only.

1. **Changes.** For each page, diff `old[p]` against `human[p]` (missing means unchanged) with
   `difflib.SequenceMatcher(..., autojunk=False).get_opcodes()`. Each non-equal opcode is one
   change: old lines `B` → human lines `A`, either possibly empty. Line endings are kept.
2. **Locate** each change against the *new* text:
   - **Exact:** `B` non-blank and unique in `old` *and* unique in `new`. This is how a change
     follows a section the source moved or reordered, including onto a different page.
   - **Window:** otherwise, scan upward in `old[p]` for the first line that is inside an equal
     opcode (the human did not touch it) and is unique as a single line in both `old` and `new` —
     the *before-anchor*. Scan downward the same way for the *after-anchor*. Both must exist in the
     same new page, in order. A missing anchor may be the start or end of `new[p]`, but only when
     `p` exists in `new` and the other anchor is in `new[p]` or also missing. The window gives
     three texts: `old_w`, `cur_w` (human side; anchor positions carry over because anchors are
     unchanged lines) and `new_w`.
   - Otherwise the change has **no location**.
3. **Overlaps.** A change lying inside another change's window on the same old page is absorbed —
   its human text is already inside that window. Two remaining locations that overlap in `new` are
   merged into one window (union of new text, both sides' old and human texts in order).
4. **Decide,** against the original `new`:
   - Exact → replace that occurrence of `B` with `A`.
   - Window, first matching case:
     1. `new_w == cur_w` → nothing to do (rule 3, or the source caught up).
     2. `new_w == old_w` → replace `new_w` with `cur_w` (rules 1 and 5, and pure insertions).
     3. Every change in the window has a non-blank `B` occurring exactly once, line-aligned, in
        both `old_w` and `new_w`, without overlapping → replace each with its `A`. (A repeated `B`
        could match the other copy, so it falls through to 4 or 5.)
     4. `new_w` blank and `old_w` not (the source removed this part) → append each non-blank `A` to
        the Appendix and leave `new_w` alone.
     5. `old_w` and `cur_w` differ only in whitespace (an insert-then-delete leaves a blank line) →
        keep `new_w`, **no model call**.
     6. Otherwise → the **merge call**. If it fails, replace `new_w` with `cur_w` followed by the
        source note. Independent windows run in parallel up to `concurrency`.
   - No location → a non-blank `A` goes to the Appendix; a blank `A` (the human deleted text that
     can no longer be found) needs nothing.
5. **Apply** all replacements to each new page from the last position to the first.

Two properties hold with **no model call**, and both have offline tests:
`rebase(x, x, z) == z` and `rebase(x, y, x) == y`.

### 3.4 Pull: accept Y

For each owned page whose GROWI revision changed (preflight, ownership, revision, prepared
evidence and mode checks all stay as they are):

1. **Transport rebase:** `canonical = merge(editable(previous_remote), editable(remote),
   editable(previous_local))`, then restore page links. On conflict, block the page. Local
   spelling inside text the human deleted is ignored, so deleting a paragraph containing inline
   code or a comment is not a conflict. `previous_local` is the page as actually published.
   A blocked page is re-checked every pull, so it unblocks on its own when the cause is gone.
2. **Remove linker output:** `L` = the Markdown links `[text](url)` (not images) present in
   `editable(previous_local)` but absent from the current page. `base = unwrap(previous_local, L)`,
   `human = unwrap(canonical, L)`, where `unwrap` replaces each such link with its text. Links a
   human added are not in `L` and are kept.
3. `base == human` → transport-only or link-only revision: record the revision, change nothing.
   **This is the "re-save without editing is not a human change" test.**
4. `off`/`observe` → block or observe. No model call.
5. `apply`:
   1. Legacy check, then bootstrap if the document has no state: copy `_planning/pages/*` (the
      live page if missing) into both `pure/` and `current/`.
   2. `rebase({page: base}, {page: human}, current pages)` → new current pages plus Appendix
      entries. Normally `current == base`, so this returns exactly `human` with no model call. When
      the local generation is newer than the published one (a build whose publication failed), the
      human change is applied **on top of** the newer local pages instead of replacing them.
   3. Classification call on the `B → A` changes; add the resulting guidance.
   4. Write the capture record, `current/` and its mirrors, `doc.json`. Drop rewritten pages from
      the fast inline-link manifest. Mark the linker pending. Record the revision.

The Appendix is pulled like any other page, so humans can edit or delete its entries.

### 3.5 Generate: apply X

`apply_generated(project, raw_rel)` runs after the writer (or fast repair) exported new pages with
`publish_output()` — at that point the live `*.md` files are exactly the new generator output.

1. Legacy check and identity check.
2. No state: keep the new pages, write them to `_planning/pages/`, write no state.
3. State: `rebase(pure, current pages without the Appendix, new pages)`. Output = merged pages
   plus the Appendix with new entries appended, renamed if the page count changed. Write every
   output page to `current/` and (unless blank) to the live folder and `_planning/pages/`. Delete
   files that are not output pages. Replace `pure/` with the new pages. Drop rewritten pages from
   the inline-link manifest.
4. Return `changed_pages` = output pages whose text differs from the exported page, plus removed
   or renamed pages. The fast repair and the writer's linker decision read that attribute.

If the writer exported nothing, the document did not change and nothing runs.

### 3.6 The three model calls

All three use `structured_ainvoke` with the standard chat configuration, thinking on, merge windows
at most `concurrency` at a time, and **no prompt truncation**: an input over 30,000 characters is
not sent and the call takes its fallback directly.

**Classify** (pull step 5.3). Input: the `B → A` changes of one accepted revision. Output per
change: `kind` = `content` or `structure`, plus for `structure` a one-line instruction in the
page's language. *Content* means a reader learns something different (a value, fact, name, step or
warning added, changed or removed); everything else (formatting, heading wording or level, moved
sections, removed duplicates, table layout) is *structure*. Instructions append to `guidance[page]`
(exact duplicates skipped, last 20 kept). **Classification only produces guidance; it never changes
how a change is merged.** If it fails, no guidance is added and nothing else is affected.

Guidance reaches four prompt sites: `section_write_prompt`, `intro_prompt` and
`incremental_page_edit_prompt` in `graph/wiki/prompts.py` (callers `graph/wiki/pipeline.py`,
`graph/workspace/writer.py`, `graph/fast/writer.py`) and `_writer_messages` in
`graph/fast/repair.py`. With no guidance the prompt bytes are unchanged, so caches stay valid. If
the writer obeys the guidance, `rebase` finds nothing to do; if it does not, `rebase` re-applies the
change and the merge instruction says formatting differences get no note.

**Merge** (window case 6). Input `old_w`, `cur_w`, `new_w`, guidance. Output
`{"edits": [{"find": "…", "replace": "…"}], "appendix": "…"}`; edits apply to `new_w`, so source
text outside them survives byte for byte. Instructions: apply the human change to `new_w` and keep
every fact in `new_w`; only what differs between `old_w` and `cur_w` is the human's change — text
identical in both is old source text and `new_w`'s version wins with no note; a same-fact
difference keeps the human value and adds the `new_w` value as `（元文書の更新: …）` (always this
exact text so later merges can find it); wording-only differences get no note; an existing note for
the same fact is replaced, not doubled; if `new_w` no longer contains the text the human modified,
put the human's changed lines in `appendix` instead.

Before accepting, three mechanical guards: every `find` is non-empty and occurs exactly once in
`new_w` with no overlaps; every digit sequence present in `cur_w` but not `old_w` appears in the
result or the Appendix; and the **verify** call returns true for both checks.

**Verify.** Input `old_w`, `cur_w`, `new_w`, the result and the Appendix. Output
`{human_kept, source_kept}`. `human_kept`: everything the human added or changed is in the result
or the Appendix. `source_kept`: every fact in `new_w` survives as text or inside a note, and a fact
the human did not touch appears with `new_w`'s value as text, not only inside a note.

Any failure retries the merge once with the failure reason; a second failure takes the fallback:
`cur_w` followed by the source note — `（元文書の更新: X）` on its own line when `new_w` is a single
line under 200 characters, otherwise `> **元文書の更新:**` with every line of `new_w` prefixed by
`> `. Both versions stay visible either way.

### 3.7 The Appendix

- **Name.** N = highest page number among the document's other output pages (leading digits of
  `NNN-Title.md`; unnumbered pages like `index.md` do not count; 0 if none) plus one, same zero
  padding. For Doc A today, `029-付録.md`.
- **Identity.** It is whatever `doc.json` names as `appendix` — never recognized by pattern. A
  generated page titled 付録 is an ordinary page.
- **Storage.** Lives in `current/` like any page, has no `pure/` copy, and is left out of the
  generation rebase. Carried over unchanged, then new entries appended. In pull it is an ordinary
  page, so human edits and deletions of entries are kept.
- **Entries** are only appended, and an entry present byte for byte is not repeated:

  ```markdown
  ## <nearest heading at or above the change, or the original page file name>

  <the human's changed text, verbatim>

  > 元の文書では、この部分は削除されました（元ページ: <original page file>）
  ```

- **Rename.** If N changes, rename it in the same run (`current/`, live folder,
  `_planning/pages`, `doc.json`). The publisher creates the new page and trashes the old one with
  its expected revision checked. Pull runs first, so an Appendix edit landing in between fails the
  revision check and blocks rather than being lost.
- **Restore.** If the source later restores the removed part, the restored source text appears
  normally and the Appendix entry stays until a human removes it.

### 3.8 Sync order

1. Lock and require a clean last-good.
2. Interrupted-operation recovery.
3. **Pull GROWI changes first**, before any scanning, building or publishing — so a UI-only edit
   is captured even when no source changed. Skipped when the endpoint or marker format changed
   (that case is a republish). Pull failures are recorded and the sync continues; that document
   blocks again at step 5, others proceed.
4. Scan, convert, generate (`apply_generated` per document).
5. Per-document late-edit capture before that document's publication (same pull code).
6. Link, preflight, publish, checkpoint.

### 3.9 Deletes, identity, rollback

- **Human information** = some `pure`→`current` change has a non-blank `A`, or the Appendix has
  entries. **Pure human deletions are not human information.**
- **Document delete** is blocked while the document has human information (its Appendix is inside
  the folder being deleted). Remove or move that text in GROWI; the next pull captures it. Applies
  to bulk deletes.
- **Document move** keeps the state key (source identity), and `move_sources` rewrites `raw_rel`,
  freeing the old path.
- **Different source at the same path** gets a new key and inherits nothing; if another key holds
  human information under that `raw_rel`, the new document blocks too.
- **Candidate rollback** replays captures onto live *before* `restore_publication`, in capture
  order, using each record's blobs — otherwise a human edit captured inside a failed candidate
  whose page had already been republished would be erased by the restore. The candidate's Appendix
  is deliberately not copied (the replay already restores those changes; copying would duplicate
  them). An edit on the candidate's own Appendix page replays onto live's Appendix.

### 3.10 What is unchanged, and the fast path

Unchanged: GROWI publication safety (ownership markers, whole-batch preflight, compare-and-swap
with `origin: view`, HTTP 409 abort with no retry, prepared/confirmed evidence, lost-response
recovery, delete inspection); activity detection and forced inventory; the three modes with
INI-over-environment precedence; linker, index, candidate worktrees, last-good.

Fast path (`sync --fast`, `--fast --link`, `--fast --repair`) uses the same hooks: fast page
writing runs inside the normal writer, which calls `apply_generated`; `--fast --repair` calls
`publish_output()` then `apply_generated` directly. The inline linker writes links only into live
`*.md`, never `_planning/pages`, so pull strips them as linker output (3.4 step 2) — that is why
`--fast --link` must never appear to invent a human change.

- The inline-link manifest (`metadata/cache/fast-inline-links/manifest.json`) undoes its own links
  before relinking; `_undo_edits` fails the whole run with `could not locate generated link` if a
  listed link and its original line are both gone. Pull and `apply_generated` must delete the
  manifest rows of any page they rewrite.
- A resumed `--fast --repair` deliberately skips the GROWI pull. A human edit meanwhile is not
  lost: preflight rejects the changed revision and the next pull captures it — it appears one sync
  later.

### 3.11 Publication, races, and lost responses

Before a batch write, `publish_pages()` fetches every destination and validates the whole batch:
expected page exists; page ID, path, revision and ownership match; a new destination that already
exists is owned and reconcilable; no two local pages map to one remote path. A foreign page is
never adopted just because the path matches. Updates send `origin: "view"` plus the inspected
revision; HTTP 409 records a race and aborts with no second PUT. Deletes inspect ownership and
expected revisions first.

Before each create/update the store records an attempt ID and the exact prepared bodies; after the
response it records confirmation. On restart, an exact body match adopts a lost response as the
bot's own write, an unchanged revision retires it, and anything ambiguous blocks.

### 3.12 Activity detection and inventory

One-shot `pull` uses `ActivityDetector` as a hint. The cursor is keyed by endpoint and boundary and
stores the last timestamp, the IDs at that timestamp, recent IDs and an optional sequence. These
force a full inventory instead of advancing the cursor: endpoint changed, missing or malformed
cursor, sequence or pagination gap, cursor too old, clock skew, malformed or unavailable activity,
permission gap, unknown page IDs, periodic audit due, or `pull --inventory`. Inventory classifies
owned pages and rejects anything outside the boundary, and commits the cursor only after
classification and checkpoint succeed.

## 4. Known limitations — read before judging a failing row

1. Text repeated many times may have no unique anchor; that change goes to the merge call or the
   fallback note. (Very likely on this corpus — see section 7.)
2. A paragraph is one line, so both sides touching one paragraph costs a merge call and a verify
   call (about 100 s each on a local model), more on retry.
3. If the source rewrites a whole page, human changes on it go to the Appendix or get notes; a
   human may need to tidy up.
4. Guidance is keyed by the planned page file name. A page whose name changes loses its guidance;
   its human text is still kept.
5. If merges keep failing while the source keeps changing the same fact, fallback notes can stack.
   One human edit removes them.
6. Numbers are checked mechanically, but a human fact made only of words is protected only by the
   verify call, which is a model. A wrong `true` could accept a merge that drops it.
7. If a human moves a section and the source changes it in the same period, the source version
   appears as a note at the old place and the moved copy keeps the older text.
8. A human deletion that cannot be located (text not unique and no consistent window, e.g.
   surroundings fully rewritten) is not re-applied, so the deleted text can reappear. No
   human-written text is lost.
9. Removing a link the linker added is not kept; the linker adds it again.
10. GROWI Markdown serialization, permalinks, images and attachments must be checked against the
    actual target version.
11. Activity APIs may be global or permission-restricted — record constrained, prove inventory.

## 5. Code map — what to read first

| Read | Why |
|---|---|
| `publisher/human_changes.py:443` `rebase()` | the whole decision procedure, section 3.3 |
| `publisher/human_changes.py:164` `merge()` | the transport 3-way merge |
| `publisher/human_changes.py:665` `HumanStore` | state layout, identity, capture, generate, blocks |
| `publisher/human_changes.py:1007` `record_observation` / `1055` `record_event` | observation and evidence records |
| `publisher/human_changes.py:1104-1196` | prepared/confirmed publication evidence and lost-response logic |
| `publisher/human_changes.py:1237` `apply_generated()` | the generate hook |
| `publisher/live_verification.py:21` | the 62 case IDs and the report class |
| `graph/workspace/writer.py` | `publish_output()` and guidance injection |
| `runner/cli.py` | the verbs: `pull`, `sync`, `watch`, `human status`, `human live-plan` |

Exact strings you will grep for (typos here are real bugs):

- `（元文書の更新: ` — the inline note; `> **元文書の更新:**` — the long-form note.
- `元の文書では、この部分は削除されました（元ページ: ` — the Appendix entry note; `# 付録` — its title.
- Blocks: `legacy human journal`, `a different source at this path holds human information`,
  `document has human information; remove or move that text in GROWI first`,
  `damaged managed link footer`, `invalid human page marker`, `invalid human page baseline`,
  `observation identity collision`, `ambiguous prepared publication outcome`,
  `human store schema is missing`, `unsupported human store schema`, `invalid human snapshot hash`,
  `edits overlap`.

---

# PART II — THE ENVIRONMENT AND THE CORPUS

## 6. Project, config, and how to verify a clean baseline

Project `test`:

```ini
[project]
source_mount = /home/seigyo/llm-wiki/data/test/mount
target_name  = test
data_root    = data
growi_url    = http://10.160.152.38:3000/
growi_token  = <LIVE TOKEN — move this to .env as GROWI_TOKEN>

[settings]
concurrency = 2
```

Notes: `human_sync_mode` is **not** set, so it defaults to `off`. `wiki_linker_enabled` is not set,
so the normal linker runs — if you want the fast-path shape (`sync --fast`) to match production,
leave as is; if production runs without the linker, set it to `false`. `structure_target_lines`
defaults to 250 lines per page, which is what produced Doc A's 28 pages. Do not add the token to
any report.

Layout of the project data root (`data/test/`, itself a Git repo):

```
mount/            the source folder you swap files into
sources/<uuid>.docx   the ingested copy; the uuid is the source identity seed
raw/<name>_docx.md    the parser's Markdown conversion — the text the pipeline really reads
wiki/<document>/      generator output + _planning/ (deleted and rebuilt on every export)
metadata/
  pipeline.json       published_documents / published_pages: page_id, revision_id,
                      marker_id, marker_seed, remote_snapshot_blob per page
  human-sync/         the human state (section 3.1)
  state/              writer state; wiped by a full rebuild
  cache/{parse,planner,fast-inline-links}/
  source-identities.json, fast-links.json, wiki-linker.sqlite, watch-queue.sqlite
```

Verify a clean baseline before every case set, and record the answers:

```bash
cd /home/seigyo/llm-wiki
.venv/bin/python main.py --project test check          # chat 200, growi 200, parser 200
.venv/bin/python main.py --project test human status   # no blocked pages, zero counts
git -C data/test rev-parse HEAD
git -C data/test rev-parse refs/llm-wiki/last-good     # must be equal
git -C data/test status --porcelain -- .gitignore sources raw wiki metadata/state \
  metadata/pipeline.json metadata/source-identities.json metadata/human-sync    # must be empty
ls data/test/metadata/human-sync/                       # doc/ absent == clean human baseline
ls data/test/metadata/human-sync/documents 2>/dev/null  # must NOT exist (legacy journal)
```

Logs and observability:

- `logs/test/debug.log` — the live log for this project. Events look like
  `[writer] page_start`, `[writer] page_judged {"page","attempt","score","approved"}`,
  `[plan] compile_retry`, `[observe] done`, `[seed] done`, `[wiki] waiting`.
- **The terminal progress bar is blind on this path** (`"current": 0, "total": 0`), so `0件` and
  `0/1` do **not** mean stalled. Count `page_judged` lines instead:
  `grep -c page_judged logs/test/debug.log`.
- `logs/test/live.log` and `logs/test/failed_docs.md` (empty == no failed documents).
- Verb reference: `pull` (capture GROWI edits only; add `--inventory` for a full scan — needs owner
  approval on this target), `sync` (pull first, then scan/build/publish), `sync --fast`,
  `--fast --link`, `--fast --repair`, `watch --growi-interval 60`, `human status`,
  `human live-plan --path /test/<subtree>`, `check`.

## 7. Doc A — `(社外秘)制御機能仕様書【3章】.docx` (the primary test document)

Already published; this is the document all four cases run on.

**Inside the Word file:** 686 paragraphs, 29 tables, 133 headings nested **eight levels deep**
(`系統図 制御` → `制御（ＴＣ機器制御）` → `ＴＣ機器の個別操作` → `制御対象機器・項目` → `個別操作の機器一覧（６ｋＶ）` → …),
45 drawings, embedded Visio plus OLE objects stored as EMF/WMF, **39 review comments**, 2 footnotes,
2 endnotes, 29 bookmarks, **no tracked revisions** (0 `w:ins`, 0 `w:del`), 446 lines containing
full-width alphanumerics, one repeated long line.

**As published:** 28 pages, 3,785 lines, ledger written 12:35, 0 blocked pages, no human state yet.

**The shape that matters.** The converter keeps Word tables as **HTML** `<table>` blocks rather
than markdown pipes, so every cell is its own line. Measured: 2,781 of 3,785 lines (**73%**) sit
inside `<table>` blocks, and 18 of 28 pages are over 60% table lines. Page 002 is 210/220 (95%),
page 006 is 229/237 (96%). Only 003, 007, 016, 023, 027 and 028 are prose. A skeleton of a page
looks like:

```
# <page title>
<intro sentence>
<table><thead><tr><th>項目</th>…      ← one line per tag/cell
<tbody><tr><td rowspan="5">…</td>…
</table>
<legend line>
---
親: [..](001-….md) ｜ 前のページ: [..](001-….md)     ← navigation footer
```

Every page also carries inline links and a footer, which matters twice: pull strips linker-added
links before diffing (3.4 step 2), and human text that *looks* like a footer or inline code must
survive untouched.

**Full page inventory** (published file → converted-source line range → shape → what it is good
for). `coverage.json` in `_planning/` is the authority for the ranges.

| Page | Src lines | Shape | Best use |
|---|---|---|---|
| 001 ＴＣ機器の個別操作 〜 操作条件 | 1-248 | 3 tables / 51 cells, 3 h2, 7 bullets | intro/prose patch, parent-page behavior |
| 002 機器制御可・不可条件 | 249-306 | 1 table / 122 cells, 95% table, ○/△/× judgments | **table-cell conflict, same-fact-different-value, disjoint rows** |
| 003 制御項目詳細 | 307-424 | prose, 5 h2, 4 bullets | **reword-without-fact-change, human caveat add** |
| 004 遠制機器の個別操作(1-2) | 425-562 | 1 table / 34, 2 h2 | second-page edits |
| 005 遠制機器の個別操作(2-2) | 563-633 | 1 table / 8, 2 h2, 6 bullets | short-page behavior |
| 006 遠制開閉器の制御可・不可条件(1-2) | 634-882 | 1 table / 144 cells, 96% table | **second big-table conflict** |
| 007 遠制開閉器の…(2-2) | 883-903 | prose, 3 h2, markdown pipe rows | mixed table syntax check |
| 008-009 子局状態…(1-9)(1-2)/(2-2) | 904-1198 | 1 table / 56 each | sibling-pair confusion |
| 010 子局状態…(2-9) | 1199-1341 | 1 table / 56, 2 h2, 9 bullets | bullets + table on one page |
| 011-012 子局状態…(3-9)(1-2)/(2-2) | 1342-1644 | 2 tables / 104, then 1 / 48 | multi-table page |
| 013 子局状態…(4-9) | 1645-1907 | 1 table / 176 | **anchor-uniqueness** |
| 014 子局状態…(5-9) | 1908-2176 | 1 table / 176, 3 h2, 7 bullets | **Case 3 wrong-sibling test (edit here)** |
| 015 子局状態…(6-9) | 2177-2447 | 1 table / 176, 2 h2, 9 bullets | **Case 3 source change here** |
| 016 子局状態…(7-9) | 2448-2460 | 20 lines, prose, 2 h2, 2 bullets | tiny page / cheap page-level delete |
| 017 子局状態…(8-9) | 2461-2722 | 1 table / 176, 2 h2, 3 bullets | anchor-uniqueness |
| 018 子局状態…(9-9) | 2723-2955 | 3 tables / 130 | multi-table sibling |
| 019 制御項目詳細 | 2956-3110 | prose, 5 h2, 4 bullets | **duplicate page title with 003** |
| 020 個別操作の対象機器の一覧 〜 機器一覧(6kV) | 3111-3210 | list table / 32 | **add-a-row both sides** |
| 021 機器一覧(22kV)(1-2) | 3211-3332 | list table / 48 | **split into two chapters (number shift)** |
| 022 機器一覧(22kV)(2-2) | 3333-3468 | 2 tables / 47 | continuation after split |
| 023 操作条件 | 3469-3476 | **stub: 8 source lines, 10 wiki lines**, links + footer | **whole-page delete → Appendix** |
| 024 開閉器メモ設定 〜 作業メモ登録(1-2) | 3477-3611 | 1 table / 45 | mid-size table page |
| 025 開閉器メモ設定 〜 作業メモ登録(2-2) | 3612-3711 | 1 table / 25, 2 h2, 5 bullets | small table + prose |
| 026 ＳＶＲ の作業メモ登録 | 3712-3799 | 1 table / 25 | alternative Appendix candidate |
| 027 作業区間設定(1-2) | 3800-3952 | **prose, 7 h2, 8 bullets** | **delete one sub-section → Appendix; rename; reorder** |
| 028 作業区間設定(2-2) | 3953-4048 | **prose, 5 h2, 22 bullets** | **bullet delete, mid-page section add, reorder** |

**Corpus hazards you must account for when interpreting results:**

1. **Anchor uniqueness will often fail.** Nine sibling pages share the heading
   `子局状態とダイアログのボタン点灯・操作受付可否`, pages 013/014/015/017 each hold a 176-cell
   table of the same shape, and two pages are titled `制御項目詳細`. `rebase` needs a change
   *unique in `old` and unique in `new`* for a mechanical relocation, so expect a heavy tilt toward
   window → merge call → possibly notes. That is not a defect; it is the realistic behavior this
   corpus exists to expose, and the failure that matters is a change landing **on the wrong sibling**
   or **on the wrong `制御項目詳細` page**.
2. **The GROWI table editor rewrites the whole block.** One `○`→`△` fix can look like "this entire
   176-cell table changed", which overlaps any source edit in the same table and forces the model
   path. This is the most common real-world shape here, so it is Case 1 Round 1.
3. **Full-width text.** 446 lines carry full-width digits and letters (`６ｋＶ`, `２２ｋＶ`). A human
   typing `6kV` half-width in the browser will not match the source exactly, which also pushes the
   change onto the model path and can produce a note the source "should not" have needed.
4. **Possible reviewer-comment leakage.** 3 of the 38 searchable comment bodies match the converted
   markdown at raw lines **331, 3083, 3953** (comment ids 20, 19, 64; authors 北誥 and 末弘). Most
   likely the comment quotes the sentence it is about. **A human must confirm those three lines are
   not reviewer chatter before publishing to any shared instance.** The Word file contains real
   reviewer names.
5. **EMF/WMF and OLE are not handled anywhere in this repository** (`rg emf|wmf` finds nothing);
   conversion is entirely the parser service's responsibility. Missing or broken diagrams can look
   like "the source deleted content" and wrongly trigger the Appendix path. Check the converted
   markdown for images before judging any Appendix result.
6. **Confidential titles.** Filenames carry 社外秘 and full-width punctuation (`【3章】`,
   `(手順チェック)`). Titles become GROWI paths, so this is both a confidentiality concern and a
   free stress test for path handling, marker seeds and the managed footer.

## 8. Doc B — `(社外秘)融通計算機能仕様書_4_編集確認(手順チェック).docx`

2.3 MB, 906 blocks, 51 tables, only 13 headings and **flat** (1 root `編集確認(手順チェック)` plus 12
siblings: エラー項目, ワーニング項目, メッセージ項目, ポップアップ, 手順実行時1手順毎チェック, 停電操作手順に対する
論理チェック, 高圧お客さま…, 異系統切替手順, 自回線ループ内SVR判定, DM時限重複チェック…), 42 PNG/GIF screenshots,
**128 review comments**, no tracked changes, 875 full-width lines, 2,096 digit-bearing lines, and a
single longest source line of 1,684 characters. Converted to 5,477 source lines; planned as 28 pages.

Because its headings are flat, it **cannot** test "my text followed its section". Use it for
exactly three things (Case 5): the linker overlay check, delete/rename/replace safety with a
bystander document, and the "everything is one enormous table, so everything goes through the merge
call" stress shape — plus image/attachment rewriting not creating human changes.

## 9. Gaps in the corpus, and how to close them in v2

- **No repeated sentences.** Doc A has one repeated long line, Doc B none. The "did my edit attach
  to the wrong copy" check is therefore weak. Fix: when you create v2, deliberately duplicate one
  sentence into a second section of Doc A.
- **No fenced code blocks or inline code in the source** (it is Word prose). That is fine: case 27
  (markdown integrity) is about a **human pasting** code fences, a markdown table, inline code and
  footer-like text into GROWI, and checking it comes back byte for byte. Do that in the UI, not the
  mount.
- **Doc A is only ~4,048 converted lines** and produced exactly 28 pages; if you want more pages per
  document for structure tests, do not lower `structure_target_lines` mid-test — page numbering and
  every Appendix name depend on it staying constant for the whole run.

## 10. Tools (`tools/hc_live/`, on `feature/growi-search-app` only)

All of them read `HC_PROJECT=<config name>` and the token from the environment. Restore them with
`git checkout feature/growi-search-app -- tools/hc_live`.

| Tool | Usage |
|---|---|
| `round.sh` | `round.sh off\|observe\|apply LABEL [extra sync args]` — rewrites `human_sync_mode` in `configs/$HC_PROJECT.ini`, runs `sync --fast` (set `HC_SYNC="sync"` for the standard pipeline), logs to `$HC_LOGS/LABEL.log` (default `/tmp/hc-e2e-logs`), then prints `rc`, elapsed, model-call count, result/ERROR lines, `HEAD==last-good`, `durable-dirty`, and `human status`. |
| `count.py` | `count.py WORD [WORD…] [--page LOCAL_PATH]…` — prints `growi N, current N, live N`. "Once" == 1/1/1. Without `--page` it reads every page below `/<target_name>/`, including 目次 pages; **always pass `--page` on this target.** |
| `edit.py` | API editor (`origin: editor`): `pages`, `show <path>`, `replace <path> OLD NEW`, `insert-after <path> ANCHOR TEXT`, `delete <path> TEXT`, `save-unchanged <path>`, `history <path>`, `restore <path> <revision_id>`. `pages` columns: local path, page id, GROWI path. **Pre-check only — never counts as the human edit step.** |
| `human.py` | Same commands as `edit.py` but authenticates with `HUMAN_TOKEN`, i.e. the tester's credential. Still an API edit; the real-user evidence must come from the browser. |
| `proxy.py` | Fault injection for the out-of-scope race cases. Documented in `handoff-conflicts-tests.md` section 4. Never point it at anything but the test instance. |
| `canary.py`, `make_doc.py`, `source.py` | The synthetic HX-300 corpus and its scripted source versions. Not needed here — you have real documents — but useful if you want a fast, non-confidential rehearsal of the same four cases in 8 minutes instead of hours. |

Without the tools, use the raw verbs and edit in the browser: `pull`, `sync`, `human status`, plus
hand-editing `human_sync_mode` in the INI. Marker counting then becomes a `grep` over
`data/test/wiki/<document>/` and `data/test/metadata/human-sync/doc/*/current/`, and a manual look
at the page in GROWI.

## 11. Offline gate before you spend a single live minute

The live matrix is expensive and slow. Run the regression gate first; if it is red, live results
are meaningless.

```bash
cd /home/seigyo/llm-wiki
.venv/bin/python -m compileall -q common convert wiki linker index publisher runner graph
for m in test_human_changes test_human_sync_rollout test_sync_fallback test_update_tiers test_fast_repair; do
  timeout 300 .venv/bin/python -m unittest -q tests.$m
done
```

On 2026-10-12 (home PC) all five passed: 77, 24, 33, 34, 33 tests. Two cautions: `test_sync_fallback`
previously hung on this work PC in `test_isolated_sync_finishes_builder_pass_before_linker_phase`
(a real thread-import race in `publisher/ahead.py` was later fixed) — rerun it here and record the
result. `tests/test_mount_diff_pipeline.py` has 5 failures older than this feature (one is a missing
fixture, `configs/diff_test_local.ini`); they are not your problem, but note them so nobody blames
your changes.

The offline suite already covers, with fake pages and a fake model: both `rebase` identities with no
model call; rules 1-5 including rule 4 via the merge call and via the fallback; line-aligned
uniqueness for exact matching; a human change surviving four source updates and a restart;
reordering and cross-page moves; mid-page section adds and heading renames; deleted sections and
whole pages going to the Appendix exactly once; Appendix naming and renaming; both sides deleting
the same text; a human emptied page staying blank and unpublished; revert and catch-up; full
regeneration; pull onto an unpublished newer generation; candidate rollback; merge-output rejection
(bad `find`, dropped number, verify `false`) and the two-strikes fallback; guidance reaching all four
prompt builders and none on failure; linker links stripped and human links kept; `off`/`observe`
blocking with no model call; idle `sync` capturing a UI edit; legacy journal blocking; a different
source at the same path; delete-blocking with human information; and inline-link manifest rows being
dropped for rewritten pages.

**What that means for your live run:** the mechanics are already gated offline. Live testing is about
*real content, a real model, and a real editor* — anchor ambiguity on 176-cell tables, half-width vs
full-width typing, the browser's table editor rewriting blocks, GROWI's own Markdown serialization,
and whether a real human's intent survives. Do not re-derive mechanics live; do measure quality.

---

# PART III — THE TEST PLAN

## 12. Protocol for every round

A **round** = one UI change and/or one mount change, one sync, and its checks. Fixed sequence:

1. `round.sh <mode> <label>` (or edit the INI and run `sync`). Labels: `v2a1`, `v2a2`, `v3b1`…
2. `count.py <marker> --page <local path>` → expect `growi 1, current 1, live 1`.
3. `human status` → no new blocked pages unless the case expects one.
4. `round.sh <mode> <label>+stable` with **nothing changed** → must be a no-op: identical hashes
   and revisions, **zero model calls**. This is the idempotence gate and it runs after every
   meaningful round.
5. Record the row (section 17).

Marker words: `QA-v2-01`, `QA-v2-02`, … unique per edit, never reused. A check is then one count,
not an eyeball. Keep a marker ledger in the report: marker, page, round, meaning.

Invariants checked on **every** round, no exceptions:

- every current human change appears exactly once, in the page or the Appendix;
- the five rules hold;
- nothing is written over a remote revision the pipeline did not inspect;
- a repeat run with no new change is a no-op with no model calls;
- `HEAD == refs/llm-wiki/last-good` and the durable paths are clean.

Sequencing rules that save you rework:

- The tester's UI edit comes **before** the sync that carries the matching source change, so the
  conflict is real rather than reconstructed.
- Do not change `structure_target_lines` mid-run; page numbers and Appendix names depend on it.
- Keep the filename identical for every version. A rename means "a different document" and is a
  separate test (Case 5).
- Do page-count-changing mount edits (splits, new chapters, page deletes) **after** the single-page
  rounds, because they shift every later page number.
- Save every version in `versions/` and keep v1 so you can roll the mount back at any time.

## 13. The four mount versions of Doc A

Create them in Word from v1, one change per numbered step, so a failed row can be traced to one
edit. Keep a plain-text change log for the tester: one line per change, naming the page she will
find it on. If she cannot find a change in GROWI within a minute, that is itself a finding.

### v1 — as published today
Baseline. Nothing to do.

### v2 — same facts, different values (content-level, no structure change)

| # | Edit | Where | Produces |
|---|---|---|---|
| 2.1 | Change one judgment `○` → `△` in the 個別操作 column | page 002 table, src 249-306 | rule-4 conflict against her edit |
| 2.2 | Reword one `備考` cell in the same table | page 002 | rule 5 (different fact, same table) |
| 2.3 | Change one value/number in the 遠制開閉器 judgment table | page 006, src 634-882 | second conflict on a second big table |
| 2.4 | Reword one paragraph with **no change of meaning** | page 003, src 307-424 | reword-without-fact-change: re-apply, no note |
| 2.5 | Duplicate one sentence into a second section | any two prose sections | anchor-ambiguity probe (closes gap 9.1) |
| 2.6 | Set one value to exactly what the tester typed earlier (do this after her edit) | page 002 | rule 3 + source catches up |

### v3 — the shape moves (structural)

| # | Edit | Where | Produces |
|---|---|---|---|
| 3.1 | Delete one sub-section (its `##` heading and body) | page 027, src 3800-3952 | Appendix entry, structure otherwise intact |
| 3.2 | Rename one `##` heading and move that sub-section above its predecessor | page 028, src 3953-4048 | rename + reorder; text must follow |
| 3.3 | Split the 22kV list into two chapters | page 021, src 3211-3332 | page split; numbering shifts; Appendix must rename |
| 3.4 | Add one row to the 6kV equipment list | page 020, src 3111-3210 | add-on-both-sides in the same table |
| 3.5 | Change one line of the 子局状態 table on page 015 | page 015, src 2177-2447 | the wrong-sibling probe against her page-014 edit |
| 3.6 | Add one short new chapter near the end | end of document | page count grows; Appendix rename; numbering shift |

### v4 — fix and finish

| # | Edit | Where | Produces |
|---|---|---|---|
| 4.1 | Restore the sub-section from 3.1, unchanged | page 027 | restore; Appendix entry must persist |
| 4.2 | Change one more value in a row that already carries a note | page 002 | new note replaces the old one, not stacked |
| 4.3 | Delete the entire 操作条件 content | page 023, src 3469-3476 | whole-page deletion with human text on it → Appendix, and the page must not be trashed with her text |

## 14. The four major live cases

### Case 1 — "My change beats yours" (mode gating + rules 1-4 + idempotence)

Run with v2 staged but not yet copied in, then copy it in at Round 4.

| # | Tester (browser, tester account) | Then | Expected |
|---|---|---|---|
| 1.1 | On page 002, change a judgment to something **different** from what v2 will say, add `QA-v2-01`; also add a short 備考 sentence | `round.sh off a1` | The document **blocks**. Nothing captured, no state directory created, **zero model calls**. Page looks untouched to her. |
| 1.2 | Same revision, same edit | `round.sh observe a2` | Still blocks, nothing captured, still zero model calls. |
| 1.3 | Nothing | `round.sh apply a3` | Captured **exactly once** (`QA-v2-01` = 1/1/1). `doc/<key>/` now exists with `pure/`, `current/`, `doc.json`, one capture record. `human status` shows human_information true. |
| 1.4 | Nothing (copy v2 in first) | `round.sh apply a4` | Her judgment stays; the source's value appears adjacent as `（元文書の更新: …）`. Her 備考 sentence intact. One model merge call, one verify call (plus retry if rejected). |
| 1.5 | Before 1.4, add an experience note `QA-v2-05` to a page-003 paragraph that v2 only rewords | same sync | Her note survives; **no note** for the reword (formatting/wording differences get no note). |
| 1.6 | Make a small edit whose exact text you then put into v2 (step 2.6) | `round.sh apply a6` | Appears once, **no note**. A later source change to that fact applies normally. |
| 1.7 | Open any page and save without changing anything (`save-unchanged` semantics) | `round.sh apply a7` | Nothing captured, revision recorded. A transport-only or link-only revision is not a human change, in any mode. |
| 1.8 | Nothing | `round.sh apply a8` | No-op: stable hashes/revisions, zero model calls. |

Failure signatures: a note appearing for 1.5 means the merge treated wording as a fact change; a
capture in `off`/`observe` is a **hard safety failure**; a model call in 1.1-1.3 means mode gating is
broken; `QA-v2-01` appearing twice means the capture or the merge duplicated content — stop.

Cases proven: 1, 2, 3, 5, 6, 8, 10, 49, 50, 53, 58, 60, and the no-op invariant.

### Case 2 — "We changed different things" (coexistence, deletions, fidelity)

| # | Tester | Then | Expected |
|---|---|---|---|
| 2.1 | Change a **different row** of the page-002 table (`QA-v2-11`) while the 1.4 note exists | `round.sh apply b1` | Both her value and the source's new row value present; the existing note untouched. |
| 2.2 | Add a paragraph at the end of a section (`QA-v2-12`) at the same place v2 adds a different sentence | with v2 staged | Both present, each once. If she and the source add **identical** text: once, no note. |
| 2.3 | Add text then delete it in the same session (leaves a blank line) | `round.sh apply b2` | No phantom human change, no note, ideally no model call (window case 5). |
| 2.4 | Delete a bullet in page 028 (`QA-v2-14`) | mount rewords that bullet in v2 | Her deletion stays; the source's new wording appears as a note. |
| 2.5 | Delete a line that the source also deletes in v2 | `round.sh apply b3` | Absent everywhere, including the Appendix. No duplicate note. |
| 2.6 | Paste human text containing a fenced code block, a markdown table, inline code, comment-ish syntax and footer-like text (`QA-v2-16`) | sync, then pull, then re-publish | Returns **byte for byte**. No part of it is interpreted as links, guidance or footer. |

Failure signatures: her paragraph absorbed into the source paragraph; a deleted bullet resurrected
without a note; a pasted code fence re-formatted (means the human text went through a merge or a
linker rewrite).

Cases proven: 5, 7, 9, 27, 42, 43, 45.

### Case 3 — "The document moved, my text followed" (the hard one)

All her edits happen on v2-published state; then v3 goes in.

| # | Tester (before v3) | Then | Expected |
|---|---|---|---|
| 3.1 | Mark one line inside the sub-section 3.1 will delete (`QA-v3-01`) | sync v3 | A new last page `029-付録.md` containing her line **once**, a `## <nearest heading>` entry and the note `元の文書では、この部分は削除されました（元ページ: …）`. The sub-section is gone from page 027; no other page changed. |
| 3.2 | Edit that Appendix entry in GROWI; later delete a different entry | sync each time | Her edit kept; a deleted entry never returns. The Appendix is an ordinary page for pull. |
| 3.3 | Edit a line under the heading 3.2 renames (`QA-v3-03`) | same sync | Her text stays with its content, under the **new** heading, once, no note. |
| 3.4 | Add a line in the middle of the sub-section 3.2 moves | same sync | Her line travels with the moved sub-section, not with the surrounding old text. |
| 3.5 | Add a row to the 6kV list (`QA-v3-05`) | v3 also adds a row (3.4) | Both rows present once each; the table is still valid HTML and renders in GROWI. |
| 3.6 | **Wrong-sibling probe:** edit one line inside page **014** only (`QA-v3-06`) | v3 changes a matching line on page **015** (3.5) | Her edit stays on 014 and appears **nowhere** in 013/015/017. Wrong-sibling placement is a failure even if nothing is lost. |
| 3.7 | Nothing (v3 splits 021 and adds a chapter) | `round.sh apply c2` | Page numbers shift; her edits followed their text; `029-付録.md` renamed itself to stay last, content intact, and the old page was trashed with its expected revision checked. |
| 3.8 | With **no** source change: add a section mid-page, rename a heading, reorder two sections (`QA-v3-08`) | `round.sh apply c3` | Everything stays exactly where she put it; her new section is still there after a later small source patch to that page. |
| 3.9 | Optional, needs owner approval: force a full rebuild (`tier_3`) | `round.sh apply c4` | Every human change present exactly once after `publish_output()` rebuilds the folder. |

Failure signatures: an Appendix entry with the **wrong** heading (means the heading search failed);
two Appendix entries for one change (a re-run appended twice); her text on the wrong sibling;
the Appendix left behind after a rename (means `doc.json` and the live folder disagree).

Cases proven: 15, 16, 17, 18, 19, 44, 54, 59, 61 (partly).

### Case 4 — "I already decided, don't ask me again" (lifecycle and non-edits)

Runs on v4 plus deliberate non-sync actions.

| # | Tester | Then | Expected |
|---|---|---|---|
| 4.1 | Resolve four notes four ways: delete the note (keep hers); replace her value with the source's and delete the note; write a combination; edit around it | sync, sync | Each resolution captured once and stable. A later **new** source value brings a **new** note; the old value never returns. Her combination becomes the human value. |
| 4.2 | Delete a paragraph she had added earlier | sync | It does not come back on any later sync (suppress). |
| 4.3 | Undo one of her own earlier edits by hand | sync, then change that fact in the mount | The page follows the source from then on, including later updates. |
| 4.4 | Restore an older body from GROWI page history (`history` + `restore`) | sync | That revert **is** a human change under rule 1 and is kept. |
| 4.5 | While a note is showing, edit a different line in the same section (`QA-v4-05`) | sync | The note survives the unrelated edit. |
| 4.6 | Format-only fix: heading level, formatting, remove a duplicated sentence (`QA-v4-06`) | sync; then a tier-1 patch; then (approved) a tier-3 rewrite | Kept in the page with **no note**; `doc.json` gains a guidance entry that reaches the writer prompt; the fix present once after each tier. Repeat once with the classifier forced to fail: the fix must still be kept. |
| 4.7 | v4 restores the sub-section from 3.1 | `round.sh apply d1` | The section reads normally, and the Appendix entry **stays** until a human removes it. |
| 4.8 | Have an un-pulled edit on page 023, then v4 deletes that content (4.3) | `round.sh apply d2` | Her text goes to the Appendix. The page is not trashed with her text on it; the delete of the document/page is blocked or the text is preserved. |
| 4.9 | Three rapid saves in the editor where the middle one undoes the first | one `round.sh apply d3` | One capture, the latest value, no duplicates. |
| 4.10 | Nothing | `round.sh apply d4` | No-op, zero model calls. |

Failure signatures: a resolved note resurrecting; guidance written but the writer prompt unchanged
(check prompt bytes are unchanged when guidance is empty, and contain the line when it is not); a
fallback-note stack (known limitation 5 — record it, one human edit clears it).

Cases proven: 11, 12, 13, 14, 44, 46, 48, 51, 52, 55, 57, 59, 60, 61.

## 15. Case 5 — the two things that need the second document

Only after Doc B is published and stable, and only on Doc B plus one Doc A page.

1. **The linker must never look like a human change.** Repeat Case 1 Round 1 (table-cell conflict),
   Case 1's caveat add, and Case 3 Round 3.8 (placement) under `sync --fast`, then `--fast --link`,
   then `--fast --repair`. Expect: no linker-inserted link text captured as a human change in either
   document; the inline-link manifest loses rows for pages pull or `apply_generated` rewrote; and
   removing a linker link is *not* kept as a human edit (limitation 9). Watch for
   `could not locate generated link`, which stops the whole run by design.
2. **Delete / rename / replace safety.** Rename Doc B's source file → her human changes travel with
   the document (`move_sources` rewrites `raw_rel`). Put a *different* file at the old path → it
   inherits nothing, and blocks if the old key still holds human information. Then remove two source
   files at once → only inspected, owned pages with expected revisions are trashed; a document with
   human information blocks the delete until that text is removed or moved in GROWI; a document with
   only human **deletions** does not block. Doc A's pages must be untouched throughout.
3. Also on Doc B: one table-cell conflict round (its 51 flat tables make everything a merge call),
   and image/attachment rewriting must not create human changes — record transport differences by
   hash, not by reading bodies.

With one document, "everything vanished" and "nothing leaked" look identical. That is why 25, 26 and
56 need the bystander.

## 16. Optional one-minute ownership checks (once per document)

- Move an owned page in GROWI → blocked, not recreated (case 20).
- Trash an owned page → blocked, deletion evidence kept (21).
- Damage or remove the ownership marker on one page → capture blocks and keeps the observed
  evidence (22).
- After any of the three, put it back and pull → the page unblocks by itself with no new revision.

## 17. Reporting

Per round record, and nothing else: marker word; page local path, GROWI path, page id, revision id;
reason code or result line; API call counts (GET/PUT/create/delete) if the round is a
write/delete/move case; model call count and whether a fallback note was used; initial and final
hashes of the source file, `pure/<page>`, `current/<page>` and the remote Markdown; the four
`growi/current/live` counts; `HEAD==last-good` and durable-dirty; expected vs actual in one line
each; pass / fail / constrained with a reason. **Never** page bodies, source text, tokens or
authenticated URLs.

Use the report class so the 62-case contract is enforced:

```python
from pathlib import Path
from publisher.live_verification import LiveVerificationReport

report = LiveVerificationReport.open(Path(REPORT_PATH))
report.record_case(CASE_NAME, passed=PASSED, initial_hashes=INITIAL_HASHES,
                   final_hashes=FINAL_HASHES, revisions=REVISIONS,
                   operation=OPERATION_SUMMARY, expected=EXPECTED_SUMMARY,
                   actual=ACTUAL_SUMMARY,
                   calls={"get": 0, "put": 0, "create": 0, "delete": 0},
                   reason_codes=REASON_CODES)
# report.record_constraint(name, reason_codes=[...]) when the environment cannot produce the case
```

`finalize()` compares against `LIVE_CASES`: a key missing from an older report counts as pending, so
a 41-case report never finalizes as complete. Do not rewrite old reports on open; record each case
separately even when one fixture covers several.

Before finalizing: every case passed, failed or constrained; a forced inventory and a final pull
done (owner approval required for a full inventory on this target); `human status` checked;
`HEAD == refs/llm-wiki/last-good` with clean durable paths; cleanup limited to the confirmed
disposable subtree; the data root and report kept until reviewed.

The acceptance bar: human information is never silently lost, source truth is never silently
replaced, remote races fail closed, and every recovery path leaves durable state auditable and
repeatable.

## 18. Triage guide

| Symptom | Likely cause | Where to look |
|---|---|---|
| `dirty last-good working tree` | an interrupted run | `git -C data/test status` on the durable paths and the last command; keep capture and prepared evidence; **never reset** |
| Blocked in `off`/`observe` | expected | flip only the disposable INI to the next approved mode and pull again |
| Human text missing | merge or capture bug | compare `current/`, `pure/`, the Appendix and `captures/` by hash; project Git history has every earlier version |
| Unexpected note appears | merge rejected (`find` missing or not unique, number check, verify `false`) or input over 30,000 chars | run log reason codes in `logs/test/debug.log` |
| Linker text captured as human | link stripping failed | compare the page record's published local page with the current page (3.4 step 2) and the fast-inline manifest |
| Remote moved/deleted/marker damaged | a real human intervened on the remote | treat as an intervention, decide explicitly, then rerun inventory/pull |
| `ambiguous prepared publication outcome` | a write whose response was lost | preserve the page and attempt record; only exact prepared evidence may be adopted as bot output |
| Model calls when there should be none | mode gating or window classification | check `human_sync_mode` actually took effect in the INI, then the round log |
| Wrong sibling received the edit | anchor not unique | confirm uniqueness by grepping the anchor text across all pages of the document; this is limitation 1 |
| Notes stacking on one line | repeated merge failures against a volatile source fact | limitation 5; record, then have the human edit the line |

## 19. Coverage of all 62 live cases

Status key: **C1-C5** = the case that proves it; **opt** = optional check; **out** = not in this
plan. Never rename or remove an ID from `LIVE_CASES`.

- **A · baseline and idempotence.** 1 `initial_multi_page_publish` (already done; re-verify as step
  0). 2 `normal_update` C1. 3 `noop_repeat` C1. 4 `process_restart` opt (stop between commands,
  rerun once, nothing duplicated).
- **B · direct human intent.** 5 `human_add` C1/C2. 6 `human_replace` C1. 7 `human_delete` C2.
  8 `same_edit` C1.6. 9 `disjoint_edit` C2.1. 10 `contradiction` C1.4.
- **C · resolution in GROWI** (page edits, no CLI resolver). 11 `keep_human` C4.1.
  12 `accept_source` C4.1. 13 `combine` C4.1. 14 `suppress` C4.2.
- **D · generator tiers and structure.** 15 `tier_0` C1.8. 16 `tier_1` C1.4. 17 `tier_2` C3.3.
  18 `tier_3` C3.9 (needs approval). 19 `split_combine_rename_reorder` C3.3/3.7.
- **E · remote integrity and ownership.** 20 `remote_move` opt. 21 `remote_delete` opt.
  22 `marker_damage` opt. 23 `foreign_destination` out (needs planting an unmanaged page).
  24 `duplicate_marker` out. 25 `source_rename` C5. 26 `bulk_delete` C5. 27 `markdown_integrity`
  C2.6. 28 `attachments_images` C5.
- **F · revision and partial-publication races — all out**, they need the fault-injection proxy or a
  pause hook: 29 `conditional_409`, 30 `edit_after_preflight`, 31 `edit_during_generation`,
  32 `lost_update`, 33 `lost_create`, 34 `partial_publish_late_edit`.
- **G · activity, rollback and restart — all out by owner decision:** 35 `watcher_restart`,
  36 `activity_cursor_restart`, 37 `full_inventory_equivalence`, 38 `candidate_rollback`,
  39 `last_good_restore`, 40 `service_restart_reconcile`, 41 `model_outage_fallback`.
- **H · page-merge edge cases.** 42 `concurrent_add_same_anchor` C2.2. 43 `human_delete_source_modify`
  C2.4. 44 `human_modify_source_delete` C3.1 and C4.8 (section, page and whole-page variants).
  45 `concurrent_delete_same_fact` C2.5. 46 `multiple_remote_revisions_before_pull` C4.9.
  47 `mixed_page_capture_failure` out (needs a damaged page and a valid one in one pull).
  48 `source_move_delete_with_remote_edit` C4.8. 49 `transport_only_remote_revision` C1.7.
  50 `mode_transition_same_revision` C1.1-1.3 (add the `apply → off → apply` restart variant if
  time allows). 51 `growi_conflict_ui_resolution` C4.1. 52 `remote_revision_rollback` C4.4.
  53 `idle_sync_remote_reconciliation` C1 (edit, then plain `sync`: captured before any other work;
  in `off`/`observe` only that document blocks; the repeat run is a no-op with no model or parser
  calls). 54 `human_section_placement` C3.8. 55 `conflict_survives_unrelated_edit` C4.5.
  56 `fast_policy_human_overlay` C5.1. 57 `human_revert` C4.3. 58 `source_catches_up` C1.6.
  59 `source_removes_then_restores` C3.1 + C4.7. 60 `regeneration_without_fact_change` C1.5.
  61 `structure_only_edit` C4.6. 62 `pull_onto_unpublished_generation` out (needs a deliberately
  failing publication).

Totals: 40 covered by Cases 1-4, 45 including Case 5 and the optional checks, 17 out — 13 of them
(29-41) excluded on purpose, the other four requiring fault injection this pass does not use. A
constrained result is not a pass; record it as constrained with a reason code.

## 20. Observed state at the time of writing (2026-10-19)

A `sync --continue` for Doc B has been running since about 12:35. From `logs/test/debug.log`:

- 12:35 parse done — cached at 11:02, so Doc B's converted text is 5,477 lines and `パーサー: 0.0秒`
  is a cache hit, not a skip.
- 12:55 `observe done` — 28 windows, 663 observations, **9 images**, 0 mechanical fallbacks.
- 13:06 planner finished after 1,954 seconds → **28 pages planned**; one `compile_retry` at 13:04
  where page 22 (`状態・エラー情報テーブル`) owned only blank lines and was merged/re-planned.
- 13:08 `context done`, `seed done` (28 pages, 9 images; 3 filename repairs, 1 structural reject).
- 13:08 onward the writer: 12 pages judged at 13:47, 22 of 28 judged (21 approved) at 13:59, judge
  attempts `{1: 6, 2: 5, 3: 1}`, one page rejected at score 98 and re-passed 45 seconds later.
  Roughly 1.2 minutes per page, so about 6-8 minutes of generation left, then link and publish.
- Doc A untouched by that run (`0/1` documents means Doc B was the only one in scope): still 28
  pages, 0 blocked.
- Nothing for Doc B under `wiki/` or `metadata/state/` yet — pages land at the end. Absence there is
  normal until publication.

Before starting Case 1, confirm: Doc B published, `check` green, `human status` clean,
`HEAD == refs/llm-wiki/last-good`, durable paths clean, and `metadata/human-sync/doc/` still absent
(the clean baseline this whole plan assumes). If it is present, a capture already happened — find out
which round created it before you trust any count.

## 21. Open questions for the owner

1. Are raw markdown lines **331, 3083, 3953** of Doc A's converted source reviewer-comment text? Must
   be answered before any shared-instance publish.
2. Do you want the mount fed from converted `.md` instead of `.docx`? Far more precise v2/v3/v4 edits
   (source ranges map 1:1) at the cost of not exercising the Word parser.
3. Approved to duplicate one sentence into v2 (gap 9.1) and to force a tier-3 rebuild on Doc A
   (case 18)? Both rewrite real pages.
4. Should `wiki_linker_enabled` be pinned in the test INI to match production, or left at its
   default for this run?
5. Is `structure_target_lines = 250` the production value you want measured? Changing it changes
   page counts, Appendix names and every expectation in this document.
