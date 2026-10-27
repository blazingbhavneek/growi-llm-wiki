# Human and mount conflict test: four-stage handoff

This is the standalone operating guide for the recurring human-edit sync check on the same two
documents. It records what each stage is meant to prove, what the GROWI tester changes, what the
mount `.docx` changes, and how to verify the result after each sync. It is written so a new agent can
resume from a stated stage without reading the earlier conversation logs.

**State recorded on 2026-10-20:** Stage 1 passed. Stages 2 and 3 were combined into one sync and
published; their human markers survived, but several expected behaviors remain unresolved. Stage 4
has not started.

## Quick handoff

When resuming, ask the operator which stage they want to continue only if they have not said. Do not
repeat a completed stage against the existing live state. Use this status:

| Stage | Meaning                                                     | Recorded state                                                                             |
| ----- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 1     | UI edits only; no mount change                              | Done. Capture and unchanged repeat passed.                                                 |
| 2     | Small mount and UI edits to Doc A                           | Run together with Stage 3; published. Partial pass.                                        |
| 3     | Structural/remapping mount edits to Doc B                   | Run together with Stage 2; published. Appendix and page-file follow behavior did not pass. |
| 4     | Resolve prior human decisions, then change the source again | Not started. This is the next planned stage.                                               |

For Stage 4, first read **Stage 2+3: what actually happened** and **Stage 4** below. Do not run
Stages 2 or 3 again just to recreate their starting state. Start Stage 4 from the Stage 2+3 mount
copies and the already-published human-sync state.

## Scope, aliases, and current files

- **Doc A:** `(社外秘)制御機能仕様書【3章】.docx`.
- **Doc B:** `(社外秘)融通計算機能仕様書_4_編集確認(手順チェック).docx`.
- **Runtime source mount:** `data/test/mount/` (the test project configured by `--project test`).
- **Original baseline backup:** `data_big/test_backup/mount/` and `mount1/`. They were byte-for-byte
  identical when checked for this guide (Doc A hash starts `76a3a667299bfa49`; Doc B starts
  `3d722e78a6823a2f`).
- **Stage 2+3 edited backup:** `data_big/test_backup/mount2/`. Both edited files there match the
  current `data/test/mount/` files by SHA-256 (Doc A starts `eaff8c820981083c`; Doc B starts
  `8e2c855d043594bb`). This is the right starting point for Stage 4. Re-check hashes before using
  these paths later; the recorded hashes are fingerprints, not a substitute for checking.
- **Stage 4 working copy:** make a new `data_big/test_backup/mount3/` from `mount2/`. Keep `mount`,
  `mount1`, and `mount2` as preserved references. Do not build Stage 4 by copying the original pair
  over the Stage 2+3 pair; that would erase the already-tested source edits.
- **Local generated pages:** `data/test/wiki/<document directory>/`.
- **Human-sync state:** `data/test/metadata/human-sync/doc/<document key>/`, especially `pure/`,
  `current/`, `captures/`, and `doc.json`.
- **Remote pages:** the test GROWI instance under `/test/`, checked by opening only the known pages
  in the browser. Confirm the endpoint and page ownership before any write.

Use the three-digit page number as the stable page reference in this guide. A page filename may not
change when its title heading changes; a heading change and a GROWI page-path rename are separate
results and must be checked separately.

## What the sync is comparing

- **Mount/source change:** a changed `.docx` is the source-side change.
- **UI/human change:** a tester edits and saves a GROWI page in the browser.
- **`pure/`:** the latest generated page before human edits are overlaid.
- **`current/`:** generated content plus accepted human changes; this is the durable local human
  version.
- **`wiki/`:** the local page tree that is published.
- **Conflict note:** when both sides changed the same fact differently, the human choice remains and
  the source value is shown in a `（元文書の更新: …）` note.
- **Appendix:** a generated final page for human text whose source section was deleted. It must not
  be typed in by hand to make a test appear to pass.
- **Guidance:** a short instruction learned from a structure/format-only human edit and saved in
  `doc.json`; it should reach the writer prompt for that page.

The intended behavior is that human text is captured once, survives source regeneration, and is not
duplicated. Source text and human text about different facts should coexist. If both sides changed
the same fact differently, the human value stays with a note showing the source value. If the source
later catches up to the human value, the note disappears. If the human later changes their decision,
the next sync should follow that decision rather than revive older text.

## Ground rules for every stage

1. `human_sync_mode = apply` is the intended permanent mode for this test. Mode gating is not one of
   the four stages.
2. For user-evidence runs, the separate tester account types and saves in the GROWI browser. Earlier
   UI changes were made through the API; those exercised the pipeline but do not prove the
   handoff's browser-typed requirement.
3. Make and save the tester's UI edits **before** swapping in the corresponding changed mount files.
   Then run one sync so the human and source changes meet in the same run.
4. Do not run two syncs at once. Before resuming a failed/incomplete run, inspect the candidate and
   last-good state. `--continue` is for resuming saved work, not a substitute for checking whether a
   previous run is still active.
5. Do not run a broad GROWI inventory for these checks. Open only the named owned pages. Never copy
   page bodies, source excerpts, tokens, or authenticated URLs into reports. Record page IDs,
   revision IDs, hashes, marker counts, and reason codes instead.
6. If a marker disappears or duplicates, a human edit is overwritten, an unexpected page moves or
   is deleted, or the run's state is unclear, stop and preserve the data root and logs. Do not
   hand-edit the output to conceal the result.
7. Before each mount edit, verify the target against the current `.docx`; parsed Markdown line
   numbers and old page headings are not safe Word-file anchors. Use copies and retain hashes of the
   two source files before and after editing.

## Stage 1 — UI-only capture and no-op

### Purpose

Prove that human-only edits are captured, that an unchanged GROWI save is not mistaken for a content
edit, and that an immediate repeat sync does not duplicate the human content. **Neither mount
document changes in this stage.**

### Tester actions

In Doc A, the prior run used these small edits:

| Page | Browser edit                                                                                               | Marker / handling                                                   |
| ---- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 002  | Change a decision cell in the `セ（設定）` row. This is separate from the reserved Stage 2 conflict row. | `QA-1a`                                                           |
| 019  | Change the selected value.                                                                                 | `QA-1b`                                                           |
| 004  | Add one short sentence to the relevant paragraph.                                                          | `QA-1c`                                                           |
| 007  | Delete one legend row.                                                                                     | A deletion has no text marker; verify absence in the rendered page. |
| 001  | Add one table row.                                                                                         | `QA-1e`                                                           |
| 028  | Paste a fenced code block and a small Markdown table.                                                      | `QA-1f`; inspect browser rendering as well as stored text.        |
| 023  | Open and save without changing the body.                                                                   | Control case; no human-content capture expected.                    |

The old run deliberately left the Stage 2 conflict cell untouched and did not edit pages 024–026.
For a fresh run, keep edits small, use a marker at the beginning of newly typed prose, and tell the
operator the page, marker, and edit type. Do not put markers inside a table value if that would make
the table invalid.

### Sequence and pass checks

1. With both mount files unchanged, run a baseline sync. It should be a no-op.
2. Save the browser edits above, then sync once.
3. Check each new human marker in the exact live page, `wiki/`, and `current/`. Check that the human
   markers are absent from `pure/`. The prior verification recorded `current/` counts of 1, 1, 1,
   2, and 2 for the five marker-bearing edits (`QA-1a`, `QA-1b`, `QA-1c`, `QA-1e`, `QA-1f`);
   preserve the intended marker placement when comparing, rather than assuming every marker must
   have a count of one.
4. Confirm the 007 deleted row did not return, the pasted content remains intact, no source-update
   note or Appendix was created, and the page saved unchanged at 023 was not captured as a content
   change.
5. Run the same sync again with no new edits. It must report no changed target pages and make zero
   model calls. The prior Stage 1 capture run had no human-merge model calls; it did have unrelated
   linker calls, so distinguish those when reporting.

### Recorded result

Stage 1 passed technically: six edits were captured, the unchanged save was recognized as no content
change, no human-merge note or Appendix was created, and the repeat run was a no-op. The browser
evidence caveat above still applies because the recorded edits were API-authored.

## Stage 2 — small, mostly content-level changes on Doc A

### Purpose

Exercise same-fact conflicts, source catch-up, independent edits on the same page, deletion behavior,
and table-row coexistence without a large structural rewrite. The mount edits are all in Doc A; Doc B
is not changed for Stage 2 alone.

### Mount edits for Doc A

Build from the Stage 1 source version. The Stage 2+3 edited copy is already available in
`data_big/test_backup/mount2/`.

| Page | Source-side change                                          | Expected behavior                                                       |
| ---- | ----------------------------------------------------------- | ----------------------------------------------------------------------- |
| 002  | Change the `ソ（送電禁止）` row's `個別操作` cell.      | Conflicts with the tester's different value.                            |
| 019  | Change the value to the one the tester supplied in Stage 1. | Source catches up: it appears once and no note is needed.               |
| 004  | Append a sentence to the `個別操作` definition paragraph. | Coexists with the tester's different sentence.                          |
| 007  | Reword the explanation the tester will delete.              | The deletion wins; source wording is retained as a note.                |
| 001  | Add a new row to the first table.                           | Coexists with a different tester-added row.                             |
| 028  | Delete the same bullet the tester deletes.                  | It stays deleted; no Appendix entry is expected for this same deletion. |

### Tester actions before the mount swap

| Page | Browser edit                                                                                    | Marker                          | Expected result                                                                                                                         |
| ---- | ----------------------------------------------------------------------------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| 002  | Change the same `ソ（送電禁止）` cell to a third value, distinct from the source's new value. | `QA-2a`                       | Her value stays; one note shows the source value. The tester should report the row but withhold the chosen value until after the check. |
| 004  | Add a different sentence in the same area.                                                      | `QA-2b`                       | Both sentences appear once; no conflict note.                                                                                           |
| 007  | Delete the explanation that the source rewords.                                                 | No marker for the deleted text. | The text stays deleted and the source wording appears once as a note.                                                                   |
| 001  | Add a different row to the first table.                                                         | `QA-2c`                       | Both new rows appear once and the table remains valid.                                                                                  |
| 028  | Delete the same bullet the mount deletes.                                                       | No marker for the deletion.     | The bullet is absent; no Appendix entry.                                                                                                |
| 019  | Make no new edit.                                                                               | —                              | The Stage 1 value is already the new source value.                                                                                      |

The Stage 1 pasted-code/table check is already separate; do not repaste it during this stage.

### Checks after the sync

- Verify `QA-2a`, `QA-2b`, and `QA-2c` on the live page, in `wiki/`, and in `current/`; none
  should be in `pure/`.
- On 002, compare the human value and source-update note with the mount-derived source. The human
  value must not be replaced, and the note must not be duplicated on a repeat sync.
- On 019, the source-caught-up value must occur once without a note.
- On 004 and 001, both sides' additions must be present once and structurally valid.
- On 007 and 028, verify the deliberate deletions; inspect 007 for the known Stage 1 legend-row
  resurrection risk.
- Run an unchanged repeat sync and confirm no new notes or human text appear.

## Stage 3 — structural/refactor changes on Doc B

### Purpose

Test whether human edits follow text when the source deletes a section, changes headings, moves
blocks, changes table structure, and reorders sections. Doc A is the bystander in a Stage 3-only
run; its captured Stage 2 state must remain intact.

### Mount edits for Doc B

The Stage 2+3 edited copy in `data_big/test_backup/mount2/` contains these changes:

| Page | Source-side change                                                         | Expected behavior                                                                                                         |
| ---- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 025  | Delete the `解列候補一覧` sub-section and rename the page-title heading. | Human text from the deleted section should move to an Appendix; text under the renamed heading should follow its content. |
| 027  | Rename a section heading.                                                  | The new heading appears. A page-path rename, if expected, must be checked separately from the body heading.               |
| 022  | Move the `※2` block within its page.                                    | Human text inside the block should travel with the block.                                                                 |
| 004  | Add two rows to the 43A table.                                             | Coexist with the tester's separate row; table remains valid.                                                              |
| 028  | Swap the two numbered blocks.                                              | Human text should follow its block.                                                                                       |
| 028  | Change the time value.                                                     | Conflicts with the tester's different value; human value plus one source note expected.                                   |

### Tester actions before the mount swap

| Page | Browser edit                                             | Marker                                                                                                             | Expected result                                                                             |
| ---- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| 025  | Mark one line inside the sub-section the source deletes. | `QA-3a`                                                                                                          | The human line should appear once on a new final Appendix page with a deletion explanation. |
| 025  | Edit a line under the page-title heading.                | `QA-3b`                                                                                                          | The text follows the renamed content and appears once.                                      |
| 022  | Mark one line inside the block the source moves.         | `QA-3c`                                                                                                          | The line stays with the moved block.                                                        |
| 004  | Add a separate row to the 43A table.                     | `QA-3d`                                                                                                          | The human row and both source rows appear once; table stays valid.                          |
| 028  | Change the same time value to a different value.         | `QA-3f`                                                                                                          | The human value remains with one note containing the source value.                          |
| 028  | Add text inside the numbered block that will move.       | `QA-3e` was planned but **could not be applied**: the expected labels were absent from the published page. | Do not count this case as tested or claim that block-follow behavior passed.                |

### Combined Stage 2+3 execution order actually used

These two conceptual stages were deliberately combined because they affect separate source
documents and the human state is stored per document. The actual order was:

1. Tester saved all feasible Doc A and Doc B UI edits first.
2. Both Stage 2+3 `.docx` files were installed in `data/test/mount/` under the same filenames.
3. One sync processed both documents.
4. The result was checked by document and marker, not as one aggregate count.

For any reproduction, follow the same ordering. If the tester saves after the mount sync, that is a
new human-only capture round and no longer tests the intended simultaneous conflict.

### Intended checks after the combined sync

- Doc A: verify the six cases in Stage 2, while confirming no unrelated Doc A pages changed.
- Doc B: verify each marker in its correct content block; inspect the table in GROWI; check the
  Appendix page and page paths independently from heading text.
- All eight applied human markers (`QA-2a/b/c`, `QA-3a/b/c/d/f`) should be present on their expected
  pages in `wiki/` and `current/`, with these exact local-page counts: `QA-2a` 1, `QA-2b` 1,
  `QA-2c` 2, `QA-3a` 1, `QA-3b` 1, `QA-3c` 1, `QA-3d` 2, and `QA-3f` 1. The table-row markers
  `QA-2c` and `QA-3d` are in two cells by design. All human markers are absent from `pure/`.
  `QA-3e` is excluded because it was not applied.
- Source-added markers `QA-M2a`, `QA-M2b`, `QA-M2c`, and `QA-M3a` should be checked in generated
  source/output too. `QA-M3a` was intentionally present twice because it labels two inserted rows.
- Confirm all expected conflict notes and source values against the mount, and repeat sync once to
  check that notes and text do not stack.

## Stage 2+3: what actually happened

The combined sync ran and published. The eight applied human markers survived in `wiki/` and
`current/` and were absent from `pure/`. Four conflict notes were observed on Doc A page 002, Doc A
page 007, Doc B page 025, and Doc B page 028. The tested run completed, but these findings remain
open and must not be described as a full pass:

1. **Appendix creation failed for `QA-3a`.** The source-deleted section had human text, but no
   `029-付録.md` was created and `doc.json` had no Appendix. The human text remained in the page
   body. The Stage 4 expectation that an Appendix entry persists after the section is restored is
   therefore **not testable from the current state**. Do not create an Appendix manually or count
   Appendix persistence as passed.
2. **Heading edits did not rename page files.** The Doc B heading changes appeared in body text,
   but `retitle_pages` was zero and the page filenames stayed the same. Text-under-renamed-heading
   behavior and actual page-path rename behavior are separate; the latter remains unproven.
3. **Two notes were malformed.** The Doc B page 025 note and Doc A page 007 note contained awkward
   fallback text/formatting. Record this if either note is resolved in Stage 4; do not normalize the
   earlier evidence away.
4. **Guidance was empty.** `doc.json` had `guidance: {}` after a reword/format-class case. Stage 4
   must include a new genuine format-only **UI** edit and verify both the stored guidance and the
   writer prompt. A mount-only reword cannot create human guidance.
5. **Stage 1 legend-row risk remains.** The deleted row had no direct counterpart in the mount
   source; the writer may recreate it. Check page 007 rather than assuming its deletion is safe.
6. **Timing/observability:** the combined run took about 66 minutes. Some logs stopped updating while
   work was still running, so check the process/candidate state before retrying. Never start a second
   sync because a progress log looks stale.

## Stage 4 — lifecycle: keep the decision, then change the source again

### Purpose

Prove that human decisions remain durable across later source changes: a human can remove or accept a
conflict note, change a prior choice, delete their own addition, or make a format-only correction;
later source edits should not revive stale human text or stack old conflict notes. This stage starts
from the published Stage 2+3 state and is **not yet done**.

### Create the Stage 4 mount files safely

1. Copy both Stage 2+3 files from `data_big/test_backup/mount2/` into a new
   `data_big/test_backup/mount3/`. Keep `mount2/` unchanged as the reproducible Stage 2+3 version.
2. Edit the Stage 4 copies with `python-docx` 1.2.0, preserving all prior Stage 2+3 edits.
3. In Doc A, make these changes:
   - Change the page 002 conflict cell to a **third source value**, different from both the previous
     mount value and the tester's current value. This must create a fresh conflict after the tester
     removes the old note.
   - Change the page 019 value again. Stage 2 made the source catch up to the Stage 1 human value;
     this later source change should now apply without reviving stale text or adding a conflict note.
   - Make one small wording-only source change on page 004 to cause the page to be written again and
     exercise the format guidance on that page.
4. In Doc B, restore only the deleted page 025 sub-section, unchanged from the original baseline in
   `mount1/` (or `mount/`). Preserve the other Stage 2+3 edits in Doc B. Do not replace the whole Doc
   B with the original file. The optional extension is to change the page 028 value once more to
   test another fresh conflict.
5. Check the `.docx` ZIP integrity and compare its package parts with `mount2/`. The previous editing
   approach expected only `word/document.xml` to change semantically and media to remain identical.
   Record hashes; do not include document text in the report.

### Tester actions before the Stage 4 mount swap

The four prior notes are on A002, A007, B025, and B028. Before editing, inspect the current live
pages and write down which action is assigned to each note. The minimum plan exercises these
different treatments:

1. **A002:** remove the old note while keeping the human value. This is the setup for the third
   source value; the next sync must produce one new note with the new source value, not stack the old
   note.
2. **A007:** accept the current source wording/value and remove the old note.
3. **B028:** rewrite the human choice as a deliberate combination and remove the old note.
4. **B025:** make an unrelated edit near the existing note and leave the note in place. This exercises
   “edit around a note”; because the prior note/Appendix result is already a finding, do not call this
   a clean resolution or Appendix pass.

Also do the following before the source swap:

- Delete the entire paragraph previously added with `QA-2b` on Doc A page 004. It must remain
  deleted after this and later syncs.
- Make one real **format-only UI edit** on Doc A page 004 (for example, a suitable heading-level or
  duplicate-format correction that changes no fact). Do not add a content marker to this edit; tell
  the operator its page and nature separately. This is required to test guidance: a source-side
  reword alone does not create a human guidance record.
- The B025 edit-around action above is the unrelated edit in a section with a note. Verify that the
  note remains after that save; do not make a duplicate edit just to repeat the same check.
- Perform three rapid saves on the same simple line that the Stage 4 mount does not change: first
  value, second save undoing the first, third save with the final value and marker `QA-4rapid`. Save
  each revision in the browser. The final value should be captured once; the first and middle values
  must not be resurrected. Report the page and marker, not private surrounding text.

The longer original Case 4 also mentioned manually undoing an earlier edit, restoring an older page
revision, and deleting an entire page's source content. Those are extensions, not part of the minimal
Stage 4 run above. If requested, test them as separately identified cases; do not silently fold them
into this run.

### Stage 4 ordering

1. Confirm the current Stage 2+3 sync is complete and inspect `human status`, `check`, and the
   last-good/candidate state.
2. Make and save all tester edits above in the browser.
3. Copy the Stage 4 `.docx` files into `data/test/mount/`, keeping the same filenames.
4. Run one sync using the test project's established command. The earlier combined run used
   `.venv/bin/python main.py --project test sync --continue`; use `--continue` only when this is a
   deliberate resume of saved work and the candidate matches this run.
5. Inspect both docs and human-sync metadata before any repeat. If clean, run one unchanged repeat
   sync to prove idempotence.

### Stage 4 acceptance checks

| Behavior                                      | Where/how to check                                                             | Pass condition                                                                                                                                                                                                                                                 |
| --------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Old note removed, new source conflict created | A002, `wiki/`, `current/`, browser, note count                             | Old source note is gone; human value remains; only one new note displays the Stage 4 source value. No old value or fallback note is stacked.                                                                                                                   |
| Source catch-up, then source changes again    | A019                                                                           | The newest mount value is present once, with no stale human value and no conflict note.                                                                                                                                                                        |
| Prior human paragraph deleted                 | Search for `QA-2b` in the exact current/live pages, not historical snapshots | Marker is absent from `wiki/`, `current/`, and the live page after Stage 4 and after the unchanged repeat. Old capture snapshots may still contain it.                                                                                                     |
| Format-only UI edit and guidance              | Doc A `doc.json`, page 004 writer input/prompt, output page                  | Format fix remains, no source conflict note is added for formatting, guidance is non-empty, and the actual writer prompt includes the guidance. A non-empty `doc.json` alone is not enough. If the writer was not invoked, prompt delivery remains unproven. |
| Source wording edit on page 004               | `pure/`, `current/`, `wiki/`, live page                                  | Source reword and human format fix coexist; the human fix appears once.                                                                                                                                                                                        |
| Note survives unrelated edit                  | The chosen page with a note, normally B025                                     | The unrelated human edit appears once and the existing note remains unchanged.                                                                                                                                                                                 |
| Rapid saves                                   | New capture record(s), exact page in `current/`, `wiki/`, and browser      | One accepted capture for the final revision, final value once, first/middle values absent from current and live content.                                                                                                                                       |
| Doc B section restore                         | Page 025 in `pure/`, `current/`, `wiki/`, and browser                    | Restored source section appears, and unrelated Stage 2+3 edits are still present.                                                                                                                                                                              |
| Appendix persistence                          | `doc.json`, current pages, `human status`                                  | **Not passable from the current baseline:** Stage 2+3 created no Appendix. First reproduce/fix and verify Appendix creation; until then record “not testable,” not “passed.”                                                                         |
| Optional second Doc B conflict                | Page 028                                                                       | If included, one new note for the new source value; no stale note stacking.                                                                                                                                                                                    |
| Unchanged repeat                              | Sync summary, model-call logs, hashes/revisions                                | Zero changed target pages, zero model calls, no new captures/notes/duplicates, and stable output.                                                                                                                                                              |

## Common post-sync verification procedure

Run these checks after every meaningful sync, scoped to the two documents and the pages touched.

### 1. Confirm the run completed cleanly

Use the normal test-project status commands:

```sh
.venv/bin/python main.py --project test check
.venv/bin/python main.py --project test human status
```

Confirm no unexpected blocked pages, failed docs, or unfinished candidate remain. Compare the data
repository's `HEAD` with `refs/llm-wiki/last-good`; investigate a mismatch before another sync.
`human status` gives per-document human-information, Appendix-entry, and guidance counts. It does
not by itself prove that every page is correct.

### 2. Check markers without dumping page bodies

For each marker, count occurrences separately in:

1. The exact live GROWI page in the browser.
2. The corresponding file under `data/test/wiki/<document directory>/`.
3. The corresponding file under `data/test/metadata/human-sync/doc/<key>/current/`.
4. The matching `pure/` page as a control.

Example for one exact local page file (replace the filename and marker; do not search the whole
document tree for a single-page count):

```sh
rg -o -F 'QA-2a' 'data/test/wiki/<Doc A directory>/<exact page>.md' | wc -l
```

Find the correct document key by reading only the `doc.json` metadata and matching its `raw_rel` to
Doc A or Doc B. Do not search all snapshots and treat old historical captures as live content:
`captures/` and `snapshots/` intentionally preserve prior revisions. For a newly accepted human
marker, use the per-page count listed for that stage in the live page, `wiki/`, and `current/`, and
expect zero times in `pure/`, except for a deliberately documented marker placement/count such as
Stage 1's table/code markers or the Stage 2+3 table-row markers.

For mount-origin markers, check `pure/` as well as the current and live output. `QA-M3a` is expected
twice because it labels two source-added rows. Deletions have no marker; verify them by checking the
specific page diff/rendered section and record only the result or a redacted hash in the report.

### 3. Check conflicts, removals, and movement

- Count `元文書の更新` notes in the two affected document directories and compare each to the
  stage's expected note locations. Read the local/live page to confirm the note contains the current
  source value; do not paste that value into the report.
- Check that a deleted human paragraph is absent from the current and live page, not just from
  `pure/`. Historical snapshots may retain it.
- For a moved block, verify the marker appears under the right heading/block, not merely somewhere
  in the same document.
- For an Appendix, check both the file under `wiki/` and `doc.json`'s `appendix` field; also check
  `human status`'s entry count. A page heading or a human marker remaining in the old page is not an
  Appendix pass.
- For a rename, check the body heading, local filename, published page path, and relevant link
  updates separately. A new heading alone does not prove a page rename.
- Inspect HTML/Markdown tables and pasted code in the GROWI browser, since a correct local text
  count does not prove the rendered structure is valid.

### 4. Prove the final no-op

After a stage's expected output has been inspected and the run is fully complete, make no further
edits and sync once more. Record the target-page count, captures, note counts, and model-call count.
The repeat must not duplicate markers, notes, rows, or Appendix entries. If the stage has not yet
passed its substantive checks, label the repeat as an idempotence observation, not an overall pass.

## Reporting format for the next agent

Keep reports concise and redacted. Use this template:

```text
Stage: <1 | 2 | 3 | 2+3 reproduction | 4>
Starting state: <completed sync/last-good commit; do not include tokens or page bodies>
Mount files: <baseline / Stage 2+3 / Stage 4; filenames and short hashes>
UI edits: <page + marker + edit type; omit private values>
Sync: <command, run identifier, completed/failed>
Checks: <live / wiki / current / pure marker counts; notes; Appendix; guidance; repeat no-op>
Findings: <pass / fail / not testable, with reason>
Next action: <the smallest next check; do not silently repair the result>
```

The Stage 2+3 run is a **partial pass**, not a clean pass. Stage 4 remains incomplete until its
independent lifecycle checks pass; Appendix persistence specifically remains untestable until
Appendix creation is first demonstrated from a valid Stage 2/3 deletion case.
