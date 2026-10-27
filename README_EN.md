# Growi LLM Wiki

## 1. Overview

Point this tool at a folder of documents (NFS or bind mount) and an AI rewrites them into a Markdown wiki, published into a single project-named folder in **GROWI**. Reading and editing happen in GROWI; the generated pages are managed by this tool.

One project = one file in `configs/`, and every operation runs through `main.py`. For design and internal details, see [`DEV_README.md`](DEV_README.md).

---

## 2. Where Things Live

| Location                       | Contents                                                              |
| ------------------------------ | --------------------------------------------------------------------- |
| Document folder (mount source) | The original documents. Never modified or copied                      |
| `<data_root>/<name>/`        | Local working data (converted text, generated pages, publish state)   |
| GROWI`/<name>/`              | The published wiki (numbered pages,`00-目次`, cross-document links) |

---

## 3. Prerequisites

- Linux with the document folder mounted readably (`mount --bind`, NFS, SMB)
- [uv](https://docs.astral.sh/uv/) (uv provides Python 3.13 for you)
- Network access to the LLM, doc-parser and GROWI endpoints

---

## 4. Initial Setup

### 1. Check that uv is available

```bash
uv --version
```

If it is not installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

### 2. Get the repository

```bash
git clone <repository-url>
cd llm-wiki
```

---

### 3. Create the Python environment

```bash
uv sync
```

This creates `.venv/` with the dependencies and Python 3.13. From now on, run every command from the repository root in this form (`uv run python main.py …` is equivalent):

```bash
.venv/bin/python main.py <command> --project <name>
```

---

### 4. .env works as shipped

The LLM, doc-parser and GROWI URLs and tokens are already configured in the `.env` file committed with the repository. **You do not need to edit it for a first run.** Only when you must change an endpoint or the generated-page language, edit `.env` following section 9 "Reference: Environment Variables".

---

### 5. Check the connections

```bash
.venv/bin/python main.py check --project test
```

Each connection is probed individually and reported like this:

```
chat    200 http://<llm-host>/v1/models
embed   200 http://<embed-host>/v1/models
parser  200 http://<parser-host>/agent/doc-parser/health
growi   200 http://<growi-host>:3000/_api/v3/healthcheck
jev     shisa backend (local start not checked)
data    /path/to/llm-wiki/data/test  mount=/path/to/llm-wiki/data/test/mount ingest=wiki linker=neo
```

Each line is one connection:

- `chat` / `embed` … the LLM and the embedding model. Nothing generates while `chat` is down
- `parser` … the document conversion service. `skipped` is fine when you only convert `.md` / `.txt`
- `growi` … the GROWI instance you publish to
- `jev` … the link-decision model. A `skipped` or `backend` line is normal; it only affects how well documents get linked
- `data` … the working folder that will be used and the mount folder that will be read. Check that `mount=` is the folder you expect

For any line showing `DOWN`, check that the service is running and that the URL in `.env` is right. `check` only reads and never changes GROWI pages, so it is safe to run as often as you like.

---

## 5. First Run: The Test Project

`configs/test.ini` is a ready-to-run test project (`--project test`).

- Document folder: `data/test/mount/` (a few sample PDFs are committed with it, so no mounting is needed)
- Publish target: the `/test` folder on the test GROWI already configured in `configs/test.ini`

```bash
.venv/bin/python main.py -v sync --project test
```

`-v` prints per-document progress. Repeated `waiting` lines every 10 seconds during a large-file conversion mean the conversion is in progress, not that it failed. A test-sized run finishes in a few minutes.

`sync` is the main command of this tool and works in this order:

1. Walks `source_mount` and collects only new or changed documents (unchanged ones are skipped)
2. Converts documents to Markdown with doc-parser (`.md` / `.txt` are used as-is)
3. The AI plans the document structure, then writes the wiki page by page
4. Adds cross-document links
5. Publishes to GROWI

Work in progress also stays on disk: `data/test/raw/` holds the converted text and `data/test/wiki/` the generated pages.

When it completes, open `http://<growi-host>:3000/test` in GROWI and check the `00-目次` page and the generated pages.

Running the same command again finishes in tens of seconds because nothing changed.

---

### Resume after a failure or interruption: `--continue`

```bash
.venv/bin/python main.py -v sync --project test --continue
```

If a run stopped partway — Ctrl-C, a network drop, an LLM or conversion-service outage, a machine reboot — rerun the same command with `--continue`.

- Published documents are not regenerated and pages already sent are not sent again; only the document that was half-built restarts from conversion.
- `--continue` reuses the analysis already paid for — entities extracted for linking and the Jev decisions — and processes only what is still pending. When in doubt, rerun with `-v --continue`. Details: `--continue` under section 8.

---

### Cleaning up the test

```bash
.venv/bin/python main.py reset --project test
```

This deletes the `/test` pages this tool published and resets the local publish state. Pages created manually in GROWI are not deleted.

---

## 6. New Project Setup

### 1. Mount the document folder

```bash
sudo mkdir -p /home/seigyo/mnt/newproj
sudo mount --bind /path/to/documents /home/seigyo/mnt/newproj
findmnt --target /home/seigyo/mnt/newproj
```

What the three commands do (the same reasoning applies to NFS / SMB mounts):

- `mkdir` … creates the empty folder to mount (the path you will write as `source_mount` in the INI)
- `mount --bind` … attaches the folder holding your documents onto that empty folder
- `findmnt --target` … confirms the attach worked; a printed line means success

The tool only reads the mount; it never modifies or copies the original files, so a read-only mount (`sudo mount -o bind,ro /path/to/documents /home/seigyo/mnt/newproj`) is fine.

---

### 2. Create the project file

Create one `configs/<name>.ini`. **The filename becomes the `--project` name.** Read the real `configs/projectF.ini` line by line.

```ini
[project]
source_mount = /home/seigyo/mnt/projectF
target_name = 中国配自SIM
data_root = data

[settings]
concurrency = 2
```

**`source_mount = /home/seigyo/mnt/projectF`**

The **absolute path** of the folder you want converted. Everything below it is walked automatically, subfolders included, and every supported format (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.md`, `.txt` …) is picked up on its own. To scope a project to part of a mount, give a folder inside it (for example `/home/seigyo/mnt/projectF/operations-manuals`) instead of the mount point. Nothing outside this path is ever read. Relative paths are not accepted, and a missing folder fails at `check` / `sync` time.

**`target_name = 中国配自SIM`**

The single folder name created in GROWI, that is **the project name your readers see**. This example publishes to `http://<growi-host>:3000/中国配自SIM/`, with `00-目次` and the numbered pages below it. Japanese and ASCII names both work, but `/` is not allowed. The same name is also used for the local working folder, so `data/中国配自SIM/` and GROWI `/中国配自SIM/` pair up. Renaming it later **creates a separate new project** and leaves the old GROWI folder behind; clear the old one with `reset` using the old config, or delete it in GROWI.

**`data_root = data`**

Where the working data lives. `data` means the repository's `data/` folder; an absolute path works too. The folder actually used is `<data_root>/<target_name>/` (here `data/中国配自SIM/`) and contains `raw/` (converted text), `wiki/` (generated pages) and `metadata/` (which document was published where). Deleting it loses the publish state, so use `reset` instead of `rm` to remove GROWI pages. Disk usage grows with document volume.

**`[settings] concurrency = 2`**

How many LLM requests this project keeps in flight at once. Higher is faster, but anything the endpoint cannot serve just queues, and once the wait exceeds `wiki_request_timeout` (default 300 seconds) **requests start timing out**. **Keep it at 4-5 when only 1-2 projects run**; when several projects run at the same time, drop it to **2 or below** so the total stays within what the endpoint can serve. This example uses `2` because it runs alongside other projects on the same machine. Omit the line and `OPENAI_CONCURRENCY` from `.env` (default 4) applies.

`[settings]` is optional, the three `[project]` keys are not. A misspelled key stops the run with `unknown project setting(s): ...`.

---

### 3. Override endpoints for this project only

Add these only when the `.env` defaults are not enough; anything you leave out keeps its `.env` value.

**`[project] growi_url` / `growi_token`** … publish this project alone to a different GROWI (the test instance, for example) by giving its URL and API token.

**`[settings] chat_base_url` / `chat_model` / `chat_api_key`** … LLM endpoint URL, model name and API key, when you want a different model or a test LLM.

**`[settings] parser_base_url`** … doc-parser service URL; not needed for `.md` / `.txt` only projects.

**`[settings] wiki_request_timeout`** … per-call model timeout in seconds; raise it when large documents keep timing out.

---

### 4. Check and sync

```bash
.venv/bin/python main.py check --project newproj
.venv/bin/python main.py -v sync --project newproj
```

The first sync takes hours depending on document volume. Interrupting keeps the already-published pages, and `--continue` resumes the rest. Later runs of `sync` only handle new and changed documents, so a fully synced project finishes in tens of seconds.

---

## 7. Pointing A Project At A Test Endpoint

`configs/test.ini` is the working example: `.env` keeps the production endpoints while this project alone points at another server.

```ini
[project]
source_mount = /home/seigyo/llm-wiki/data/test/mount
target_name = test
data_root = data
growi_url = http://10.160.152.38:3000/     # test GROWI instance
growi_token = <test API token>

[settings]
concurrency = 2
```

- Settings apply in this order: `.env` → `configs/<name>.ini` → command-line arguments. Only the keys written in the INI are overridden.
- To change only GROWI, write the two `[project]` keys; to change only the LLM, write `[settings] chat_base_url` (plus `chat_model` when the model name differs).
- To copy it for your own verification, create `cp configs/test.ini configs/mytest.ini` and rewrite `source_mount` and `target_name`. Keeping `target_name = test` would publish into the same GROWI folder, so change it.

---

## 8. Operations Commands

So far you only used `check` and `sync`. Regular operation uses these four commands; every one takes `--project <name>` (add `-v` for per-document progress).

<table>
  <thead>
    <tr><th>Command</th><th>Options</th><th>Purpose</th></tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3"><code>sync</code></td>
      <td><em>none</em></td>
      <td>Convert → generate → publish only new and changed documents</td>
    </tr>
    <tr>
      <td><code>--continue</code></td>
      <td>Reuse work already done and rebuild only what is unfinished</td>
    </tr>
    <tr>
      <td><code>--force "&lt;path&gt;"</code></td>
      <td>Regenerate one named document even when it is unchanged</td>
    </tr>
    <tr>
      <td><code>publish</code></td>
      <td>—</td>
      <td>Re-send built pages to GROWI without regenerating</td>
    </tr>
    <tr>
      <td><code>pull</code></td>
      <td>—</td>
      <td>Capture edits people made in GROWI</td>
    </tr>
    <tr>
      <td><code>reset</code></td>
      <td>—</td>
      <td>Delete the pages this tool published</td>
    </tr>
  </tbody>
</table>

`--continue` and `--force` are options of `sync` only. The other commands take no options.

---

### sync

```bash
.venv/bin/python main.py -v sync --project newproj
```

Scans the mount and converts → generates → publishes only new and changed documents. On an already-synced project nothing changed, so it finishes in tens of seconds; a first run takes hours depending on document volume.

#### `--continue` (resume)

```bash
.venv/bin/python main.py -v sync --project newproj --continue
```

`sync` proceeds in this order:

- Per document: convert → generate pages → publish to GROWI → accept locally. An accepted document is never redone unless its source file changes
- Once every document is finished, cross-document links are added in one pass, that pass is accepted, and only the pages the links changed are re-published
- Index pages are published last

Example: you run `sync` over 5 documents and it stops after 3 have been generated and published.

- The 3 accepted documents are left alone: no regeneration, no re-publish, no cost, because unchanged sources are never queued
- The document that was half-built restarts from conversion through page generation. Pages already sent to GROWI stay as they are, so a plain `publish` is enough when only the send was missing
- `--continue` reuses the analysis already paid for. The entities and behaviours extracted from each page and the Jev role and link-candidate decisions are cached locally, so documents already analysed are skipped and only the unanalysed ones are processed. Without it, every document in the link scope is analysed again, which re-pays those LLM calls

#### `--force "<path>"` (rebuild one document)

```bash
.venv/bin/python main.py -v sync --project newproj --force "Manuals/UserGuide.pdf"
```

The path is relative to the mount. That document is regenerated and published whether or not it changed; other documents are untouched.

---

### publish

```bash
.venv/bin/python main.py publish --project newproj
```

Sends the pages that already exist locally to GROWI without regenerating anything. If a send fails partway, running it again sends what is still missing.

---

### pull

```bash
.venv/bin/python main.py pull --project newproj
.venv/bin/python main.py human status --project newproj
```

Imports edits made by people in GROWI so the next sync does not overwrite them. `human status` lists pages still holding human edits and pages held back by conflicts; resolve a conflict by editing the page in GROWI.

---

### reset

```bash
.venv/bin/python main.py reset --project newproj
```

Trashes the GROWI pages this tool published and resets the local publish state. Pages created manually in GROWI are not deleted. Running `sync` again rebuilds them.

---

## 9. Reference: Environment Variables

Set them in the root `.env`. The top block of `.env` holds the settings you change during operation; everything below it is developer flags, so do not touch it until you know what each one does. Values are overridden in this order: `.env` → `configs/<name>.ini` → command-line arguments; `.env` is shared by all projects and a project file overrides only its own values. There is no `.env.example`, so edit `.env` directly. Do not commit real endpoints or tokens to a public repository.

---

### LLM

| Variable | Purpose | Example | What changes |
| --- | --- | --- | --- |
| `OPENAI_BASE_URL` | LLM endpoint URL | `http://10.160.144.101:51029/v1` | Switches the LLM used for generation. Wrong value makes `chat` show `DOWN` in `check` and nothing generates |
| `OPENAI_API_KEY` | API key; use `local` for endpoints that need no key | `local` / `sk-...` | A wrong key makes `chat` return 401 and `sync` stops on the first document |
| `OPENAI_MODEL_NAME` | Model name on the endpoint | `gemma-4-31B` | Generates with another model, changing writing style and summary granularity; a non-existent name errors out |
| `OPENAI_CONCURRENCY` | Concurrency (default `4`) | `2` (several projects at once) / `4`-`5` (1-2 projects) | Higher is faster, but anything the endpoint cannot serve queues and then times out. Divide it between projects |
| `WIKI_REQUEST_TIMEOUT` | Per-call model timeout in seconds (default `300`) | `900` | Fewer timeouts on large documents; too high delays failure detection and you wait longer on a stalled run |

---

### doc-parser (document conversion service)

| Variable | Purpose | Example | What changes |
| --- | --- | --- | --- |
| `WIKI_PARSER_BASE_URL` | Conversion service URL | `http://10.160.144.101:51027/agent/doc-parser/` | Unset or wrong, and `.pdf` / `.docx` / `.xlsx` conversions fail (can stay unset for `.md` / `.txt` only projects) |
| `WIKI_PARSER_TIMEOUT` | Maximum wait per file in seconds (default `7200`) | `3600` / `14400` | Lower fails large files partway; higher keeps you waiting longer when the service is down |

---

### GROWI

| Variable | Purpose | Example | What changes |
| --- | --- | --- | --- |
| `GROWI_URL` | GROWI URL | `http://10.160.152.235:3000/` | The GROWI instance you publish to. Writing `growi_url` in the project INI sends that project alone to the test GROWI |
| `GROWI_TOKEN` | API token | `xEOD0xTA…` (works with or without the `API Token:` prefix) | Without page-create permission `publish` fails; mixing it up writes to the other GROWI environment |
| `GROWI_MODE` | `attach` (default) or `own` | `attach` | `attach` never writes outside the project folder; `own` treats the pages as owned by this tool |
| `GROWI_TIMEOUT` | GROWI API timeout in seconds (default `30`) | `60` | Raising it on a slow GROWI reduces missed publications |

---

### Generated pages

| Variable | Purpose | Example | What changes |
| --- | --- | --- | --- |
| `WIKI_OUTPUT_LANGUAGE` | Language of generated pages (default `Japanese (日本語)`) | `"Japanese (日本語)"` / `"English"` | Changes the language of pages generated from now on; existing pages only change after `--force` |
| `WIKI_LINKER_ENABLED` | Whether to add cross-document links (default `true`) | `false` | Skips link building, saving those LLM calls and that much running time |

---

## 10. Supported File Formats

- `.md`
- `.txt`
- `.pdf`
- `.docx`
- `.doc`
- `.pptx`
- `.xlsx`
- `.xlsm`
- `.xls`
- `.csv`

`.md` and `.txt` are used as-is; everything else goes through the doc-parser service. Convert Word/Excel 97-2003 (`.doc`, `.xls`) to a modern format beforehand. `.xlsm` macros are never executed; worksheets and the macro content (VBA) are published as separate pages.

---

## 11. Logs and Working Files

- `<data_root>/<name>/raw/`: converted text
- `<data_root>/<name>/wiki/`: generated pages
- `<data_root>/<name>/metadata/`: publish state, watch queue
- `logs/<name>/`: per-run `live.log` / `debug.log` output and the token/URL-redacted `failed_docs.md` summary

Deleting `<data_root>/<name>/` removes the local publish state while the GROWI pages stay (use `reset` to delete them). Logs and generated content contain internal documents; set access rights and retention to match your operational policy.
