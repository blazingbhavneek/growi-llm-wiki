## Changes safe to do on the Mount side

The current version can safely process normal source-document updates when the existing page and heading structure remains recognizable:

- Rewrite or extend paragraphs under an existing heading.
- Change multiple facts, values, names, dates, descriptions, or recommendations.
- Add or remove ordinary paragraphs.
- Add, edit, reorder, or remove list items.
- Update table-cell values or add rows when the same table is not being edited in GROWI simultaneously.
- Make several independent changes across different sections or pages.
- Move a short, otherwise unedited block within the same page.
- Reword content without changing its meaning or structural location.

These changes may be combined in one source update. Unique surrounding text helps the synchronizer place changes correctly.

## Changes safe to do on the GROWI UI side

The current version can safely preserve normal human edits when the source is not simultaneously restructuring the same content:

- Rewrite or extend an existing paragraph.
- Correct multiple facts or replace an explanation with a human-reviewed version.
- Add or remove ordinary paragraphs under an existing heading.
- Add, edit, reorder, or remove individual list items.
- Update table-cell values or add a row when the mount is not restructuring that table.
- Make independent edits across multiple sections or pages.
- Apply small formatting corrections while keeping the underlying content recognizable.
- Save an unchanged page without it being treated as a human edit.

Human edits should be made inside existing pages and headings. Page titles, heading boundaries, ownership markers, and large structural regions should remain unchanged in this version.

## Expected results for the five change combinations

### Case 1 — Mount changes only

The mount may contain several paragraph rewrites, fact changes, list updates, and table-value changes in the same run.

Expected result:

- The updated source content appears in the corresponding wiki locations.
- Superseded source content disappears.
- Existing structure remains valid.
- No human-conflict note is created.
- Unrelated pages and previously preserved human content remain unchanged.

### Case 2 — GROWI changes only

A user may rewrite paragraphs, correct facts, update lists, and make ordinary table edits without changing the mount.

Expected result:

- GROWI edits are captured and preserved in the local wiki state.
- The edits remain visible after synchronization and publishing.
- Each addition appears once in its original section.
- A save without an actual edit produces no content change.
- No source-update note is created.

### Case 3 — Mount and GROWI make the same change

Both sides independently arrive at the same final wording, fact, list item, or deletion.

Expected result:

- The shared result appears once.
- No duplicate content is created.
- No conflict note is necessary because the two sides agree.
- A deletion made by both sides remains deleted.

### Case 4 — Mount and GROWI change different content

Both sides change the same document, but they edit separate, uniquely identifiable paragraphs, sections, lists, or pages.

Expected result:

- Both sets of changes coexist in the resulting wiki.
- Each change remains under its intended heading and on its intended page.
- Human content is not absorbed into the source-edited paragraph.
- Source content is not moved into the human-edited paragraph.
- No conflict note is created because the changes do not overlap.

### Case 5 — Mount and GROWI change the same content differently

Both sides rewrite the same identifiable paragraph, fact, recommendation, list item, or other content unit with different results.

Expected result:

- The GROWI version remains the active human-approved content.
- The different mount version is retained next to it as a source-update note.
- Independent non-conflicting changes from the same run are still applied normally.
- Neither version is silently lost or copied to another page.

# Known failed cases — fixing in the next PR

During wider conflict testing, I found cases that are not safe enough in the current version:

- Large document restructures that reorder, split, or combine multiple sections.
- Page-title changes or heading-level changes.
- Deleting or replacing a complete section containing human-edited content.
- Editing a block in GROWI while the mount moves that block elsewhere.
- Simultaneously adding, deleting, or reorganizing rows on both sides of the same table.
- Conflicts involving repeated or nearly identical paragraphs where the correct anchor is ambiguous.
- A human deletion conflicting with a substantial source rewrite of the deleted content.
- Repeated synchronization causing conflict notes to become duplicated or stale.
- Full model rewrites that omit existing facts, values, or table content.
- Remote page moves, deletions, or ownership-marker changes being treated as ordinary content edits.

I will address these conflict-pipeline cases in the next PR. That work will add safer placement, deletion handling, structural-content preservation, fact validation, and dedicated regression coverage for aggressive mount and GROWI rewrites.
