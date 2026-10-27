# Aggressive human-sync test — Doc A (conf-test-1-agg.md)

growi_url = http://10.160.152.38:3000/
growi_token = (set `GROWI_TOKEN` in `.env`)


Same five behaviours as `conf-test-1.md`, pushed to the awkward end: more edits at once, uglier
content, hostile placements, and collisions at sites that already have a decision on them. The point
is to find where the merge stops being robust, not to break it on purpose. Full rewrites, wholesale
section deletion and page retitles are **out of scope** — they are known-broken, not robustness.

Project: `--project test_conf` → GROWI `/test_conf`, local state `data/test_conf`, source
`data/test_conf/mount/`. Only Doc A (`(社外秘)制御機能仕様書【3章】.docx`) is edited. Doc B pages must
stay byte-identical every round; if they move, stop.

## Limits (aggressive, but legal)

Allowed: many edits in one session, mid-table insertions, multi-block pastes, mixed full/half-width
text, repeated saves, same-fact collisions at 3+ sites, deletions of your own earlier text.
Not allowed, and the reason in each case:

- Never edit a page's first line (`# <title>`) or change any heading level — the title is dropped and
  nothing renames; the page is then unrecoverable without a rebuild.
- Never delete a whole `##`/`###` section that contains your own text — the Appendix path is known
  broken and the run will not fail loudly.
- Never add more than ~10 lines of new prose per site, never add a new table with more than 5 columns,
  never paste an image. A paste you would not type is not a robustness test.
- Never touch `data/test_conf/mount1/` (pristine baseline) and never create a `付録` page by hand.
- One sync at a time, ~1 hour. A silent log is not a dead run. Check the process and the candidate
  state before deciding anything.

## How to make the changes

**UI side** — browser editor on the GROWI test instance, tester account, never the bot account.
Page bodies are Markdown with raw HTML `<table>` blocks. To add a row, copy an existing `<tr>` and keep
the same number of `<td>`; to delete a line, remove the whole line, not just the text. Put a marker
inside every change (`（QA-r1a）`). Save once per intended change, except where a case asks for rapid
saves. Record page number + marker + change type as you go.

**Mount side** — never click around in Word. Use `python-docx` 1.2.0 with `.venv/bin/python`, locate
every target by **exact text match**, never by paragraph index, and never trust wiki line numbers as
anchors. Keep the baseline and build each round as a numbered copy, then install it under the same
filename:

```bash
 mkdir -p data_big/test_conf_backup && cp -r data/test_conf/mount data_big/test_conf_backup/mount-r2  # copy of what is live now
sha256sum data/test_conf/mount/*.docx | cut -c1-16               # hash before and after every edit
```

```python
# mount_edit.py — the four primitives you need
from copy import deepcopy
from docx import Document

P = 'data/test_conf/mount/(社外秘)制御機能仕様書【3章】.docx'
doc = Document(P)

def set_text(par, text):            # keep the paragraph's formatting, replace its text
    for run in par.runs[1:]:
        run.text = ''
    if par.runs:
        par.runs[0].text = text
    else:
        par.add_run(text)

for par in doc.paragraphs:                       # 1. change a value in prose
    if 'αは１０秒' in par.text:
        set_text(par, par.text.replace('αは１０秒', 'αは２０秒'))
    if 'あること。' in par.text and '選択応動' in par.text:   # 2. append a clause
        set_text(par, par.text + '（※２：区間解除中は除く。）')
    if '送電系統図画面にて当該区間を再度クリック' in par.text:  # 3. delete a bullet
        par._element.getparent().remove(par._element)

tbl = doc.tables[7]                              # 4. verdict cells (○ × △)
for row in tbl.rows:
    if row.cells[0].text.strip().startswith('ソ'):
        row.cells[3].text = '○'                  # adjust the column index after you print it

row = doc.tables[0].rows[2]                      # 5. insert a row mid-table, not at the end
fresh = deepcopy(row._tr)
row._tr.addnext(fresh)
doc.save(P)
```

Re-open the file with `python-docx` and print the edited sites before installing it. Then:

```bash
cp <your-edited>.docx 'data/test_conf/mount/(社外秘)制御機能仕様書【3章】.docx'
.venv/bin/python main.py --project test_conf sync      # --continue only to resume the same run
grep -rc 'QA-r2a' 'data/test_conf/wiki/(社外秘)制御機能仕様書【3章】.docx/'
```

**Anchors that exist in the current Doc A source** (verify against the live `.docx` before editing):

- 001 `ＴＣ機器の個別操作 〜 制御対象機器・項目` — device tables (`機器種別 / 操作画面 / 項目`), 6–12 rows.
- 002 `機器制御可・不可条件` — the big verdict table, 37 rows, `操作可否` + `備考` columns, `○ × △` marks;
  contains the `ソ（送電禁止）` row.
- 003 and 019 `制御項目詳細` — near-duplicate timeout bullets. 019 has full-width `αは１０秒`,
  `Ａ：１２５秒 / ６５秒 / ３５秒`, `試充電監視時間：１～３６００秒`; 003 has half-width `10 秒` / `30 秒`.
- 004 `個別操作` — the definition paragraph (single sentence today).
- 007 `遠制開閉器の制御可・不可条件（2/2）` — `## マークの凡例` with `### 開閉器操作に関するマーク` and bold
  entries such as `**黒×マーク（開閉器操作不可）**`, each followed by one sentence.
- 008–018 `子局状態とダイアログのボタン点灯・操作受付可否` — wide 6–12 column tables with merged headers;
  the most fragile pages in the document.
- 023 `操作条件` — heading + tables only.
- 028 `作業区間設定（2/2）` — bullet lists under `### 対象区間の選択`, `### 詳細情報の設定`.

---

## Round 1 — UI aggressive, mount untouched

**Tests:** capture under volume and hostile content; nothing gets lost, duplicated, or read as a fact
change when only one side moved.

- 10–12 edits in one session, at least one per page: 001, 002, 003, 004, 007, 019, 023, 028.
- One paste containing a fenced code block, a small Markdown table, inline code, a `//`-looking line, a
  `---` rule and a footer-like line (`（QA-r1a）`) on 004.
- Insert a row **in the middle** of the 37-row verdict table on 002, and one row into a wide state table
  on 014 (merged header, 10+ columns).
- Change a `ソ（送電禁止）` verdict to `△` **and** write in the adjacent `備考` cell (`（QA-r1b）`).
- Change `αは１０秒` → `αは１５秒` and `Ａ：１２５秒` → `Ａ：１３５秒` on 019.
- Delete one whole legend entry on 007: the bold mark line **and** its sentence.
- Delete three consecutive bullets under `### 対象区間の選択` on 028.
- Add a new `###` subsection in the middle of 028 and reorder two of your own lines.
- Deliberately ugly text: mixed full/half-width digits, double space at line end, a marker inside a
  table cell and another inside a list item.
- Controls: save 005 with no edit at all; on 006 add a line and delete it again in the same session.
- Then one sync, then a second sync with nothing further changed.

**Expect:** every marker at count 1 (2 when it sits in a table row), the pasted block byte-for-byte with
the fence intact and not re-linked, both mid-table rows kept with valid column counts, all deletions
still deleted, the net-zero session producing **no** capture, the no-edit save producing no capture, and
the repeat sync producing zero changes, zero notes, zero model calls. No `（元文書の更新: …）` anywhere.

---

## Round 2 — mount aggressive, UI untouched

**Tests:** source-only changes land silently, in place, including on the fragile wide tables.
Wait for Round 1 to publish and settle before starting.

- Flip verdict cells in five different rows of the 002 table, including `ソ（送電禁止）`.
- Change the constants on 019: `αは１０秒` → `αは２０秒`, `Ａ：１２５秒` → `１１５秒`,
  `１～３６００秒` → `１～７２００秒`.
- Append a clause with a reference id to the 004 definition paragraph.
- Add one row in the middle of a 001 device table, and two rows to the 002 verdict table (adjust the
  merged/spanning header accordingly).
- Delete four consecutive 028 bullets and reword one 007 legend sentence, same meaning.
- Move one paragraph to a different position inside its section, and swap two adjacent condition items.
- Add one new sub-item (keep its level; do not touch any page title line).
- Extend a footnote reference from `※１` to `※１，※２`.

**Expect:** every change visible exactly once in the right place; zero conflict notes; deleted bullets
gone with no Appendix; tables rendering with correct column counts; no page renamed, moved, added or
deleted; Doc B untouched. A ready-made source pair with six Doc A edits exists at
`data/test_conf/mount2/(社外秘)制御機能仕様書【3章】.docx` if you want a known-good mount-only round.

---

## Round 3 — both sides, same run (after Rounds 1 and 2 are published and settled)

**Tests:** the merge itself under load — collisions resolved in the human's favour, coexistence kept,
deletions respected, and old decisions not resurrected or stacked.

- Three simultaneous same-fact collisions: the `ソ（送電禁止）` cell, the `α` constant on 019, and one
  condition on 023 — each side picking a **different** value. Expect your value live plus exactly one
  `（元文書の更新: …）` per site.
- Same paragraph, both sides append a different sentence (004 definition paragraph). Expect both, once,
  no note.
- You delete the legend entry on 007 that the mount rewords in the same round. Expect the deletion to
  win, the new wording only inside a note, nothing resurrected as body text.
- You and the mount delete the same 028 bullet. Expect it gone everywhere, no note, no Appendix.
- Both sides add rows to the same table at the same time (001 and 002). Expect all rows, once, valid
  column counts.
- Catch-up: the mount adopts **exactly** one of your Round 1 values (`αは１５秒`). Expect the value once
  and no note at all; if a note existed from an earlier round, it must disappear.
- Neighbour-crossing probe: your edit on 019, the mount changing the near-identical bullet on 003. Your
  text must appear nowhere on 003, and vice versa. Wrong-sibling placement is a failure even with no loss.
- Rapid saves: three saves on 002 where the second undoes the first, while the mount changes a different
  fact on the same page. Expect one capture, the final value, no duplicates.
- Then, while the Round 3 notes are showing, edit an unrelated line on 002 and sync again. Each note
  must survive unchanged — never doubled, never moved into a neighbouring row.
- Finish with two no-change syncs.

**Expect:** 3 notes on the 3 collision sites and nothing elsewhere; all Round 1–2 markers still at their
expected counts; the catch-up site note-free; zero stacking across the two no-op syncs.

---

## Round 4 — lifecycle stress on the same decisions

**Tests:** decisions are durable; a human who has moved on is not overruled by an old source value.

- Resolve the Round 3 notes four different ways: delete the note and keep yours; replace your value with
  the source's and delete the note; write a combination of both; edit the line around the note. Then
  change those same facts in the mount again. Expect a **new** note with the new source value and the
  old value never returning.
- Suppress: delete one paragraph you added in Round 1, then sync twice, then make an unrelated mount edit
  to that page. It must not come back.
- Revert: restore an older body for one page from GROWI page history. That revert is a human change and
  must be captured and kept, including against the next mount change.
- Delete something **you** deleted in Round 1 by hand: your own deletion must stay deleted.
- Format-only fix (bold, half/full-width, remove a duplicated sentence). Expect no note. A `guidance`
  entry reaching the writer prompt is the expected behaviour and is **known not to work today** — record
  `guidance: {}` as a known fail, not as your error.
- Optional, if a page already carries a note: change the mount's value for that same fact twice in a row,
  one sync each. Expect exactly one note, always showing the newest source value.

---

## Report format

Per round, one line per edit: page, marker, marker count, note present y/n, pass/fail — plus per round
the total note count, the count of Doc A pages that changed, and a y/n for "Doc B untouched". No page
text, no source excerpts, no screenshots of content.

## Escalate, do not fix

- marker count 0, or higher than expected (lost or duplicated)
- your value replaced by the source value, with or without a note
- a note whose payload is empty, a `---`, or spans multiple blocks
- a note sitting between `<tr>` and `</tr>` (record the site; it is a known formatting defect)
- deleted text reappearing as body text; a `付録` page you did not cause
- relative links disappearing from a page you edited (known round-trip defect — record, keep going)
- any page renamed, moved, created or deleted that you did not ask for
- anything changed on a Doc B page, or two documents moving in one round when you edited one
