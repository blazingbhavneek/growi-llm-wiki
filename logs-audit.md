# Logging Audit — the complete map of logging in this repo, and every place that needs INFO / DEBUG / ERROR

**Audience:** developers and managers. Plain English, no jargon without explanation.
**Method:** 21 review agents read all 105 non-trivial Python files completely (the whole repo, not a sample).
This document is the compiled result. Raw per-file reports with even more detail:
`logs/audit-notes/batch-01.md` … `batch-21.md` (one per 5 files). Intermediate digests: `logs/audit-notes/digest-1..3.md`.
Line numbers were spot-checked against the current code; re-verify a line right before editing from it.

## Small word list (used throughout this document)

- **INFO / DEBUG / WARNING / ERROR** — log levels. INFO = normal progress worth showing staff.
  DEBUG = detailed developer chatter. ERROR = something failed. Live logs should show INFO + ERROR.
- **Traceback** — Python's raw crash dump. Useful for developers in `debug.log`, useless as the
  only record of a failure for someone reading the live log. Full tracebacks belong only in
  `debug.log` and its rotated backups, not in live logs, the terminal, or `failed_docs.md`.
- **Swallowed (an error)** — the code catches an error and then does nothing: no log line,
  nothing. The failure simply disappears. Written as `D` in Part 3 and listed in Part 5.
- **Choke point** — one function that most of the code passes through (like `_atomic_write`).
  Fixing it once fixes a huge area.
- **ENOSPC / EACCES / ENOENT / EBUSY / EROFS / EIO** — OS error codes: disk full / no permission /
  file not found / file is locked by something else / disk is read-only / disk hardware error.
- **OOM (out of memory)** — the machine ran out of RAM and the OS killed the program. No log line
  is written by the killed program, which is why these are dangerous.
- **Daemon / worker thread** — a helper process inside the program that runs in the background.
  If it dies quietly, the main program waits for it forever (a hang with no error).
- **Atomic write** — write to a temp file, then rename it over the real file, so a crash never
  leaves a half-written file.
- **Corrupt JSON** — the file exists but its contents are broken (often from a disk-full or a kill
  mid-write). "Corrupt" and "missing" look the same to much of this code, which causes silent
  full rebuilds.

**The manager feedback this answers:**
1. Don't delete old logs — rotate `live.log` to a new file after 1 MB; debug logs handled separately (they are bigger).
2. Keep `logs/<project>/` and the final aggregated `failed_docs.md` — confirmed, no change.
3. Stop streaming terminal output into logs. Company staff read the log files, not the terminal.
   Prints become INFO. Live logs show INFO + ERROR. Errors must carry a clear reason, not a bare traceback.
4. Find all possibilities of **system-level errors** (not program errors) and log them at ERROR level.

---

# PART 0 — DECIDED SCOPE (final)

Team decision, final. The original four manager demands all stand. The design rule:

> **Application logs go through Python's standard-library `logging` module. Full tracebacks go
> only to debug logs.** The terminal gets tqdm, machine-contract stdout where required, and
> short emergency notices if logging fails — never a copied log stream or a full traceback.
> File size and rotation are the library's job — no custom Tee, no hand-written rotation.

1. **Kill the Tee** (`publisher/failure_logs.py:36-62`). Today `live.log` is written by copying the
   terminal stream; that is the design being replaced. `capture_run_logs()` shrinks to what it
   should have always done: make the folder, reset `failed_docs.md`. Done.
2. **`live.log` = one stdlib handler** — `RotatingFileHandler(maxBytes=1_000_000,
   backupCount=99, encoding="utf-8")`, then `setLevel(logging.INFO)`. It contains **INFO,
   WARNING, ERROR, and CRITICAL**: timestamped, single-line Japanese messages with context and
   a clear reason. Use the traceback-free live formatter specified in 2.4, not the existing
   `JapaneseFormatter` unchanged. No copied prints, tqdm, or raw tracebacks. Keep default
   numeric backup names. **100 files total = current file + 99 backups**, about 100 MB/project.
3. **`debug.log` = one stdlib handler** — `RotatingFileHandler(maxBytes=10_000_000,
   backupCount=99, encoding="utf-8")`, then `setLevel(logging.DEBUG)`. **100 files total**,
   about 1 GB/project, including full exception chains. This is the default, not a floating
   1–10 GB allowance; 100 MB debug files would mean about 10 GB and require a separate budget
   decision. Sizes are approximate rollover thresholds, not hard disk quotas (2.1).
4. **Terminal = tqdm, not a logging destination.** `SyncProgress`/tqdm keep drawing on the
   terminal; they must not reach the file. Apart from the stdout contracts in point 5 and short
   emergency notices in point 9, remove the normal console log handler. Full tracebacks never
   go to the terminal, even with `-v`. The transient phase lines that
   `publisher/progress.py` writes into the transcript (330, 470-472, `write_transcript`) become
   `log.info` records like any other output.
5. **Prints → logger** (the original demand, back in full): every `print()` that should be part of
   the record becomes `log.info(...)` (full table restored in Part 2.3); machine-contract JSON on
   stdout may stay a print ONLY where scripts parse it, mirrored by one INFO line.
6. **ERROR records with a clear reason** (manager point 3): proper `log.error` records everywhere
   Parts 3-5 say so — the live handler writes them to `live.log`; recurring messages get a
   `JapaneseFormatter._MESSAGES` template ("that is exactly what japanese formatter") so the line
   is clean Japanese; unknown English still works via the "ログ詳細（原文）:" fallback.
7. **ERROR-only rule for the 81 zero-logging library/engine files** (list below): they get ONLY
   `log.error(...)` lines (+ 2-line logger header) — silent in the live log unless something fails.
   Exception: their `__main__.py`/CLI entry points (e.g. `graph/wiki/__main__.py`,
   `graph/linker/__main__.py`, `jev/benchmark.py`) are "prints", so they also get the 2.3
   print→INFO treatment like everything else.
8. **Existing-log files**: promote failure-hiding WARNING/DEBUG to ERROR, add `error=<Type>: <msg>`
   to reasonless `log.exception`s (`publisher/pipeline.py` ×7, `publisher/queue.py` ×2, …).
9. **Log-failure hardening**: handle initial folder/file setup failures explicitly; an emit-time
   `handleError` override alone cannot catch those. Disable a failed file handler without
   recursive logging, report which destination failed with one short emergency notice, and
   preserve healthy destinations (2.1). Do not dump logging-internal tracebacks to stderr.
   A logging-only disk failure must not kill publishing; real publication/data-write failures
   must still fail normally. If debug storage is unavailable, full traces cannot be guaranteed:
   report that limitation briefly, never spill them to live/terminal/failed-doc summaries.
10. **Keep**: tqdm bars, folder layout, `failed_docs.md` as the aggregated failed-document
    summary, CLI flags and failure exit status. **Deliberate changes**: `failed_docs.md` keeps
    document/reason/context and a debug-log reference, not full tracebacks (2.2); `-v` does not
    change live-log shape or the debug-only traceback rule. Bootstrap and cleanup must meet 2.1,
    rather than preserving today's unsafe order unchanged.

**Files with zero logging today → ERROR-only (81).** `common/async_tools.py, context.py, ledger.py,
legacy.py, links_render.py, llm.py, markdown.py, mokuji_data.py, paths.py, policy.py, settings.py,
storage.py` · `convert/__init__.py` · `graph/clients/chat.py` · `graph/common/*` (4) · `graph/config.py` ·
`graph/fast/model.py, passes.py, policy.py, wiki.py, writer.py` · `graph/formats/*` (context, pptx,
tabular, tree, xlsx, __init__) · `graph/linker/catalog.py, chunks.py, legacy.py, neo.py, render.py,
meta_cache.py, prompts.py, wire.py` · `graph/wiki/*` (the whole package) ·
`graph/workspace/project.py, writer.py, xlsm.py` · `index/__init__.py` ·
`jev/*` except `engine.py` and `backends/torch.py` · `linker/*`, `wiki/*`, `main.py` ·
`publisher/activity.py, history.py, ledger.py, live_verification.py, phase.py, scanner.py` ·
`runner/compat_check.py, steps.py` · plus the CLI entry points from point 7
(`publisher/__main__.py, progress.py, failure_logs.py; graph/wiki/__main__.py,
graph/linker/__main__.py, convert/__main__.py, index/__main__.py, linker/__main__.py,
wiki/__main__.py; jev/benchmark.py, jev/parity.py`).
(Regenerate: `grep -L "log\.\(info\|debug\|warning\|error\|exception\)" <the ≥30-line .py files>`.)

---
# PART 1 — HOW LOGGING WORKS TODAY

## 1.1 The three output systems running at once

**System 1 — the stdout Tee → `live.log` (the current "live log")**
- `runner/cli.py` `main()` (~line 1242) wraps the whole command in `capture_run_logs()` (`publisher/failure_logs.py:75`).
- `capture_run_logs()` replaces `sys.stdout` and `sys.stderr` with a `_Tee` object
  (`publisher/failure_logs.py:36-62`). Since HEAD ("Separate live logging from tqdm output",
  2026-10-22) the Tee knows two kinds of writes: **terminal-only** (tqdm bar redraws go via
  `terminal_stream`) and **record content** (normal prints, logger lines → terminal AND `live.log`;
  `write_transcript()` lets the progress layer write the record directly). That is why `live.log`
  is already clean and bar-free.
- Observed on the production server (`logs/aferr/`): `live.log` = clean JapaneseFormatter INFO/
  WARNING lines + stable prints, **zero exceptions or tracebacks**; `debug.log` = everything, all
  exceptions; the failure itself was visible on the terminal only. A failed run currently does not
  say so in the live record.
- `live.log` is opened with mode `"w"` (line 84): **each run erases the previous run** (`"w"` is also
  the only reason its size is capped at one run's worth).
- On startup it also deletes the legacy `failed_docs.log` files (lines 79-81) and truncates
  `failed_docs.md` (line 82).

**System 2 — the Python `logging` tree**
- Configured in `runner/cli.py:1243-1249` with `logging.basicConfig(force=True)`:
  - `TqdmStreamHandler` → stderr, level **INFO** (DEBUG with `-v`), formatter `JapaneseFormatter`
    (normal) or `ShortNameFormatter` (`-v`). Defined in `publisher/progress.py:89-215, 217-236`.
  - `logging.FileHandler(logs/<project>/debug.log, mode="w")`, level **DEBUG**, format
    `%(asctime)s %(levelname)s %(name)s %(message)s` — **also erased every run** (line 1246).
- Third-party loggers silenced to ERROR (`asyncio`, `httpx`, `urllib3`, `openai`, `langchain*`,
  `anthropic`) at `runner/cli.py:1252-1266`.
- `capture_failure_logs()` (`publisher/failure_logs.py:120-141`) attaches a temporary handler that
  collects tracebacks + `failure_summary` warnings during one command, then `_write_summary()`
  turns them into `logs/<project>/failed_docs.md` (keep) and prints a failed-document summary to
  stderr (which the Tee copies into `live.log`).

**System 3 — progress callbacks (not logging at all)**
- The engines (`graph/wiki/pipeline.py`, `graph/fast/*`, `graph/linker/service.py`, …) emit
  structured events to an `on_progress` callback; `publisher/progress.py` `SyncProgress` draws
  tqdm bars and prints phase lines. These reach `live.log` **only through the Tee** — once the Tee
  is removed, these engines become completely invisible in logs unless milestones get `logger.info`.
- Many engines also write failure detail into **artifact `.txt` files** (`*-error.txt`,
  `work/page-NNN/…`) instead of logging — a reader of the log files never sees them.

## 1.2 What the code emits today (whole-repo counts)

| Kind | Count | Where |
|---|---|---|
| `print()` | 42 | 14 files: `runner/cli.py`, `runner/compat_check.py`, `*/__main__.py` ×7, `publisher/failure_logs.py:230,234`, `publisher/progress.py:330,470`, `graph/wiki/prompts.py` (docs), `jev/benchmark.py`, `jev/parity.py` |
| `logger.info` | 22 | mostly `publisher/pipeline.py`, `graph/workspace/parser_client.py` |
| `logger.debug` | 39 | |
| `logger.warning` | 39 | many real failures hide at this level |
| **`logger.error`** | **3** | `publisher/pipeline.py:1083`, `runner/cli.py:178`, `runner/cli.py:742` — that's everything |
| `logger.exception` | 31 | raw traceback dumps, usually without a plain-language reason |
| swallowing excepts (`pass`/`continue`) | ~31 | see Part 5 |

**Plain summary:** everything the terminal shows gets copied into `live.log` (prints, progress
text, partly-translated logger lines, tracebacks). Real errors mostly appear as raw tracebacks or
not at all. Old logs are deleted at startup. Rotation does not exist. There are effectively two
ERROR messages in the entire program.

## 1.3 Files with zero logging infrastructure problems but zero output too
`common/git.py`, `common/hashing.py`, `common/markers.py`, `common/images.py`, `graph/clients/embeddings.py`,
`graph/common/hashing.py`, `graph/formats/csv.py|docx.py|md.py|pdf.py`, `graph/growi/paths.py|publisher.py`,
`linker/policy.py`, `wiki/policy.py`, `runner/compare.py` and the `__init__.py` stubs were also scanned:
they contain no prints and no logging (thin re-export or pure-logic modules). Findings for anything
that fails inside them belong to their callers (noted in the per-directory parts below).

---

# PART 2 — THE FOUR MANAGER REQUESTS, MAPPED TO CODE

## 2.1 Rotation and retention — 100 files per log type, per project

Exactly two lines destroy history:
- `publisher/failure_logs.py:84` — `open(root / "live.log", "w")`
- `runner/cli.py:1246` — `FileHandler(log_dir / "debug.log", mode="w")`

Design (final, per Part 0 — Python stdlib handlers, append mode, default numeric names):

| Log | `maxBytes` per file | `backupCount` | Files including current | Approximate budget/project |
|---|---:|---:|---:|---:|
| `live.log` | `1_000_000` (1 MB) | `99` | `100` | `100 MB` |
| `debug.log` | `10_000_000` (10 MB) | `99` | `100` | `1 GB` |

These are decimal MB/GB. **`backupCount` counts old files only**: setting it to `100` would
keep 101 files including the active file, not 100. Budgets apply separately to each project
and exclude `failed_docs.md` and other artifacts. A 10 GB debug budget would require 100 MB
files at this retention count; that is an optional later decision, not the default here.

1. **`debug.log`** — its own larger budget, full tracebacks preserved:

   ```python
   from logging.handlers import RotatingFileHandler
   debug = RotatingFileHandler(log_dir / "debug.log", maxBytes=10_000_000,
                              backupCount=99, encoding="utf-8")
   debug.setLevel(logging.DEBUG)
   debug.setFormatter(logging.Formatter(
       "%(asctime)s %(levelname)s %(name)s %(message)s"))
   ```

2. **`live.log`** — its own handler; `LiveJapaneseFormatter` below is a proposed implementation
   name, **not a stdlib class**. An equivalent formatter is fine if it meets all of 2.4:

   ```python
   live = RotatingFileHandler(log_dir / "live.log", maxBytes=1_000_000,
                             backupCount=99, encoding="utf-8")
   live.setLevel(logging.INFO)  # INFO, WARNING, ERROR, CRITICAL; never DEBUG
   live.setFormatter(LiveJapaneseFormatter())  # implement the contract in 2.4
   ```

   Configure the root logger at DEBUG so the debug handler receives DEBUG records; attach each
   file handler once, with module loggers propagating instead of attaching duplicate handlers.
   The snippets show configuration, not startup-error hardening; production construction must
   follow point 5 below. Do not switch the live formatter to `ShortNameFormatter` under `-v`.
3. **Restart and pruning behavior**: default mode is append, so restarts do not truncate logs.
   Rotation happens by size, not at every new run. The active files remain `live.log` and
   `debug.log`; archives are `.1` (newest) through `.99` (oldest). When another archive is
   needed, the oldest is deleted. Here "don't delete old logs" means no startup wipe, **not
   keep history forever**: this finite retention policy deliberately prunes the oldest backup.
   Write run-start/run-finish summaries with timestamp, run ID, project, and outcome so runs
   sharing the active file can be distinguished.
4. **No timestamp `namer` shortcut or hard-cap promise**: use the default `.N` chain. A namer
   using the current timestamp can break rollover renaming, not merely pruning; the previous
   "3-line prune" suggestion is withdrawn. Timestamped archives need a separately designed and
   tested policy. `maxBytes` is a rollover threshold, not a hard file-size/disk quota: a single
   oversized record can exceed it, and Japanese UTF-8 output must be tested. Never split or
   discard a traceback merely to make a file exactly fit the threshold.
5. **Harden startup as well as writes**: guard directory creation, handler construction/open,
   `failed_docs.md` reset/write, emit/rotation, and flush/close. `handleError` handles errors
   raised inside the rotating handler's `emit`, **not constructor/directory-creation failures**.
   Use one tested handler subclass that marks only the broken destination disabled and emits
   at most one short, best-effort notice per failed destination to original stderr. Do not log
   from `handleError` back into the same logger, or rely on `logging.raiseExceptions = False`
   to disable handlers or provide a fallback: it only suppresses the default diagnostic.
   Keep healthy destinations working and the publish run alive after logging-only failures;
   never turn an actual business/data failure into success. No default logging-internal
   traceback may leak to stderr, including during shutdown. If both file destinations fail,
   use only concise emergency messages without stack traces; do not promise unavailable debug
   evidence. A broken stderr is best-effort too and must not cause a second logging crash.
6. **One coordinated file writer**: stdlib rotating handlers support threads in one process,
   not independent processes rotating the same paths. Check overlapping `watch`, `check`,
   `status`, and other CLI invocations. Existing business locks are acquired after today's log
   setup, so do not assume they protect rotation. Either enforce one writer before opening
   project log files for the entire invocation, or use a shared single-writer listener when
   simultaneous commands must be supported. Preserve required command behavior; document and
   test the chosen policy. Never let two independent rotating handlers own the same path.

Library references: [Python 3.13 rotating handler](https://docs.python.org/3.13/library/logging.handlers.html#rotatingfilehandler),
[naming requirements](https://docs.python.org/3.13/library/logging.handlers.html#logging.handlers.BaseRotatingHandler.namer),
[multiple-process limitation](https://docs.python.org/3.13/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes).

## 2.2 Keep `logs/<project>/` and the aggregated `failed_docs.md`

Layout: `logs/<project>/live.log`, `live.log.1 … .99`, `debug.log`, `debug.log.1 … .99`,
and `failed_docs.md`. No `old/` directory. The whole `logs/` tree stays gitignored
(`.gitignore:54`); the current file keeps its stable name.

Keep failed-document grouping, clear reasons, redaction, and the final aggregate report.
**Remove embedded full tracebacks from that report** to honor the debug-only rule:
`publisher/failure_logs.py::_write_summary` currently appends `trace.rstrip()` into its body.
Instead include the document, exception type/reason, timestamp/run ID, and a reference to the
debug-log family (rotation moves records, so do not promise a permanent line in `debug.log`).
Keep capture/matching logic where needed for useful summaries; do not duplicate full traces
into Markdown or other newly added diagnostic outputs.

## 2.3 Remove the Tee; terminal = progress, not a log stream; prints → INFO

1. **Delete the `_Tee`** (`publisher/failure_logs.py:36-62`); `capture_run_logs()` keeps only the
   folder setup + `failed_docs.md` reset; stop wrapping `sys.stdout/stderr`. `live.log` is now the
   handler from 2.1 — nothing else can write to it, which is exactly why tqdm can no longer leak
   in: "control tqdm in live.log" is solved by construction (tqdm keeps `terminal_stream`/stderr
   for the bars; it simply is not the log file anymore).
2. `publisher/progress.py`: the `write_transcript` plumbing (`_write`, `_ensure_phase`, 330,
   470-472) writes the same lines through `log.info` instead.
3. **Print conversion list — everything here becomes `log.info` (or ERROR where noted); JSON on
   stdout may remain a print ONLY where scripts parse it, always mirrored by one INFO line:**

| File:line | Today | Live-log line |
|---|---|---|
| `runner/cli.py:170` | per-path done-row JSON | keep print (contract) + `INFO processed path=… status=…` |
| `runner/cli.py:174-178` + `publisher/failure_logs.py:234` | end-of-run failed-document list (print; per-failure ERROR on one path only) | `for f in failures: log.error("document failed: %s", f)` + `INFO failed documents: %d; details: %s` — **without this the Tee removal erases every run's failure list from the log** |
| `runner/cli.py:253-266` | `check` status per endpoint | INFO each; **line 260 DOWN → `log.error("check %s unreachable url=%s error=%s")`** |
| `runner/cli.py:316, 849, 876, 976, 1109-1111` | republish notice / recovered-on-retry / pull JSON / convert summary / relink | one INFO each |
| `publisher/failure_logs.py:230` | "failed to save error log" print | it IS an error: `log.error("failed to save failure evidence to %s: %s")` |
| `publisher/__main__.py:22,28,31`, `wiki/__main__.py:29`, `linker/__main__.py:21,27`, `index/__main__.py`, `convert/__main__.py:21` | CLI result JSON | keep print + one INFO each; publish failures>0 → also ERROR |
| `graph/wiki/__main__.py:82-89` | every pipeline step printed to stderr | `INFO wiki stage=… step=… detail=…`; `[fallback]`/`error=` fragments → ERROR |
| `graph/linker/__main__.py:91,104,106,129,137` | Jev calibration lines + status dict | INFO each; "no threshold reached 0.9" → ERROR |
| `jev/benchmark.py:47-48`, `jev/parity.py:47` | benchmark table, parity JSON | INFO rows + final line; parity FAILED → ERROR |
| `runner/compat_check.py:105` | compat JSON report | keep print + INFO summary |

4. **Engine milestones** (progress-callback output that used to reach the log via the Tee) are in
   the Part 3 "A" items — now in scope again; keep per-attempt detail at DEBUG so it only goes to
   `debug.log`.
5. Remove the normal `TqdmStreamHandler` from bootstrap; otherwise INFO/ERROR logs and their
   tracebacks still reach the terminal. Do not remove tqdm itself or required JSON stdout.
   The only logging-related stderr exception is the short emergency path in 2.1.

## 2.4 Errors must carry a clear reason (this is Parts 3-4)

Fix pattern for an exception at its reporting boundary:

```python
except OSError as exc:
    log.error("write failed path=%s error=%s: %s", path, type(exc).__name__, exc,
              exc_info=True)
    raise  # where the operation must still fail; preserve the existing failure behavior
```

The record carries both a useful reason and exception information: live shows the short reason,
debug shows the full traceback. A plain `log.error(...)` without `exc_info` does **not** send a
traceback to debug automatically. Existing `log.exception` calls can stay, but their message
must name the reason too. Report once at the layer that knows the failed operation/document;
avoid repeating the same error at the helper, caller, and top level.

**Required live formatter contract (`LiveJapaneseFormatter` or equivalent):**

- At the reviewed baseline, `JapaneseFormatter.format` delegates to `logging.Formatter` with
  exception information intact, so it **does append tracebacks**. Reusing that baseline
  formatter unchanged is a known implementation bug; changing its implementation to meet this
  contract is also valid.
- Format a **copy** of the record. Clear `exc_info`, cached `exc_text`, and `stack_info` on that
  copy before Japanese formatting. Clearing only `exc_info` is insufficient if the debug
  formatter ran first and cached `exc_text`. Never mutate the shared record: debug and failure
  capture must retain its original exception information. Test both handler orders.
- Every live record has a timestamp with an explicit timezone, level, operation/stage, relevant
  document/path/endpoint, and a human-readable reason on failure; add a run ID for correlation.
  Keep the same shape under `-v`. Preserve Japanese messages and full useful paths, not the
  terminal's shortened columns. Existing DEBUG-and-higher text formatting is fine for debug.
- Escape embedded CR/LF in live messages and exception reasons so one event stays one physical
  line. Do not insert `traceback.format_exc()` output into an ordinary message: clearing
  exception fields cannot hide a traceback already embedded in `record.msg`.
- Redact credentials and sensitive endpoint query values in both logs and summaries. Preserve
  useful exception/path context; do not dump arbitrary prompts or response bodies into live.
- CLI failure boundaries and uncaught-thread/async-exception reporting must route full traces
  to debug without the default traceback dump to stderr. Preserve nonzero exits, cancellation,
  and actual failure behavior; do not swallow exceptions to silence them. Before bootstrap or
  if debug storage fails, use a concise emergency notice and explicitly accept that debug
  evidence is unavailable. This is reporting policy, not permission for unrelated bug fixes.

Library reference: [exception formatting and the shared `exc_text` cache](https://docs.python.org/3.13/library/logging.html#logging.Formatter.format).

Two rules so these lines look right in the live record:
1. **Feed the formatter.** For recurring messages add the template to
   `publisher/progress.py` `JapaneseFormatter._MESSAGES` (or write them in the `stage=` key=value
   style the formatter already translates), so live.log shows a clean Japanese ERROR line —
   "that is exactly what japanese formatter" does. Unknown English still works (it gets the
   "ログ詳細（原文）:" prefix), it just reads worse.
2. **Records, not prints/tracebacks.** The failure must produce a `log.error` *record*; the live
   handler writes it into `live.log`. Today most failures produce only a terminal traceback — and
   server evidence shows the live record never mentions the failure. Verification per change
   batch: cause one test failure and check the ERROR line is in `live.log`.

---

# PART 3 — EVERY DIRECTORY, EVERY FILE: WHAT TO CHANGE

Legend: **A** = print/callback → INFO (in scope again under Part 0 — anything that should be in
the record must become a real log call). **E** = program-level ERROR gap. **S** = system-level gap
(full cross-cutting list in Part 4). **D** = swallowed error. All "today" states verified by the audit.

## 3.1 `common/` — shared helpers (today: ZERO print, ZERO log calls in every file)

**`async_tools.py`** — S: `thread.start()` refused by OS (25) = thread/file limit or OOM → bare traceback. E: async phase failure (28) re-raised with no log, thread context lost → log once at the layer that aborts the run.

**`context.py`** — `Context.stage()` builds a *perfect* failure line ("stage X failed for document Y: Type: msg") then only emits it through the callback; with `emit=None` (36) the event is **thrown away** and the error re-raises as a traceback. E: also `log.error` that same line (53). This small change alone gives every stage of every document an ERROR record.

**`ledger.py`** — E: corrupt JSON (35), unsupported `schema_version` (37), malformed fields (39-41) → traceback-only; ERROR must name file + version + "supported 1-4". S: `exists()` False on EACCES (33-35) makes a permission problem look like a *first run* → empty ledger → **mass re-publish** of the whole wiki. Save path (48) unprotected ENOSPC/EACCES.

**`legacy.py`** (adapter layer) — every one of 13 adapters (parse 41, publish 78, link 84/92, …) re-raises unlogged; this layer uniquely knows the document/path argument → one `log.error("parse failed for source <s>: …")` per adapter (pick this layer OR the inner module, not both). S: missing parser_base_url → empty string (32) → confusing URL failure later; broken deps surface mid-phase as bare `ModuleNotFoundError` from 15 lazy imports.

**`links_render.py`** — E: bad edge data → KeyError/TypeError naming no page (16/22/28) → log at the caller that owns the page name.

**`llm.py`** — E: structured call failure / retry exhaustion / validator reject (37, 39) raise with **no document context** → `log.error("LLM structured call failed for <doc> (schema=<s>): <err>")`. S: chat/embed network failures (37, 48) propagate unlabeled. A: optional INFO per call (char count) since `count_call` is in-memory only and invisible.

**`markdown.py`** — D/E: big HTML table silently replaced with "[large table omitted]" (38) → content loss invisible; source decoding to empty string (13-15) unnoticed → caller logs ERROR "source normalized to empty; convert/encoding may have failed".

**`mokuji_data.py`** — D (repo top-10): `except ValueError: continue` (106-107) drops broken JSONL records → **search data quietly wrong forever**; non-dict lines (110) dropped. E: missing header record (112), unknown version (116) return `None` exactly like "no block exists" → ERROR distinguishing "corrupt" from "absent".

**`paths.py`** — S: `DataLayout.ensure()` mkdir (122) uncaught → disk-full/read-only crashes with raw traceback, never names the directory. A: INFO on first-run directory creation (120-123).

**`policy.py`** — E: invalid policy name (150), offline-not-supported (93) traceback-only, value not named. D: missing/mis-set policy field silently resolves to "standard" (156). A: INFO "policy resolved: …" (144-149).

**`settings.py`** — E: unknown override key (62); int/float cast failures name the value, not the setting (90-104). S: missing/unreadable INI or env var → traceback not naming the project file (59). A: INFO which project/config loaded, mount redirect (78).

**`storage.py` — the single highest-value file in the repo for system errors.** `_atomic_write()` (34-45): **every atomic write in the project** — convert state, ledgers, checkpoints, wiki pages, linkers — passes through its mkdir/mkstemp/write/flush/fsync/`os.replace`. ENOSPC / EACCES / EROFS / EIO / locked-file EBUSY / Windows WinError 5/32 all exit as bare tracebacks that **never name the target path**. One catch-all ERROR here ("write to '<path>' failed: <errno/strerror> (disk full, permissions, or file locked?)") covers every write that **goes through this module** — measured: 5 caller files; `graph/wiki/storage.py:99` is a **separate copy** of the same function (23 caller files) needing the identical change, and ~120 direct `write_text`/`open` sites go through neither (7.4). Also: `clean_workdir` (76-78) rmtree-then-mkdir — if mkdir fails, contents are already gone (ERROR must warn half-deleted state); temp-cleanup failure (44) masks the real write error via exception chaining; `read_json`/`read_text` (64-71) ENOENT vs corrupt vs encoding all traceback-only without file name; `json.dumps` failure (31, 56) → TypeError far from the builder.

**`convert/__init__.py`** — E: missing source (77-78) raises with only the path (`is_file()`=False on EACCES misleads); stat race after move (103); document/parser failures (78/90/91) with no source context. S: corrupt convert state (102).

**`convert/__main__.py`** — A: result JSON print (21) keep + INFO. S: no logging configured at all for standalone runs.

## 3.2 `graph/config.py` — the big settings + mermaid module

- E: ~52 `int()/float()(env(…))` casts (339-585, first at 360) — a bad number in `.env`/INI dies with a ValueError naming the *value*, never the *variable*. One wrapper + ERROR naming `WIKI_…` fixes all 52. Line 69 runs **at import time, before logging exists** — keep that in mind for the fix order.
- E: missing `[project]` section keys (80-99), bad `target_name`/absolute path rules (93-105, 563), unknown setting names, pydantic failures — traceback-only, INI path never named.
- S: `load_dotenv()` silence on missing `.env` (22) → run continues on defaults, fails later as confusing auth errors → ERROR ".env not found or unreadable".
- S: configparser (91) **silently skips an unreadable INI** — permission-denied is reported as "config file not found"; non-UTF-8 INI = raw UnicodeDecodeError without the path.
- E+S: mermaid: binary missing from PATH (1452-53), timeout / start-failure / nonzero-exit of `mmdc` (1461-72) all converted to `(False, reason)` tuples no logger ever sees → "all diagrams skipped" happens invisibly; unrepairable diagram after retries (1509-21) only a UI event; D: `except Exception: break` (1504-05) makes a **full LLM outage identical to "model couldn't fix the diagram"**; D: sanitizer failure (1763-68) passes huge unsanitized payloads to the LLM untraced. Temp dir/file writes (1456-59) ENOSPC/EACCES uncaught.

## 3.3 `graph/clients/`, `graph/common/`

**`chat.py`** — E: hard timeout fires (117) re-raised with no log of value/endpoint/model; up to 3 failed JSON attempts (142-148) — attempts 1-2 leave no record; unrepairable JSON (43-53, 66-67, 111-13) traceback with no response sample → ERROR quoting first ~120 chars + document. S: all vendor/httpx errors (110, 143: refused, DNS, reset, TLS, timeout) uncaught — the retry loop even retries a dead server 3× → ERROR naming `base_url`; D: unparseable `request_timeout` (86-89) **silently disables the hard timeout** → calls can hang forever; `max_tokens` cap disabled (98-101) → OOM kill uncorrelatable. A: INFO when fallback plain-JSON prompt retry starts (124).

**`embeddings.py`** — thin client, no output; failures surface in `catalog.py` (3.7).

**`graph/common/async_tools.py`** — same as common version: thread-start OS refusal (25-26),
exceptions (21-22) re-raised with no log at this layer (log at the caller that aborts the run;
the `else None` branch at :29 is unreachable — do not add anything there, see 7.1 #7).
**`graph/common/images.py`** — D: invalid base64 payload (94) → opaque token, corrupt media never surfaces; huge data URI (92-93) → possible OOM kill, optional size-guard ERROR naming the document.
**`graph/common/markdown.py`** — E: `chunk_text` with non-positive size (90) → traceback without document/caller.
**`graph/common/prompts.py`, `markdown.py` (text utils)** — clean, no logging belongs in them.

## 3.4 `graph/fast/` — fast repair / fast link engines (today: almost no logging; repair.py has 6 `log.exception`, the rest have ~none)

**`inline_linker.py`** — E: metadata extraction fails 3× (523-26) log exists but no reason; per-page failures ABORT the whole run (1043-45) but logged only WARNING (549-56) → promote to ERROR; Jev role/candidate exceptions captured then dropped, only a count survives (649, 696-98, 821-29) → per-item ERROR; Jev engine unavailable (1133-35) no reason in message; reset/undo failures only in return lists (1001-03, 1167-71) → **fake links stay on published pages silently**. S: 13 atomic writes (558-1176) ENOSPC unprotected; `read_text` UnicodeDecodeError + `rglob` PermissionError (365-385-928-1026-1167, 376-80); corrupt manifest (175) / page cache (491-92: silent re-extract, hidden cost) / run.json (979 — kills the checkpoint write itself) / pipeline.json (1019). D: manifest/page mismatches `continue` (363-64, 382-83). A: milestones (see 2.3 table).

**`linker.py`** — E: whole page-batch metadata call fails → **bare `return`, zero log lines** (90-96); Jev judge fails → WARNING but all related-page edges for the chunk are dropped (167-71) → ERROR. S: artifact writes 87-98 unprotected. D: invalid model answers `continue` (100-108). A: 113-114 answered M-of-K (M<K = silent partial), 79-80 cancelled-by-flag.

**`model.py`** — zero logging; E: blank judge API key silently falls back to chat key (202-216) → later 401 with no hint; endpoint identity (writer vs judge, base_url) lost before upstream errors can name it (62, 83-89). One run-start INFO + type-qualified ERRORs upstream fix both.

**`passes.py`** — D: cancelled-task failures discarded via `return_exceptions=True` (59-62); exceptions pass through without item identity (23-27, 57) → the operations' own ERROR lines must name the failing chunk/page.

**`repair.py`** — E: judge/writer call failures live only in artifact files (658-60, 695-708, 799-812, 948-62); rewrite budget exhausted → page needs human review, callback only (743-77); **`page_failures` list collects every page failure and is never logged** (1687, 1963) — loop-and-ERROR is the cheapest big win in the file; document-load validation (1348-1422) uncaught. S: corrupt checkpoints/sidecars (1125, 1211, 1380) crash uncaught; atomic writes (1228-1933) traceback-only (the two commit paths ARE caught at 1691/1736 but messages lack the reason); review-file read after ALL model work (1204) encoding crash = lost work. D: corrupt plan.json → silent queue-order default (1301-05); resume silently disabled per page (1808-09). A: full milestone list in 2.3.

**`wiki.py`** — S: corrupt hierarchy checkpoint (315) crashes (default only covers missing); checkpoint write (327) unprotected.

**`writer.py`** — D-top-10: writer/judge API errors appended to a local list, never logged (234-41, 266-76) → **broken API key looks like a normal run**; whole-page exception → silent verbatim source fallback (381); no acceptable candidate → page published as raw source dump, visible only in the progress bar (298-313) → ERROR "falling back to verbatim source: page=…, last errors:…". E: unresolved image placeholders abort commit loop (430-32) traceback-only. S: artifact/page writes (413, 457-66) mid-loop partial state. A: 210/282/467/474 + final count line.

**`graph/formats/`** — `context.py`: D: parent-summary model failure swallowed entirely (67-68) → silent quality drop weeks later; checkpoint write (72)/read (50) S; A: summaries-ready INFO (76). `pptx.py`: D: ALL slide-sectioning LLM failures → silent heuristic fallback (118-23); A plan milestones. `tabular.py`: D: orientation call failure → silent row fallback → **wrong records/statistics** (306-07); SQL errors returned as "error:" strings (444, 452-53); corrupt sheet cache = "no cache" (553); E: unguarded analysis call (625-30), malformed specs/empty ranges (346-86), IndexError; S: atomic writes 518-644, link-rename sweep encoding (604-610 file never named), sqlite in-mem (422). `xlsx.py`: E: missing/corrupt plan or page file → opaque KeyError (212-264); S: writes/reads 118-347; D: corrupt plan/source → silent full rebuild (304-08). `tree.py`: E: unclosed fence names line not document (34). `csv/docx/md/pdf` — thin, findings belong to callers.

## 3.5 `graph/growi/client.py` — the GROWI HTTP client (1,644 lines)

Today: `log.exception` at pull-block states only; **zero ERRORs** for anything else.

- E+S (highest-value system spot in the client): every httpx error (114-127: ConnectError, DNS, timeout, TLS, read error) uncaught → wrapper ERROR "GROWI network request failed (<method> <path>): Type — check server URL and network".
- E: every HTTP 4xx/5xx → GrowiAPIError raised unlogged (128-29) → ERROR with method, path, status, detail + "check API token" on 401; 409 conflict (803-06) → "page changed by another editor"; preflight/publish failures (750-57, 812-16) only tqdm events.
- D-top-10: `health()` (174) swallows network/DNS/auth into `False` — **the one failure with zero evidence anywhere**.
- E: expected pull-block states (remote deleted/moved, markers removed) logged via `log.exception` = full traceback every run (1197-1210, 1399) → one-line live ERROR; retain captured exception information for debug only, following 2.4, rather than stripping it from the shared record. Corrupt `source.json` marker seed (849, 1211) surfaces later as misleading "source mapping missing".
- S: local reads (863, 1249) ENOENT/EACCES/Unicode; publish ledger writes (936-989) unprotected. E: invalid embedded image (468-69) ValueError names no page → include page id + image hash. D: original ValueError dropped during retry; "not_landed" unlogged (1228).

## 3.6 `graph/linker/` — catalog, chunks, judges, service (today: `__main__.py` 5 prints / 0 logs; `legacy.py` infamous swallow)

**`legacy.py`** — D **worst in repo**: `except Exception: continue` (96-97 — round 2 verified the
round-1 98-99 citation was off by two) on every edge-model call → whole candidate groups silently
deleted from link quality, no counter, no artifact. Same bug in `neo.py:159-166` (without even an
artifact). E: missing target chunk → `AttributeError: None` with no names (88) → guard + ERROR with
chunk_id/document.

**`neo.py`** — as above; E: invalid `wiki_linker_hop_caps` silently defaulted / bare raise (87-88).

**`catalog.py`** — D: embedding batch failure → rollback + zero logs, every run forever (351-54); corrupt/old-schema catalog DB → "no mode stored" (66-69); `sqlite_vec` load double-swallowed → **50× slower Python fallback with no explanation** (45-49). E: `insert_edge` False (chunk missing/stale) + missing rows hit bare `continue` (428-451) → edges silently dropped; report "restored N/M" counts, ERROR if ratio high. S: catalog mkdir/DB-create (33-35, 79-121), `flock(LOCK_EX)` **blocks forever with zero output** (142-150) → at least INFO "waiting for linker lock", "database is locked" after 30s traceback-only, links.json write (488), sqlite commits (118-172-307-316). A: open/sync/embeddings-done/links-written INFOs (57, 188, 355, 488).

**`chunks.py`** — E: describe call fails after retry → chunk keeps degraded 300-char summary, only artifact (342-51, 384-88). D: `except Exception: return ChunkMeta()` (236-40) — a chunk looks cached while carrying nothing. S: non-essential artifact writes (335-90) abort the whole describe pass; snapshot IO (267-82).

**`jev_judge.py`** — six Jev engine calls (93, 130, 215, 233, 271, 285) with **no try at all**: subprocess crash, model-load OOM, refused socket, timeout → traceback naming no target/team. E: role-check and tie-break failures WARNING-only (62-65, 261-62) → promote to ERROR (WARNINGs DO
appear in live.log since the handler level is INFO — this is severity reclassification, not
visibility: related-page edges are dropped).

**`service.py`** — E: edge call failure → artifact + continue (373-78); curator failure → silent old references (258-60); **whole-document link failure written to `linker.json` + re-raised into a sink that never logs** (897-901) → ERROR "linker failed for document X (run Y)". S: planning mkdir before try (454), 7 atomic writes (319-977) — the failure-marker write (898) can **replace the original error** on disk-full; page read/write via render (291, 315); service locks (480, 934, 957) → "lock unavailable", not traceback. D: missing original page `continue` (286-87); `remove_document` on missing DB → silent `[]` (952). A: 10 lifecycle INFOs (463-907).

**`meta_cache.py`** — D: corrupt entry → `continue`, cache rots, hidden extra LLM calls (34-37) → WARNING count. S: cache write (46) crashes into the ahead-worker.
**`render.py`** — E: malformed managed-links footer → `[]`, existing references lost next render (209-15) → WARNING; inline anchors demoted silently (151-71) → INFO count; invalid limits raise bare (121-22). A: "wrote page" INFO (230).
**`wire.py`, `prompts.py`, `policy.py`** — clean. **`__main__.py`** — S: `source.json`/`chunks.json` reads with no document named (28, 31, 131); Embedder init failure swallowed → `embedder=None`, vectors silently off (49-50) → ERROR naming missing key/model; D: `return_exceptions=True` + isinstance filter (94-98) → calibration numbers from partial sample unreported; E: calibration goal missed (106) plain print → ERROR; unknown document (149) → ERROR before SystemExit.

## 3.7 `graph/wiki/` — the document→wiki pipeline (biggest subsystem)

**`pipeline.py` (2,088 lines)** — only output channel is `_emit` (see 3.4-A). D-top-10: three resume-cache swallows (934, 997, 1215) `except (KeyError, OSError, TypeError, ValueError): return None` — corrupt plan/cache/page-state → **silent hours-long re-plan/rebuild**, and they swallow OSError so **disk problems hide inside "model failed" paths**; caller says "could not be resumed" without the reason. E: writer attempt failures per attempt (1370-74); **no writer attempt passed → section published as verbatim source, event-only (1490-96) — the most important quality ERROR in the subsystem**; judge says info missing/unavailable (1427-38, 1467-80) → under non-strict policy unreviewed sections ship as clean; `_structured_with_artifacts` failure sink for ALL research/judge calls (564-70) → `return None` + artifact, zero logs; reference research failed → page written with zero cross-page facts (830-35); no model client → verbatim publish looks like a smoke test (1289-95); structural plan rejected → forced expensive path, event-only (1058); 0-page plan or section-less page reported as SUCCESS (1928, 1599-1602); PipelineError/ResumeUnavailable reasons (1572, 1641, 1783-96, 1902) traceback-only. S: ~20 write sites (run dir, snapshots, prompts, results, state, `_review.md`: 487-2075) ENOSPC unprotected — at 562 a **disk-full on the success write is misread as model failure and retried pointlessly**; cache write (1005, called 1124/1926) kills the whole doc build for an optional optimization; source read (1854) ENOENT/encoding; follower waits on planner leader **with no timeout** (1110, 1138) → leader dies = run hangs forever; mkstemp/TemporaryDirectory EMFILE under concurrency (1113).

**`model.py`** — no `except` at all in the file: one ERROR in `_run` "model call failed role/model/endpoint: Type: msg" then re-raise (131-34). E: unknown content shape → `str(part)` junk silently (121-27); timeout → bare `TimeoutError`, no model named (118-19); client construction failure never names the env var (69-75).

**`page.py`** — E: bare `assert` (118-19) → AssertionError with no text/page/range (and deleted by `python -O`) → put range + page into the message.

**`document_map.py`** — D-top-10: all regional/semantic attempts fail → degraded fallback, run "succeeds" (471-72, 574) → ERROR with attempt count + last error; three resume-cache `except (OSError,…): pass` (421-22, 534-35, 630-31) → invisible costly re-plans. S: mkdir/atomic-write family (424-748), `clean_workdir` (635), encoding (85, 151, 291).

**`windows.py`** — E: every window attempt fails → mechanical placeholder, stage reports success (275-79) → ERROR naming window/lines/document/last error; validation rejects (260-67) artifact-only → WARNING. S: checkpoint writes outside try (245, 271-73, 291-98); write INSIDE retry try (259) → disk-full retried as "bad model answer" burning all attempts; corrupt checkpoint = normal cache miss (227-28). A: observe start/done INFOs.

**`incremental.py`** — D: `read_json(default={})` × 5 (261, 377, 412, 458, 480) → *missing or
EACCES* state = `{}` = unexplained full rebuild; a *corrupt* state file instead raises uncaught
(4.7a-c) → ERROR distinguishing the three cases; `rmtree(ignore_errors=True)` ×3 (382, 455, 471). E: git diff failures / no hunks / wrong tier (93, 101, 429) raise-only → ERROR with document + stderr excerpt. S: /tmp diff files (78-81), state writes (399-502), git binary missing → `FileNotFoundError` that never mentions git (82-92). A: diff size + tier decisions INFOs.

**`legacy.py` (2,878 lines, old pipeline)** — E: 300 s timeout re-raises unlogged (1052); both structured-call fallback attempts vanish (1083-86); split retries ×6 at DEBUG incl. bad-key/refused (1912-41); mechanical fallback quality drop at DEBUG (1927); enrichment fallbacks to generic names/"一般" DEBUG-only → published artifact names change silently (2444-89); integrity gates (2616, 2628 — the single most important gate) raise good messages **without the document name** → pipeline must log at ERROR with document. S: writes/mkdirs (210-2551), rmtree under CLEAN_OUTPUT (2680) mixing old+new output, daemon thread dying at shutdown → confusing KeyError (2852-75), invalid timeout disables timeout (1016-17), read_text encoding family. A: 1900/1927/2036 → INFO.

**`storage.py`** — E+S: the shared artifact read/write root: write failure / non-serializable /
corrupt JSON re-raised **with no file name in the message** (109-134); `read_json(default)` —
missing/EACCES silently returns the default (EACCES lies), corrupt raises nameless (129-140; see
4.7a-c); `os.replace` on locked/RO target (108); rmtree/mkdir/mkstemp (26-102); encoding (134-141).
NOTE: this is the **second, independent** atomic-write implementation — changes to
`common/storage.py` do NOT apply here (see 7.4 coverage map).
**`export.py`** — E: missing/corrupt plan/manifest, `KeyError: pages/filename`, missing published page → tracebacks naming no run/page (25-58). S: rmtree dest partial delete (30), copyfile (44, 102).
**`config.py`, `ids.py`, `schemas.py`, `markdown_blocks.py`, `images.py`, `__main__.py`, `wire.py`** — config/ids/prompts: none needed. markdown_blocks (119-213) & images (132, 311, 334): integrity raises need the document name from the pipeline logger. `__main__.py`: bad `--kind`/pydantic/pipeline crashes → catch-all ERROR naming `--config` + source (19-25, 88); policy typo silently runs "standard" (`wiki/__main__.py:23` pattern).

## 3.8 `graph/workspace/` — mount scan, convert, parse, build orchestration

**`convert.py`** — D-top-10: `UnsupportedDocument` → note in convert.json + `continue`: **a document silently never publishes** (56-57); any other per-file failure → traceback string appended to a return list, zero log lines (58-60); permission-denied subdirectories dropped by `rglob` silently (26-31); E: `WIKI_PARSER_BASE_URL` missing raised per-file then swallowed (43) → one ERROR with affected file count; duplicate generated path collision aborts run (31). S: output mkdir+write **outside the try** (61) → one bad file aborts everything AND the state file is never written → every file re-parses next run; stat race (33); encoding chain (22-48: legacy Japanese encodings; 48 blames source when *previous output* is unreadable; corrupt state file (22) kills the run before the loop, JSON error names no file).

**`parser_client.py`** — E: the existing parser ERROR (288-89) has **no file name and no reason** → rewrite with file/endpoint/elapsed/type/msg; 415 logs one ERROR per endpoint instead of one per file; payload-shape rejections name no document (270-84); post-parse image-description + fence repair outside any try → good parse result thrown away by unnamed traceback (300-308). S: requests ConnectError/DNS/timeouts caught at 287 without file names — timeout must say "read timeout after Ns"; XLSM members + rebuilt workbook fully in RAM (192-203); preflight deps (msoffcrypto/olefile/pypdf) failures logged at **DEBUG only** (166-181).

**`project.py`** — E: two mounts writing the same output (45-48), raw path both file and dir (60-62), missing `target_name`/`source_mount` (171), mount missing (173-74) — raise-only. S: folder create, rglob walks, zip in BytesIO (146-186). A: 3 INFOs.

**`writer.py`** — the build orchestrator. D: embedder init swallowed (818-21) — missing API keys land here, truncated to 200 chars in a progress event; `ResumeUnavailable` → tier-3 full rebuild silently (692); corrupt up-to-date stamp `except (OSError, ValueError): return False` (884-902) → silent expensive rebuild; per-page patch failures stored in a dict only → document silently degrades to rebuild (266-75, 681). E: bad patch output discarded as retry feedback, only last error survives into RuntimeError (23-47, 254-56) → WARNING per retry + ERROR before raise; `KeyError: pages` no context (676); "legacy human state" raise without explanation (618). **S-publish window: `rmtree(target)` then copy (493-504) — a failure mid-way leaves the published wiki DELETED or half-published with only a traceback; the ERROR must state the output is incomplete.** S: page/stamp/index writes (278, 869-922), locked replace/unlink + rmtree hiding failures (704, 763, 877).

**`xlsm.py`** — D: broken workbook → "not a macro file" passthrough (31-36); bad formula → zero lineage deps, bare `except Exception: tokens=()` (41-43); anchors → "unknown" (82, 129-33). E: `oletools` missing → unlogged RuntimeError (139-42); malformed drawing XML / load_workbook internal message with no file name (99-137, 358). S: load_workbook OOM/EIO on disconnected share (358).

## 3.9 `index/` — legacy indexer CLI

`__init__.py:55` — failures stored in a Result field only; `__main__.py:22` — exit 1 with **no ERROR line at all** → ERROR per failure + "index run finished with N failures". S: `DataLayout.ensure`/mount mkdir on unmounted volume (41, 52 — no `parents=True`); one outer `except OSError` naming `--out` covers all writes (20). Config/`--out` missing → ERROR naming them (19-20). `growi_url=""` fallback built silently (42-47).

## 3.10 `jev/` — local LLM-judge engine (GPU models)

**`engine.py`** — D **repo-worst**: worker thread `_work` (191-219) has no outer handler → any unexpected exception kills the daemon thread **silently**; queue stops draining; every `decide()` blocks forever — a hang with zero log evidence. E: per-request failures counted in stats only (114-17, 228); batch give-up after silent per-state retry logs nothing (246-53); result-count mismatch raise-only. S: **CUDA OOM (230-45): counter bumped, budget shrunk, ZERO log lines at any level** — flagship system error, never logged; record.jsonl write mislabeled as scoring failure (98-100); worker thread start refused "can't start new thread" (84-85).
**`config.py`** — E: every bad `WIKI_JEV_*` value raises unlogged (75-87, 115-19 — one fix at `_int` covers all); Django-settings bypass (97-111); S: missing BASE_URL/API_KEY default to `""` (37, 38, 56, 58); missing backend silently defaults to `shisa` → wrong-backend runs with zero trace. A: resolved-config INFO (88).
**`backends/torch.py`** — E: model files/runtime/TEMPLATE_VERSION/tokenizer problems raise unlogged (32, 152, 155, 216); constructor TypeError → silent retry without args hides a real bug (161); D: local snapshot probe `except Exception: pass` (30) → EACCES/corrupt cache silently triggers re-download; torch import/bf16 probe `pass` (51-52) → broken install explodes much later. S: non-CUDA-OOM GPU failures (driver, device assert, pin_memory, 138-39, 203-06) collapse into generic wrapper; load OOM (160); HF download DNS/disk/401 (25-31). A: loaded-model summary (150).
**`backends/gguf.py`** — D: first `snapshot_download` error fully discarded before retry (28) — real cause gone from log AND traceback; constructor TypeError retry (38). E: incomplete snapshot / missing runtime script (31-34); input too long / runtime crash without state_id/question (49, 68). S: download+load (22-39) network/disk/PATH.
**`backends/shisa_jev.py`** — E: catch-all labels bugs AND network "request failed" (97-108) → split decide vs parse; **None answer accepted as string "None"** (111-33, 124) → bad data flows downstream with no error; missing env/package messages never logged (23-32); first-attempt network error dropped (101-05).
**`backends/hosted.py` / `llm2jev.py`** — E: ValueError without question/state identity (58-67, 126-63 / 41, 73-97); S: 5xx status + URL dropped from final message (102-15); malformed server confidence silently entropy-falls back (137). A: INFO on engine creation upstream (`jev/__init__.py:15-17` — can take minutes).
**`benchmark.py` / `parity.py` / `test_jev.py` / `types.py`** — benchmark cell dies = traceback no identity (28-33); torch ImportError → peak_vram 0 forever, "no GPU" vs "no torch" indistinguishable (44); fixture errors name no file/line/key (37-45); **parity FAILED (model regression) changes only the exit code — no log line** (48) → ERROR. Host OOM kills benchmark silently (33).

## 3.11 `linker/`, `wiki/`, `main.py` (thin top-level packages)

**`linker/__init__.py`** — D: missing source.json → `default={}` hides a broken `_planning` dir (52); `getattr(…, ()) or ()` → broken return contract = "0 touched" = looks like a legit empty run (83). S: rglob over missing wiki tree / EACCES (61, 65); sqlite locked/EACCES + old-schema raw OperationalError (95-97).
**`linker/__main__.py`, `wiki/__main__.py`** — A: keep result-JSON prints + INFO mirrors (21-27; 29). `wiki/__main__.py:23` D: `--set policy=<typo>` silently runs "standard". `wiki/__init__.py`: E: missing source path in FileNotFoundError (73-74); **plan.json missing/empty → build reports SUCCESS with zero pages** (106) → ERROR "build claimed success with no pages". S: mkdir/copy/move/run.json writes (75-105), strict utf-8 reads (102, 106).
**`main.py:39`** — D: compat wrapper mapping `wrappers.get(fn, fn)` — if `cli.py` renames a `cmd_*`, the wrapper silently stops applying (worth one assert or WARNING at import).
**`runner/steps.py`** — A: phase start INFOs (11, 23, 29). E: invalid phase value (21-22) → ERROR naming the bad value.

## 3.12 `publisher/` — the live publishing engine (the part company staff watch)

**`failure_logs.py`** — itself part of the logging machinery: S — log dir mkdir (79), failed_docs write (85), live.log open (84) and every Tee write (44-60) are unprotected: **disk-full during logging breaks the pipeline for a logging-only failure** → preserve healthy log destinations, with a short emergency notice if needed (2.1), never a console traceback. E: "failed to save error log" is an un-leveled stderr print (227-30) → ERROR through healthy logging or the emergency path. `_write_summary` currently embeds full traces: keep the grouped report but replace those with reasons and debug-log references (2.2). D: broken ledger JSON `continue` in alias building (172-73).

**`progress.py`** — A: phase lines (330) + final count (470-72) → logger.info. Implement the timestamped, single-line, traceback-free live formatter in 2.4; the reviewed baseline `JapaneseFormatter` does not suppress exception details. Leave tqdm drawing intact but remove the normal console log handler in bootstrap.

**`pipeline.py` (2,079 lines)** — the 3 repo-wide `log.error`s include one here (1083). E: 7 `log.exception` sites (643, 697, 720, 787, 1121, 1422, 1789) carry scope but **no reason text** → append `error=%s: %s`. E: scan/capture failures on non-isolated paths returned unlogged (857-60, 893-97); GROWI pull total failure → `fallback_reason="pull_exception"`, nothing logged (1718-21); lock-held/dirty-tree/missing-GROWI_URL raises unlogged (303-04, 847, 853, 864, 1172, 1998, 2062). S: `_write_raw` (212-21) re-raises without naming the path; rmtree(ignore_errors) marks ledger "deleted" while tree still on disk (760-61, 1774-75); unreadable linker.json → document invisible to publishing, reader sees "not ready" or nothing (205-06); malformed source.json → doc silently dropped from pending links (385-87); unreadable activity cursor → silent forced full inventory (472-84).

**`queue.py` (1,328 lines)** — E: operation/recovery ERRORs lack inline reasons (1148, 1159); index-reconcile failure stored in result only (1225-29); **watch-daemon scan/index errors reach only DEBUG (1281-82, 1315-16) → a permanently broken daemon logs nothing forever** → ERROR at the except; "cannot recover publication" RuntimeErrors traceback-only (625-74). S: sqlite connect/DDL ENOSPC/EACCES/"database is locked" (128-37); worker lock traceback-only (1236-42); D: `_docx_similarity` `return 0.0` on any exception → move-detection misses silently (190-92); resume capability `return None` silently (531-33); candidate/git-removal `continue` → **undeletable worktrees accumulate until the disk fills** (543-44, 583, 606-07); git rev-parse failures `continue` → resume restarts from scratch silently (574-83).

**`scanner.py`** — D: per-dir/per-file OSErrors collected into an `errors` dict that two of three callers (watch daemon, queue scan) never log → **unreadable mount = silently empty scan** (56-58, 70-71, 84-85); errors suppress delete-detection with no explanation (93) → WARNING with count; symlinks escaping the mount dropped silently (40-42, correct but invisible).

**`activity.py`** — logs NOTHING: corrupt cursor, unparsable timestamps, pagination/sequence gaps (missed events!), clock skew (NTP broken) all die as `fallback_reason` strings (83-133) → ERROR per fallback with path/reason; S: GROWI calls uncaught (94, 185, 214), poll loop degrades silently; inventory boundary raise-only (186-87); cursor commit write unprotected (180).

**`ahead.py`** — E: plan-ahead/link-ahead/Jev-roles disabled for the whole run (usually config/env) → one lone WARNING (187, 269, 278) → ERROR. S: worker-thread reads OUTSIDE the per-job try (79-117: read_blob, utf-8 reads, cache mkdir, temp dir, cache write) → thread dies with excepthook traceback only; **disk-full mislabeled as "the build will parse it"** (121-33); parser HTTP timeout mixed with real parse errors (125).

**`history.py`** — git/worktree manager. S: `_git` (37-49): git missing from PATH, nonzero exit (corrupt repo, `.git/index.lock`, ENOSPC) — git's stderr never logged; stat/read of source, `.gitignore` read/write, worktree mkdir, sqlite copy leaving orphan `.sqlite.tmp` (148-284); "database disk image is malformed" (287-91, 363-66) uncaught; prune failures `except CalledProcessError: pass` (297-373); `rmtree(ignore_errors=True)` → cleanup failures lost, leftover worktrees fill the disk over months (300-318, 516-19); unreadable candidates dir reported as "no candidates" (`except OSError: return []` ×3, 329-47). E: baseline refused / source-changed-while-staging raises unlogged (72-120, 230); **promote failed + rolled back — one of the biggest events of a run — logs nothing** (489-91); last-good ref missing → silent re-baseline (190-207); corrupt schema.json → `return False`, later failures blame the wrong thing (415-16). A: 4 milestone INFOs (174, 380, 435, 484).

**`human_changes.py`** (1,255 lines) — E: model merge gave-up after 2 attempts → DEBUG only (353-61) → ERROR + last reason; classify failure → guidance silently skipped, DEBUG (380-81); damaged footer / bad store schema / **snapshot checksum mismatch** / corrupt record / blocked document / ambiguous publication outcome → good messages, traceback only (670-800, 1194). S: every atomic write (677-953): 836 is the **live wiki page — disk and GROWI diverge on failure**; 863 final commit state; 922 capture loss = a human revision becomes unreplayable; strict utf-8 reads (735, 746-76, 1240) crash naming no path.

**`index.py`** — E: 5 index failure sites at WARNING (550, 567, 578, 596, 693-95) → **promote to ERROR** (severity: WARNINGs do appear in live.log at handler level INFO, but these
are failures, not warnings); stale-page cleanup failure appended to return value, zero logs (626-27); missing GROWI_URL / conflicting index locations raise unlogged (481, 496). S: manifest/coverage/chunks reads + index unlink (74-86, 157, 387); related-docs model call and GROWI deletes (189, 619-66) — 645/666 completely uncaught; D: catch-all (192-93) hides a cache **disk-write** failure behind the message "judge failed".

**`ledger.py`** — E: missing schema / invalid maps / corrupt JSON traceback-only (36, 38, 43) → ERROR naming file+problem. S: **the most important state write of the publisher** (54) — failure means duplicate/orphan pages next run; unprotected.
**`live_verification.py`** — E: boundary-not-confirmed / path-escape / bad report are safety events → ERROR before raise (60-170); failed live case stored in JSON only (148-70). S: report writes lose the whole test matrix on finalize failure (123-192); git rev-parse FileNotFoundError uncaught, nonzero → `commit=""` with stderr discarded (88-91).
**`phase.py:59`** — D: requested document has no generated state → `continue` → **document silently never assembled** → WARNING minimum.
**`publisher/__main__.py`** — A: 3 INFO mirrors (22, 28, 31); config file unreadable at entry → traceback only (26).

## 3.13 `runner/` — the CLI every command starts from

**`cli.py`** — the big one.
- E (biggest in batch): end-of-run failure list — `report_failure_summary` → print only on the normal path (`cli.py:174-178` + `failure_logs.py:234`); per-failure `log.error` exists only on the else-path (178). When the Tee goes away, **every run's failure list vanishes from live.log** → loop `log.error("document failed: %s", f)` + INFO summary.
- E: check DOWN printed only (260) → ERROR; compat-check subcommand crash collapsed to a 500-char JSON string, traceback destroyed (`compat_check.py:101-02`) → ERROR + traceback to debug.log; compat check FAILS → exit 1 with zero ERROR lines (107-08).
- S: **`_settings(args)` and logging bootstrap run BEFORE handlers exist (1241-49)** — missing INI/.env or unwritable log dir = console-only traceback, nothing in any file → guard bootstrap explicitly (2.1), use a short emergency reason without a traceback, and keep any healthy log destination. Route command failures through 2.4 with nonzero exit status; do not log then re-raise out of the CLI entry point and let Python print the same full traceback to stderr.
- D: git status failure → `return False` → **resume silently disabled, `--continue` restarts all work** (68-77 → cli.py:76-77); unreadable repair-checkpoint JSON → generic refusal, real cause never named (111-13).
- S: `--from-file` read (984-85); source-discovery ERROR (742) exists and is a good template — use its format everywhere.
- **`compat_check.py`**: temp dir + copytree of whole data tree ENOSPC (91, 93); read_bytes during hash (33, 104); config missing at entry (85) → ERROR naming the path.

---

# PART 4 — SYSTEM-LEVEL ERROR FIELD GUIDE (cross-cutting, complete lists)

"What could go wrong with the machine/environment, and where do we currently NOT log it?"
Eight families. Everything listed is **today: bare traceback or swallowed**, unless noted.

## 4.1 Disk full / read-only / permission denied (ENOSPC, EACCES, EROFS, EIO, EBUSY)
- **Choke points (fix these two functions first — they cover most of the repo):**
  `common/storage.py:_atomic_write` (34-45, every atomic write in the project) and
  `graph/wiki/storage.py` (104-108, every wiki artifact).
- Then the callers with special consequences: `workspace/convert.py:61` (state file never written → full re-parse next run); `workspace/writer.py:493-504` (**published wiki deleted or half-published mid-copy**); `publisher/human_changes.py:836` (live page vs GROWI divergence); `publisher/ledger.py:54` (duplicate/orphan pages next run); `publisher/failure_logs.py` (preserve healthy destinations after logging failure; short emergency notice, never a console traceback — 2.1); `graph/linker/service.py:898` (failure-marker write **replaces the original error**); `graph/wiki/windows.py:259` (disk-full **retried as if the model answered badly**); `graph/wiki/pipeline.py:562` (same mislabel on success write); `graph/linker/chunks.py:335-90` (optional artifacts abort a whole pass).
- Network-mount rename locks: `storage.py:42`, `writer.py:704-763`, `wiki/storage.py:108` → ERROR wording "(file locked, permission, or mount cannot rename-over)".
- Failed deletes hiding stale state: every `rmtree(ignore_errors=True)` — `incremental.py:382,455,471`; `history.py:300-318,516-19` (worktrees accumulate → disk fills over months); `pipeline.py:760,1774`; `writer.py:704,763`; `wiki/legacy.py:2680`.
- `clean_workdir` half-state: `common/storage.py:76-78`.
- EMFILE (too many open files): `wiki/storage.py:102`, `pipeline.py:1113` mkstemp/TemporaryDirectory under concurrent windows.

## 4.2 Missing file/dir (ENOENT, races)
- Name-the-path ERROR rule applies to: source reads (`pipeline.py:1854`, `writer.py:566-869`, `convert.py:33` stat race), planning reads (`linker/__main__.py:28-31-131`, `service.py:73-487`), resume artifacts (`pipeline.py:1195,1331`, `wiki/legacy.py`), `export.py:42-43`, `project.py:173-74` (mount missing — good message, never logged), `wiki/__init__.py:73-74`, `xlsx.py:311,347`, `convert/__init__.py:77-103`, `storage.py:64`.
- **The deceptive ones:** `ledger.py:33-35` (EACCES looks like first run → mass re-publish), `config.py:91` (EACCES INI → "config not found"), `common/policy`/`settings` variants. These need an explicit `os.access`/`PermissionError` distinction before concluding "missing".

## 4.3 Network / DNS / timeout (LLM endpoint, GROWI, parser service, Jev, HuggingFace)
- `graph/growi/client.py:114-127` (wrapper ERROR) + `:174 health()` swallow (evidence-zero).
- `graph/clients/chat.py:110,143` (+ retries a dead server 3×); `common/llm.py:37,48`.
- `graph/workspace/parser_client.py:258-69` (file name + "read timeout after Ns"); `publisher/ahead.py:125`.
- `graph/wiki/model.py:118-19` + every retry loop in wiki/fast: **ERROR must include `type(exc).__name__`** so "endpoint dead" ≠ "model answered wrongly".
- Jev: `graph/fast/inline_linker.py:639-41,1133-35` (total outage = zero lines); `jev/backends/*` (URL lost from final message, first attempt dropped).
- HuggingFace model download: `torch.py:25-31`, `gguf.py:22-30,28` (DNS/refused/401/timeout; first error discarded before retry).
- `publisher/activity.py:94,185,214`; `publisher/index.py:189,619-66`; `publisher/pipeline.py:1490-1505`.

## 4.4 Subprocess crashes / wrong exit (git, mmdc)
- `publisher/history.py:_git` 37-49 (git missing, `.git/index.lock`, ENOSPC, corrupt repo — stderr must be logged); `wiki/incremental.py:82-92`; `live_verification.py:88-91` (exit → `commit=""`, stderr discarded); `runner/cli.py:68-77` (git status fail → resume silently off); `publisher/queue.py:574-83`; `graph/config.py:1461-72` (mmdc missing/timeout/nonzero → tuples nobody logs → all diagrams silently skipped).

## 4.5 Out of memory / thread & file limits / GPU
- `jev/engine.py:191-219` **silent worker-thread death → infinite hang** (add try/except → CRITICAL + resolve pending futures with the error).
- `jev/engine.py:230-45` CUDA OOM zero logs; `torch.py:138-39,160-61,203-06`; `gguf.py:37-39`.
- `thread.start()` refusal: `common/async_tools.py:25`, `graph/common/async_tools.py:26`, `jev/engine.py:84-85`.
- Unbounded RAM reads (OOM-kill without log — mitigation = start-of-run INFO with path + byte size): `pipeline.py:1854-55`, `parser_client.py:192-203`, `xlsm.py:358`, `project.py:179-86`, `graph/common/images.py:92-93`, `chat.py:98-101` (max_tokens disabled), big prompts (`page.py:245-62`, `prompts.py:497,531,596`).
- Hangs: `wiki/legacy.py:2852-75` daemon thread dies at interpreter shutdown → confusing KeyError; `pipeline.py:1110,1138` planner-leader wait with no timeout.

## 4.6 Bad environment: missing `.env`, env vars, config files, packages
- `config.py:22` load_dotenv silence; `runner/cli.py:1241-49` bootstrap-before-logging; config path never named at any CLI entry (`compat_check.py:85`, `wiki/__main__.py:22`, `linker/__main__.py:23`, `publisher/__main__.py:26`, `index/__main__.py:19-20`, `common/settings.py:59`).
- Missing required settings: `WIKI_PARSER_BASE_URL` (`convert.py:43`), `GROWI_URL` (`pipeline.py:853,1998,2062`, `index.py:481`), `GROWI_API_TOKEN` (surfaces only as 401), `WIKI_JEV_*` (`jev/config.py:37-58`; backend defaults to `shisa` silently), missing API keys landing in embedder-swallow (`linker/__main__.py:49-50`, `writer.py:818-21`), `model.py:69-75` construction.
- Invalid numeric settings that DISABLE protections: timeout→None (`chat.py:86-89`, `wiki/legacy.py:1016-17`); ~52 env casts (`config.py:339-585`); jev `_int` (115-19).
- Missing optional packages: oletools (`xlsm.py:139-42`), msoffcrypto/olefile/pypdf (`parser_client.py:166-81`, DEBUG-only), sqlite_vec (`catalog.py:45-49`), torch (`benchmark.py:44`), lazy `ModuleNotFoundError` mid-phase (`common/legacy.py` ×15 imports, `llm.py:32`, `policy.py:49,147`).

## 4.7 Corrupt JSON / SQLite on disk (file exists but broken)
- The systemic problem splits into **three distinct mechanisms** (verified against
  `common/storage.py:60-66` / `graph/wiki/storage.py:128`):
  (a) `exists()` is False on **EACCES**, so `read_json(default=…)` returns the default on a
  *permission* error — a lockout looks exactly like "first run": `incremental.py:261,377,412,458,480`;
  `writer.py:884-902`; `wiki/storage.py:129-140`; `linker/__init__.py:52`.
  (b) **corrupt** JSON raises `JSONDecodeError` **uncaught** (default does NOT cover it) — kills
  the run with a message naming no file: `inline_linker.py:670,797,979`; `wiki.py:315`; `repair.py:1125`.
  (c) `except (OSError, ValueError): return/fallback` handlers that deliberately treat corrupt as
  absent → silent full rebuilds/re-plans: `pipeline.py:934,997,1215` (also swallows OSError → disk
  errors hide); `windows.py:216-226`; `xlsx.py:304-08`; `tabular.py:553`; `repair.py:1295,1301-05`;
  `meta_cache.py:34-37`; `mokuji_data.py:106-10`; `inline_linker.py:491-92`.
  Fix rule: (a) open the file and branch on errno instead of trusting `exists()`; (b) helper wraps
  `json.loads` and logs "<file> is corrupt (not missing): <err>"; (c) each swallow logs its own
  reason before falling back.
- Raise-only corrupt: `convert.py:22`, `convert/__init__.py:102`, `ledger.py:35`, `publisher/ledger.py:43`, `activity.py:83`, `history.py:415-16,447`, `cli.py:111-13`, `pipeline.py:203,385,1764`, `human_changes.py:713,791,930-34`, `index.py:74-76,157`, `parity.py:37`, fast/* manifest reads (3.4).
- SQLite: "file is not a database", SQLITE_FULL/IOERR, "database is locked" after 30 s: `catalog.py:34,79-121`, `queue.py:128-37`, `linker/__init__.py:95-97`, `history.py:287-91,363-66`; flock forever-block: `catalog.py:142-150`; publisher/queue locks raise RuntimeErrors traceback-only (`pipeline.py:299-306`, `queue.py:1236-42`).

## 4.8 Encoding (UnicodeDecodeError on Japanese mounts)
- ~40 `read_text(encoding="utf-8")` sites crash naming no file: all listed in 3.4-3.8 digests (`pipeline.py:1855`, `inline_linker.py` ×5, `repair.py:1204-1591`, `tabular.py:604-610`, `human_changes.py:735-1240`, `ahead.py:92-117,244`, `index.py:86`, `wiki/__init__.py:102,106`, `config.py:91`, `convert.py:22-48`, `parser_client.py:133-49`, `catalog.py:172`, `chunks.py:273-79`, `service.py:85-291`, `client.py:863`, `ledger.py:35`, `storage.py:66-71` …).
- Practical fix: catch `UnicodeDecodeError` in the shared read helpers (storage/wiki-storage) + ERROR naming the path; convert cp932 explicitly where legacy encodings are expected; fix `convert.py:48` mis-blaming and `parser_client.py:144` unlogged blob.

---

# PART 5 — SILENTLY SWALLOWED ERRORS — TOP 15 (do these first)

An error happens; nothing is recorded anywhere. These are ERROR-coverage gaps AND data-quality bugs:

1. `jev/engine.py:191-219` — Jev worker thread dies silently → every `decide()` blocks forever. Hang with zero evidence.
2. `graph/linker/legacy.py:96-97` + `neo.py:159-166` — `except Exception: continue` → link quality silently deleted.
3. `graph/wiki/pipeline.py:934/997/1215` — resume-cache swallows (incl. OSError) → disk problems disguised as model problems; hours of silent re-planning.
4. `graph/fast/writer.py:234-41/381` + `graph/wiki/pipeline.py:1490-96` — pages silently published as raw source dumps; **a broken API key looks identical to success**.
5. `common/mokuji_data.py:106-110` — broken records dropped → wiki search data quietly wrong forever.
6. `common/ledger.py:33-34` + `workspace/writer.py:884-902` — permission/corrupt mistaken for "first run" → mass re-publish / full rebuilds.
7. `graph/growi/client.py:174` — `health()` swallows all evidence of GROWI being unreachable.
8. `graph/config.py:1504-05` — `except Exception: break`: LLM outage == "model couldn't fix the diagram".
9. `graph/fast/inline_linker.py:1169-71` — reset-undo failures unlogged → **fake/stale links stay on published wiki pages**.
10. `publisher/scanner.py:56-93` — unreadable mount = silently empty scan (2 of 3 callers never log the errors dict).
11. `graph/workspace/convert.py:56-60` — "unsupported document" + any convert failure → zero log lines → document silently never publishes.
12. `graph/linker/catalog.py:45-49/66-69/351-54` — sqlite_vec missing (50× slowdown), corrupt DB ("no mode"), embedding batches failing forever — all unlogged.
13. `graph/linker/__main__.py:49-50` + `workspace/writer.py:818-21` — embedder init swallowed → vector search silently off; API-key errors die here.
14. `publisher/queue.py:1281-82/1315-16` — watch daemon errors reach only DEBUG → a permanently broken daemon is silent forever.
15. `graph/wiki/model.py` (whole file) + `graph/fast/linker.py:90-96` (bare return) + `graph/formats/context.py:67-68` + `graph/linker/chunks.py:236-40` (`return ChunkMeta()`) + `publisher/phase.py:59` (`continue`) + `publisher/index.py:626-27` (return-value-only) + `runner/cli.py:76-77` (resume silently disabled) + `publisher/history.py:190-207/329-47` + `graph/wiki/legacy.py:1083/1912-41`.

---

# PART 6 — ROLLOUT (final scope: everything through the logging library)

Verify after every step: cause one failure and check the ERROR line is in
`logs/<project>/live.log`; check rotation (shrink `maxBytes` in a test config and watch it roll);
check full tracebacks appear in debug only, and `live.log` contains neither traceback nor tqdm
junk. The concrete acceptance checklist in 6.1 is required before calling this implemented.

1. **The core swap** (one focused commit): delete `_Tee`, slim down `capture_run_logs()` to folder +
   `failed_docs.md` setup, install the two `RotatingFileHandler`s in the logging bootstrap
   (live INFO 1 MB / debug DEBUG 10 MB, **`backupCount=99` for both**), add the traceback-free
   live formatter in 2.4, and remove the normal console log handler. Point `publisher/progress.py`'s
   transcript writes at `log.info`. Keep root DEBUG, register each file handler once, and close
   both on exit. Resolve single-writer ownership before opening files (2.1). Existing records
   meeting the live threshold now reach live directly; prints do not, which steps 2-3 fix.
2. **Print→INFO conversions** — the Part 2.3 table + the Part 3 "A" milestone items in the files
   that already log; engine callbacks get milestone INFOs (per-attempt detail stays DEBUG →
   `debug.log` only).
3. **Failure visibility** (manager point 3): end-of-run failure list as `log.error` records
   (`runner/cli.py:174-178`, `publisher/failure_logs.py:230,234`), check-DOWN ERROR, publish/
   index summaries ERROR when counts > 0. Keep grouped `failed_docs.md` summaries, replacing
   embedded full traces with reasons and debug-log references. Keep exception information on
   logged records so debug retains it. Cover CLI/thread/async failure reporting without leaking
   full traces to stderr or changing failure exits (2.4).
4. **System choke points** (coverage measured — see 7.4): `_atomic_write()` in `common/storage.py`
   **and its copy in `graph/wiki/storage.py:99`** (both needed; together they cover the atomic-write
   calls of 28 caller files, not the ~120 direct write/read sites), and the request wrapper in
   `graph/growi/client.py:_request` (covers **all GROWI HTTP** errors incl. 4xx/5xx/409 — but NOT
   the 7 files with direct httpx calls: parser_client, hosted, llm2jev, inline_linker, jev_judge,
   runner/cli, which keep their own ERROR lines).
5. **Swallow surgery** (Part 5, top 15): one contextual ERROR at the appropriate reporting
   boundary before each retained failure `continue`/`return`/fallback; include `exc_info=True`
   when reporting a caught exception. Do not duplicate reports already owned by a caller/helper.
6. **ERROR-only sweep of the 81 zero-logging files** (Part 0 list): E/S sites from Parts 3-4, one
   line each + logger header; recurring messages get `JapaneseFormatter._MESSAGES` templates so
   live.log stays clean Japanese. Their CLI entry points also get the step-2 print treatment.
7. **Existing-log files**: promote failure-hiding WARNING/DEBUG sites (Part 3) to ERROR; append
   `error=<Type>: <msg>` to reasonless `log.exception` messages (`publisher/pipeline.py` ×7,
   `publisher/queue.py` ×2, …).
8. **Log-failure hardening**: guard startup/summary writes and flush/close in addition to the
   emit-time `handleError` override (2.1). Disable only the failed destination, with one short
   best-effort emergency notice; no recursive logger call and no stderr traceback. Logging-only
   failures must not kill publishing or mask actual publication failures.

## 6.1 Required acceptance tests — implementation is not complete without these

Run on the project's supported Python runtime (currently Python 3.13), in temporary test
directories, using simulated failures rather than filling the real disk. These are tests for
the implementor to add/run, **not a claim that the proposed implementation already passes**.

1. **Traceback separation**: raise a real exception, log its type/reason/context with exception
   information, and assert live has one timestamped line while debug has the full traceback.
   Repeat with live-before-debug and debug-before-live, a pre-cached `exc_text`, exception
   chaining, `stack_info=True`, and multiline messages. The live formatter must not alter the
   original record's exception fields (the normal debug formatter may populate `exc_text`).
   Confirm no full trace in captured stdout/stderr or the generated `failed_docs.md`; verify
   the same under `-v`. Check CLI and uncaught background-exception reporting paths too.
2. **Output shape and levels**: DEBUG appears only in debug; INFO/WARNING/ERROR/CRITICAL reach
   both appropriate files. Live preserves Japanese, timestamp/timezone, run ID, useful context,
   and error reason, with no ANSI escapes, tqdm carriage returns, full tracebacks, or secrets.
   A single reporting boundary must not produce duplicate ERROR records.
3. **Both rotations and retention**: lower `maxBytes` in tests but keep `backupCount=99`;
   force more than 100 rollovers for each handler. Assert the active file and exactly 99
   archives remain, `.1` is newest, `.99` is oldest, and oldest records are pruned. Also assert
   production defaults are 1 MB live / 10 MB debug, both 99 backups. No `.100` or timestamp
   naming shortcut; do not confuse "100 files" with "100 backups".
4. **Restart preservation**: close/reopen both handlers and write a second run. Existing
   contents remain until size rotation naturally removes them; run-start/finish records let
   readers distinguish invocations. No `mode="w"` path or startup archive wipe remains.
5. **UTF-8 and large records**: rotate Japanese messages and long debug tracebacks; records
   stay readable and exception chains are not split/truncated. A record larger than the
   threshold may make a larger file: test and document this, not a strict size-cap assertion.
6. **Failure hardening**: inject directory/open/summary-reset, write, rollover-rename,
   flush/close, and broken-stderr failures. The failing destination is disabled and receives
   no repeated write attempts; a healthy destination still works. Only one concise emergency
   notice per failed destination, no recursive logging/default stderr stack dump, and no
   publishing crash caused solely by logging. Actual data-operation failures still fail.
7. **Concurrency and cleanup**: exercise multiple threads plus two overlapping CLI processes
   for the same project. Prove the chosen single-writer/ownership policy prevents conflicting
   rotations without breaking required command behavior. Verify no duplicate handlers or
   leaked open files after repeat initialization and shutdown.
8. **Progress and CLI contracts**: tqdm still works on the terminal, required JSON stdout is
   still machine-readable, live contains no progress redraws, and failed commands keep nonzero
   exit codes. `failed_docs.md` keeps its grouped, redacted summary and debug reference.

---


---

# PART 7 — ROUND 2: FULL RE-AUDIT + NEW FINDINGS (verified)

Round 2 was quality-first; round 2 re-audited all 21 batches demanding **complete accounting**:
every worker re-read each file, grepped it for every `except` / `raise` / file-IO / subprocess /
thread / network / json-read / `default=` / print / callback site, and assigned each grep line a
category (already-correct / needs-ERROR / needs-INFO / irrelevant-with-reason) in a per-file
coverage table. Reports: `logs/audit-notes/batch-01-round2.md` … `batch-21-round2.md`. The
accounting is thorough but **not a proof of completeness** — tagging style varies by batch and
counts are not exactly reconcilable (7.0); individual claims verified well (7.0), three did not
survive review (7.1 #4/#7, 7.1 #13 example).

## 7.0 Independent verification of round 2 (parent, after "is it lying?" challenge)

Machine check of every reference in the 21 round-2 reports:
- **460/460 backtick-quoted `file.py:LINE` references resolve to a real, in-range line** (0 misses,
  0 missing files). Broader scan of loose numeric mentions: unresolvable cases turned out to be
  this checker's own basename collisions (`__main__.py`, `prompts.py` exist in several packages).
- Semantic spot-audit: 18 random "claim vs actual line" pairs — **17/18 exactly right** (one
  citation off by ~4 lines), including subtle ones (`storage.py:77` `exists()`-lies-on-EACCES,
  `convert/__init__.py:96` inline-conditional EACCES, `client.py:1398` except-tuple gap).
- Runtime check of round 2's *correction* to round 1: `read_json(default=…)` truly returns the
  default only for **missing** files — **corrupt JSON raises** (`common/storage.py:60-66`).

**Counts — retracted and reconciled.** Round-2 reports contain ≈1,700 table/bullet rows; ≈166 are
coverage-accounting (grep math, not findings) and ≈1,200 are finding rows. Every automatic
headcount I produced (885, 619, 1,717, ≈590, "~150 new") **disagreed with the next one because the
batches do not tag consistently** — some tables carry a `[NEW]`/`[R1]` status column, others have
no status cell at all (~500 finding rows are untagged). Therefore: **all headline counts in this
document are retracted.** What is trustworthy is per-claim: a quoted reference resolves, and the
claim itself must be re-read against the code before it enters the change list (several did not —
see 7.1 corrections). The **only** operative to-do list is Part 6 + the coverage map in 7.4, which
states per change exactly what it covers. No "exhaustive coverage" guarantee is claimed beyond
what 7.4 enumerates; Parts 3-4 remain "thorough for these 105 files", not provably complete.

## 7.1 New worst findings (things round 1 missed)

Classification legend — every finding gets exactly one class:
**A** = confirmed missing ERROR log (the failure happens, is handled/propagates, but is invisible
in live.log); **B** = program bug (logging alone does not fix it — fix the code, then log);
**C** = optional hardening (no reachable failure shown); **D** = normal-by-design (no action).
Items re-checked against callers and existing guards after review; three were wrong or overstated
(4, 7, 13) and are corrected here.

1. **B** `common/paths.py:100-103` — `raw_file`/`document` join caller `raw_rel` with no check:
   an absolute path or `../` escapes the data root (parent-verified: `Path / "/abs"` replaces).
   Reject non-relative rels (code fix); the rejection path gets the ERROR.
2. **B** `common/mokuji_data.py:118` — a header record **missing its hash gets it silently
   recomputed**, defeating the hash-based corruption detection. Code fix: treat missing hash as
   integrity failure; ERROR "mokuji header has no hash; integrity check disabled for this block".
3. **A** `graph/fast/writer.py:186` — `model is None` (missing config/API key) makes **every page
   a verbatim source dump**, zero logs, zero callbacks. ERROR once per run.
4. **C (withdrawn as bug)** `graph/linker/jev_judge.py:216-217` — round 2 claimed `zip(screen,
   screened)` silently truncates if the engine returns fewer results. **No reachable path**: the
   engine rejects count mismatches (`jev/engine.py:229` raises `JevUnavailable("backend returned
   misaligned results")`) and `adecide_batch`/`decide_batch` construct exactly one output per
   submitted future (`jev/engine.py:165-177`, verified line-by-line). An assert on equal lengths
   is *optional* defense-in-depth, not a fix.
5. **A** `common/context.py:55-56` — a crash inside the emit sink's *else* branch turns a **successful
   stage into a failure** and drops its done event.
6. `common/legacy.py:27-35` (E) — `convert_mount`, a whole network-parse adapter, was invisible to
   round 1 entirely; its failures reach nothing.
7. **D (withdrawn)** `common/async_tools.py:29` — round 2 claimed `result[0] if result else None`
   "discards a completed result when a teardown error fires". **Wrong as written** (parent
   re-read `common/async_tools.py:18-29`): `result.append(asyncio.run(…))` runs *only after*
   `asyncio.run` returns — if it raises during teardown the append never happens, the exception
   goes into `error[]`, and lines 27-28 re-raise it. `error` empty + `result` empty is
   unreachable. No action; the only real gap is generic (re-raised exception never logged), which
   is step-5 work at the caller that aborts the run.
8. **A** `graph/config.py:1470-1472` (S) — mmdc **OOM-killed** (negative returncode, empty stderr) yields
   an *empty* error string fed to the repair LLM: a machine failure mislabeled as bad syntax,
   never logged.
9. **B** `graph/config.py:413/442/455/459 vs 387/396/483/548` (E) — two different case-sensitive boolean
   env parsers: a typo like `FALSE` silently flips feature flags (`enable_mermaid` ON, linker OFF),
   no exception, no log (parent-verified: two parser styles do coexist). Fix = one parser + one
   INFO of resolved flags at start.
10. **A** `graph/fast/inline_linker.py:670/797` (S) — corrupt decision-cache JSON raises uncaught
    despite `default={}` (default only covers *missing* files — parent-verified against
    `common/storage.py`), killing the run with a nameless traceback.
11. **B** `graph/fast/linker.py:95+113` (S) — error-artifact write *inside* the except handler can fail
    (ENOSPC) and mask the original LLM error; a plain `asyncio.gather` then leaves sibling tasks
    running detached with unretrieved exceptions.
12. **A** `graph/growi/client.py:1308/1368` (E) — merge/capture model-call failures are outside the
    `except (OSError, KeyError, TypeError, ValueError)` tuple at 1398: one LLM outage aborts the
    whole pull with a raw traceback and skips all remaining pages.
13. **A (corrected)** `graph/growi/client.py:318` (S) — `update_page` response `.json()` can throw
    *after the remote write succeeded*, leaving the ledger "prepared-unconfirmed" with zero log
    output. Round 2's example (502 HTML page) was **wrong**: `_request` raises `GrowiAPIError`
    for every 4xx/5xx *before returning* (verified `client.py:127-129`), so a 502 never reaches
    `.json()`. The reachable scenario is a **2xx response with a non-JSON body or drifted payload
    shape** (misbehaving proxy/CDN, GROWI version mismatch) — rarer, so this is downgraded from
    "worst": one ERROR around the post-write parse naming page_id.
14. **A** `graph/linker/catalog.py:65-69` (S) — `DatabaseError` from `stored_mode` escapes its own except
    clause; `restore_edges` (:447) crashes on corrupt links.json with no path named.
15. **B** `graph/linker/chunks.py:344-353` (S) — mid-loop abort (artifact write ENOSPC) orphans in-flight
    asyncio tasks and destroys **all** collected model-call errors.
16. **B** `graph/wiki/incremental.py:82` (S) — `subprocess.run(git …)` has **no timeout**: a hung git
    freezes the whole run silently forever. Fix = `timeout=` (code fix), not a log line.
17. **A** `graph/wiki/legacy.py:2869` (S) — `thread.start()` refusal ("can't start new thread", fd/
    thread-limit) — two-line traceback naming nothing about the pipeline.
18. **B+A** `common/storage.py:38,62,77` (S) — fd leak if `os.fdopen` fails (**B**); `exists()` False
    on EACCES makes `read_json` return the default on **permission errors** (**A**, verified);
    `clean_workdir` rmtree partial-delete state (**A**).
19. **A** `graph/clients/chat.py:146-148` (E) — the fallback loop swallows `TimeoutError` and re-sends,
    violating the file's own no-retry-on-timeout policy, unlogged; missing `request_timeout`
    attribute silently disables the hard timeout (85).
20. **A** `wiki/__init__/_settings path (batch-21)` (E) — with no `--config`, settings are *fabricated*
    (`chat_base_url=""`, `chat_api_key="local"`): the real cause ("no config loaded") never appears;
    failure shows up much later inside the LLM client. One startup INFO naming the config source.

Also new systemic notes: pptx/tree divisor loops infinite-loop (silent 100% CPU hang) when target
lines ≤ 0 (**B**, code fix); `catalog` `insert_edge`-returns-False edge drops; `test_jev.py` barrier
hang (test-only); several "import-seam" sites where a lazy import failing mid-run is unlabeled.

## 7.2 Round-1 corrections (already merged into this document)

- `graph/linker/legacy.py` swallow is at **96-97**, not 98-99 (Parts 3.6/5 fixed); guard at 88.
- `graph/config.py` env casts: **~75**, not ~52 (whitespace-collapsed count).
- `graph/growi/client.py`: ledger writes at 923/930/965/980/987 (was 936-989); raise at 129;
  read_json at 1207.
- `common/ledger.py:35` merged three distinct failure classes (missing/corrupt/version) — split.
- `common/storage.py`: `exists()`-swallow also at 62 and 77, not only 71.
- `graph/wiki/pipeline.py:261`-class correction: `read_json(default={})` does **not** hide corrupt
  JSON — corrupt raises uncaught; *missing* silently defaults. Both need their own ERROR/INFO;
  this changes the fix wording in several Part 3 entries ("corrupt looks like missing" was wrong —
  "missing looks like first run" is the real trap).
- ~60 line-number renumbers across repair.py/client.py/formats (round-2 files list each).
- **Corrections TO round 2** (parent review after this doc shipped): jev_judge zip-truncation
  withdrawn (engine guards count at `engine.py:229`, one output per future at :165-177); the
  `async_tools.py:29` "discarded result" scenario is unreachable (`append` runs only after
  `asyncio.run` returns; the exception path re-raises at :27-28); the `update_page` "502 HTML
  reaches .json()" example is wrong (`_request` raises on all 4xx/5xx at :127-129) — true case
  is 2xx + non-JSON body. All three reflected in 7.1.

## 7.3 Effect on the plan

Parts 3-4 remain the work list; Part 7.1 items map onto the Part 6 rollout: #1/#2/#3/#17/#20 →
step 6 sweep; #5, #12, #14, #19 → step 5 swallows; #13 → step-4 client wrapper scope; #8 → config
step. **No longer in the logging plan**: #4 (engine already guards it), #7 (unreachable path).
**Moved to a separate bug list (class B — code fixes, not log lines)**: #1 path-escape reject,
#2 hash policy, #9 one bool parser, #11 artifact-write-in-except + detached tasks, #15 orphaned
tasks, #16 `timeout=` on git, infinite-loop divisors, fd leak. Nothing changes the design; class-A
items are still just "one `log.error` line with the reason".

## 7.4 Coverage map: each planned change → the sites it actually handles (measured, not assumed)

| Change (handler function) | Really covers | Does NOT cover — separate work |
|---|---|---|
| `common/storage.py::_atomic_write` +ERROR | atomic writes of the 5 files importing `common.storage` (common/*, parts of publisher) | `graph/wiki/*` (uses its own copy); ~120 direct `write_text`/`open` sites |
| `graph/wiki/storage.py::_atomic_write` +ERROR (2nd implementation, :99) | the 23 files of the wiki/graph pipeline | everything outside it |
| `common/storage.read_json` / `graph/wiki/storage.read_json` +ERROR | json reads going through the two helpers | direct `json.loads(x.read_text())` sites — e.g. `publisher/human_changes.py:735` (`_read` does its own `glob`+`read_text`), `failure_logs.py` open() writes — these keep individual try/ERROR |
| `graph/growi/client.py::_request` +ERROR (verified single exit for status codes, :127-129) | **every GROWI HTTP error** incl. 4xx/5xx/timeout | direct httpx in 7 other files (parser_client, hosted, llm2jev, inline_linker, jev_judge, runner/cli); post-2xx body-parse failures (`update_page` :318, 7.1 #13) need their own ERROR |
| `publisher/history.py::_git` +ERROR(git stderr) | publisher git/worktree operations | `graph/wiki/incremental.py:82` own subprocess; `common/git.py`; queue/cli/live_verification git calls |
| Part 5 swallow surgery (per-site) | the 15 listed swallow blocks | — each is its own `log.error` before `continue`/`return` |
| 81-file ERROR-only sweep (per-site) | E/S sites in zero-logging files, incl. the ~120 direct-IO sites | sites in already-logging files (step 7 territory) |

Honest limits of this map: helper-caller counts are grep-measured (import counts above); the
~120 direct-IO figure counts `read_text|write_text|open(` occurrences outside the two storage
modules (includes reads inside already-covered functions' callers being double-counted at file
edges). It bounds the work — it is not a proof of completeness; see 7.0.

*Sources: round 1 — 21 full-file audit reports `logs/audit-notes/batch-01.md`…`batch-21.md`
(4,700 lines); digests `digest-1..3.md`. Round 2 (full re-audit, grep-accounted, verified per
7.0) — `logs/audit-notes/batch-01-round2.md`…`batch-21-round2.md` (≈4,900 lines). Direct parent
verification of `runner/cli.py`, `publisher/failure_logs.py`, `publisher/progress.py`,
`common/storage.py`, `common/paths.py`, `common/mokuji_data.py`, `jev/engine.py`,
`graph/linker/legacy.py`, `graph/linker/jev_judge.py`, `graph/fast/writer.py`,
`graph/clients/chat.py`, `graph/config.py`.*
