# Human-sync test guide — Doc A

## Setup (once)

- You get a config with **Doc A** already published to the GROWI test tree.
- Two things can change: the **UI** (you edit + save a page in the browser, tester account, never the bot account) and the **mount** (a new `.docx` goes into the mount folder under the same filename).
- For the both-sides cases: **UI edit first, save, then** swap in the new `.docx`, then run one sync. That makes your change and the source change meet in the same run.
- One sync at a time. Roughly an hour. A quiet log is not a dead run — never start a second one.
- Put a marker in everything you add, e.g. `（QA-a1）`, so we can count it later without reading the page.
- Count instead of reading: `grep -rc 'QA-a1' data/test/wiki/<Doc A folder>/`. Expect 1 (2 if your text is inside a table row — that's normal).

---

## Case 1 — UI only, no mount change

**What it tests:** that your edits are captured and survive regeneration, that an untouched save is not mistaken for a change, and that a re-run with nothing changed stays silent.

**Try these changes**
- add a sentence to a paragraph (page 004): `運用メモ：夜間確認（QA-a1）`
- change a table cell verdict (page 002): `○` → `△`
- add one row at the bottom of the first table (page 001)
- delete one bullet you don't want (page 028)
- bold a term or fix a full-width/half-width character (page 019)
- insert a bullet in the middle of an existing list
- open one page and press save with no edit at all (control)

**What you should see**
- every marker still present after sync, once each, in the place you left it
- no `（元文書の更新: …）` note anywhere — nothing conflicted
- the deleted bullet stays deleted
- the no-edit save produces no capture and no visible change
- a second sync with nothing changed changes nothing: no duplicated text, no new note

---

## Case 2 — Mount only, no UI change

**What it tests:** that source edits flow into the wiki silently, in the right place, without inventing notes or touching pages nobody edited.

**Try these changes**
- change one cell verdict in a table (page 002): `×` → `○`
- change a value written in prose (page 019): `10秒` → `20秒`
- append a sentence to a definition paragraph (page 004)
- add a new row to the first table (page 001)
- add a bullet or numbered item to a list
- delete a bullet nobody commented on (page 028)
- reword one sentence, keeping the same meaning (page 007)
- move a short block or sub-item to another position on the same page

**What you should see**
- new source text visible in the right paragraph/cell, once
- deleted content simply gone — no note, no 付録 page
- a reworded sentence shows the new wording and **no** note (wording difference is not a conflict)
- tables still render, numbering and row grouping still sane

---

## Case 3 — Both sides changed the **same** value

**What it tests:** the core promise — a real conflict is resolved in your favour, and the source's different value is recorded next to it instead of overwriting you.

**Try these changes**
- you set a timeout to `30秒` on page 019, mount sets the same one to `20秒`
- you change a verdict cell to `△` on page 002, mount changes that same cell to `○`
- you change a value and mount changes it to **exactly your value** (catch-up)
- after a note appears, run a second sync with nothing changed
- while a note is showing, edit a different line on the same page

**What you should see**
- your value stays live, with one short note beside it: `…30秒（元文書の更新: 20秒）…`
- catch-up case: your value appears once and **no note at all**
- repeat sync: still exactly one note — old notes must never stack or double
- your note survives an unrelated edit on the same page, unchanged
- never your value silently replaced by the source's

---

## Case 4 — Both sides changed **different** places

**What it tests:** that non-colliding edits coexist — nobody's work is absorbed, dropped, or moved, and coexistence produces no note.

**Try these changes**
- you add a row to the page 001 table, mount adds a different row to the same table
- you add a sentence to the page 004 paragraph, mount adds a different sentence to a nearby paragraph
- you edit page 002, mount edits a similar-looking line on page 003 (neighbour probe)
- you and the mount add **identical** text in the same place
- you add a new section in the middle of a page, then a later mount patch touches that page

**What you should see**
- both rows present once each, table structurally valid
- both sentences present once each, yours not merged into the source's paragraph
- no `（元文書の更新: …）` note anywhere — nothing collided
- your text never shows up on the neighbouring page
- identical text appears **once**, not twice
- your added section is still there after the later patch

---

## Case 5 — Deletions and moved blocks

**What it tests:** that a human decision to remove something sticks, that both sides removing the same thing is not treated as a loss, and that your text follows content when the source reshuffles it.

**Try these changes**
- you delete a bullet on page 028, and the mount deletes the same bullet
- you delete a short explanation on page 007, and the mount only rewords it
- you add a line inside a block (`（QA-a5）`) and the mount moves that block elsewhere on the page
- you add text inside a table row and the mount adds more rows to that table
- you delete a paragraph **you** added in an earlier case, then run two more syncs

**What you should see**
- same-deletion case: gone everywhere, no note, no 付録 entry
- you-deleted / mount-reworded: your deletion wins; the source's new wording appears only inside a note, never as resurrected body text
- moved block: your line travels with the block, not with the old surrounding text
- your row text stays in your row, nothing shifts into a neighbouring row
- your own deletion does not come back on any later sync

---

## Leave these alone

Don't rename a page's own title line, don't change heading levels, don't delete a whole section that contains your text, and never create a 付録 page yourself. Those paths are known-broken and would look like your mistake.

## Report format

Per case, one line: page number, your marker, marker count (1, or 2 inside a table row), whether a note appeared, pass or fail. No page text, no screenshots of source content.

## Escalate instead of fixing

Your marker count is 0 or unexpectedly high · your value replaced without a note · a note whose inside is empty or garbled · deleted text reappearing as body text · a 付録 page you didn't create · links vanishing from a page you edited.
