"""Tables: grid, model-selected structure, records, pages, and safe queries."""

from __future__ import annotations

import asyncio
import csv as _csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from common.policy import policy_of
from graph.config import app_concurrency

Cell = tuple[int, int]


@dataclass
class Grid:
    cells: dict[Cell, str]
    col_letters: dict[int, str] = field(default_factory=dict)

    @property
    def rows(self) -> list[int]:
        return sorted({r for r, _ in self.cells})

    @property
    def cols(self) -> list[int]:
        return sorted({c for _, c in self.cells})

    def get(self, row: int, col: int) -> str:
        return self.cells.get((row, col), "")

    def letter(self, col: int) -> str:
        return self.col_letters.get(col) or _letter(col)


def _letter(col: int) -> str:
    result = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        result = chr(65 + rem) + result
    return result


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._in_th = False
        self.headers: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._span = (int(attrs.get("rowspan", 1) or 1), int(attrs.get("colspan", 1) or 1))
            self._in_th = tag == "th"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = "".join(self._cell).strip()
            if self._in_th:
                self.headers.append(text)
            else:
                self._row.append((text, *self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def grid_from_html(table_html: str, *, origin: Cell = (1, 1)) -> Grid:
    parser = _TableParser()
    parser.feed(table_html)
    if not parser.headers or parser.headers[0].casefold() != "row":
        cells: dict[Cell, str] = {}
        covered: set[Cell] = set()
        for row, raw in enumerate(parser.rows, origin[0]):
            col = origin[1]
            for text, rowspan, colspan in raw:
                while (row, col) in covered:
                    col += 1
                if text:
                    cells[(row, col)] = text
                for dr in range(rowspan):
                    for dc in range(colspan):
                        if dr or dc:
                            covered.add((row + dr, col + dc))
                col += colspan
        return Grid(
            cells=cells,
            col_letters={col: _letter(col) for col in {col for _, col in cells}},
        )
    letters = {i: name for i, name in enumerate(parser.headers[1:], 1)}
    cells: dict[Cell, str] = {}
    covered: set[Cell] = set()
    for raw in parser.rows:
        row_number = int(re.sub(r"\D", "", raw[0][0]) or 0)
        col = 1
        for text, rowspan, colspan in raw[1:]:
            while (row_number, col) in covered:
                col += 1
            if text:
                cells[(row_number, col)] = text
            for dr in range(rowspan):
                for dc in range(colspan):
                    if dr or dc:
                        covered.add((row_number + dr, col + dc))
            col += colspan
    return Grid(cells=cells, col_letters=letters)


def _table_origin(body: str) -> Cell:
    match = re.search(r"_元範囲:\s*([A-Z]+)(\d+):", body)
    if not match:
        return (1, 1)
    col = 0
    for character in match.group(1):
        col = col * 26 + ord(character) - 64
    return int(match.group(2)), col


def grid_from_gfm(lines: Sequence[str]) -> Grid:
    cells: dict[Cell, str] = {}
    row = 0
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", p) for p in parts):
            continue
        row += 1
        for col, text in enumerate(parts, 1):
            if text:
                cells[(row, col)] = text.replace("\\|", "|")
    return Grid(cells=cells)


@dataclass
class Region:
    id: int
    r1: int
    c1: int
    r2: int
    c2: int
    cell_count: int

    def preview(self, grid: Grid, *, rows: int, cols: int) -> str:
        picked = list(range(self.r1, min(self.r2, self.r1 + rows - 1) + 1))
        if self.r2 - self.r1 + 1 > rows:
            picked += [self.r2 - 1, self.r2]
        return "\n".join(
            f"行{r}: " + " | ".join(
                f"{grid.letter(c)}:{grid.get(r, c)[:24]}"
                for c in range(self.c1, min(self.c2, self.c1 + cols - 1) + 1)
                if grid.get(r, c)
            )
            for r in picked
        )


def find_regions(grid: Grid) -> list[Region]:
    rows, cols = grid.rows, grid.cols
    if not rows:
        return []
    occupied = set(rows)
    bands: list[tuple[int, int]] = []
    start: int | None = rows[0]
    for row in range(rows[0], rows[-1] + 1):
        if row not in occupied:
            if start is not None:
                bands.append((start, row - 1))
            start = None
        elif start is None:
            start = row
    if start is not None:
        bands.append((start, rows[-1]))
    regions: list[Region] = []
    for r1, r2 in bands:
        used = sorted({c for r, c in grid.cells if r1 <= r <= r2})
        c_start = used[0]
        for i, col in enumerate(used):
            next_col = used[i + 1] if i + 1 < len(used) else None
            if next_col is None or next_col > col + 1:
                count = sum(1 for r, c in grid.cells if r1 <= r <= r2 and c_start <= c <= col)
                regions.append(Region(len(regions) + 1, r1, c_start, r2, col, count))
                if next_col is not None:
                    c_start = next_col
    big = [r for r in regions if r.cell_count > 3]
    for small in [r for r in regions if r.cell_count <= 3]:
        host = next((b for b in big if b.r1 - 2 <= small.r2 < b.r1 or (b.r1 <= small.r1 <= b.r2 and abs(b.c1 - small.c2) <= 2)), None)
        if host:
            host.r1, host.c1 = min(host.r1, small.r1), min(host.c1, small.c1)
            host.r2, host.c2 = max(host.r2, small.r2), max(host.c2, small.c2)
            host.cell_count += small.cell_count
    result = [r for r in regions if r.cell_count > 3] or regions
    for i, region in enumerate(result, 1):
        region.id = i
    return result


class TableSpec(BaseModel):
    region: int = 0
    title: str = ""
    orientation: Literal["rows", "columns"] = "rows"
    header_rows: list[int] = Field(default_factory=list)
    label_cols: list[int] = Field(default_factory=list)
    data_rows: list[int] = Field(default_factory=list)
    data_cols: list[int] = Field(default_factory=list)
    notes: str = ""


class SheetStructure(BaseModel):
    summary: str = ""
    tables: list[TableSpec] = Field(default_factory=list)
    ignore: list[int] = Field(default_factory=list)


class VbaDescription(BaseModel):
    title: str = Field(description="VBA処理の内容を表す短い日本語名")
    summary: str = Field(description="VBA処理の日本語説明")


def _numeric(text: str) -> float | None:
    raw = text.strip()
    if not re.fullmatch(r"^[-+]?[¥$€]?\s*\d[\d,]*(\.\d+)?\s*%?$", raw):
        return None
    try:
        return float(raw.replace(",", "").replace("¥", "").replace("$", "").replace("€", "").rstrip("%").strip())
    except ValueError:
        return None


def heuristic_structure(
    regions: Sequence[Region],
    grid: Grid,
    *,
    header_row: bool = True,
    orientation: Literal["rows", "columns"] = "rows",
) -> SheetStructure:
    def spec(region: Region) -> TableSpec:
        if orientation == "columns":
            return TableSpec(
                region=region.id,
                title=f"領域 {region.id}",
                orientation="columns",
                header_rows=[region.r1],
                label_cols=[region.c1] if region.c1 < region.c2 else [],
                data_rows=[region.r1, region.r2],
                data_cols=[min(region.c1 + 1, region.c2), region.c2],
            )
        return TableSpec(
            region=region.id,
            title=f"領域 {region.id}",
            orientation="rows",
            header_rows=[region.r1] if header_row else [],
            label_cols=[region.c1],
            data_rows=[min(region.r1 + 1, region.r2) if header_row else region.r1, region.r2],
            data_cols=[region.c1, region.c2],
        )

    return SheetStructure(
        summary=f"（自動判定: {'行' if orientation == 'rows' else '列'}方向）",
        tables=[spec(region) for region in regions],
    )


async def decide_structure(sheet: str, grid: Grid, regions: Sequence[Region], *, model: Any, config: Any) -> SheetStructure:
    from langchain_core.messages import HumanMessage

    row_structure = heuristic_structure(
        regions,
        grid,
        header_row=getattr(config, "source_kind", "csv") != "xlsx",
        orientation="rows",
    )
    if not policy_of(config).model_table_structure:
        return row_structure  # deterministic row-oriented reading, no model call
    column_structure = heuristic_structure(regions, grid, orientation="columns")
    previews = "\n\n".join(
        f"### 領域 {region.id}\n{region.preview(grid, rows=config.tabular_preview_rows, cols=config.tabular_preview_cols)}"
        for region in regions
    )
    try:
        answer = await model.text([HumanMessage(content=(
            f"シート「{sheet}」の表は、1行を1件として読む表ですか、それとも1列を1件として読む表ですか。\n\n"
            f"{previews}\n\n回答は「行」または「列」の一語だけにしてください。"
        ))])
    except Exception:
        return row_structure
    choice = answer.strip().splitlines()[-1].strip("。. `").casefold() if answer.strip() else ""
    return column_structure if choice in {"列", "column", "columns"} else row_structure


@dataclass
class Record:
    key: str
    label: str
    values: dict[str, str]


def headers_for(grid: Grid, spec: TableSpec) -> dict[int, str]:
    names: dict[int, str] = {}
    last = ""
    if spec.orientation == "rows":
        c1, c2 = spec.data_cols
        for col in range(c1, c2 + 1):
            parts = [grid.get(row, col) for row in spec.header_rows if grid.get(row, col)]
            name = " / ".join(parts) or last or grid.letter(col)
            if parts:
                last = name
            names[col] = name
    else:
        r1, r2 = spec.data_rows
        for row in range(r1, r2 + 1):
            parts = [grid.get(row, col) for col in spec.label_cols if grid.get(row, col)]
            name = " / ".join(parts) or last or f"行{row}"
            if parts:
                last = name
            names[row] = name
    return names


def records_for(grid: Grid, spec: TableSpec) -> tuple[list[str], list[Record]]:
    names = headers_for(grid, spec)
    records: list[Record] = []
    if spec.orientation == "rows":
        for row in range(spec.data_rows[0], spec.data_rows[1] + 1):
            values = {names[col]: grid.get(row, col) for col in names if grid.get(row, col)}
            if values:
                label = " ".join(grid.get(row, col) for col in spec.label_cols if grid.get(row, col))
                records.append(Record(f"行 {row}", label, values))
    else:
        for col in range(spec.data_cols[0], spec.data_cols[1] + 1):
            values = {names[row]: grid.get(row, col) for row in names if grid.get(row, col)}
            if values:
                label = " ".join(grid.get(row, col) for row in spec.header_rows if grid.get(row, col))
                records.append(Record(f"列 {_letter(col)}", label, values))
    return list(dict.fromkeys(name for record in records for name in record.values)), records


def stats_for(columns: Sequence[str], records: Sequence[Record]) -> list[dict[str, Any]]:
    output = []
    for column in columns:
        raw = [record.values.get(column, "") for record in records if record.values.get(column, "")]
        nums = [value for value in (_numeric(item) for item in raw) if value is not None]
        entry: dict[str, Any] = {"column": column, "filled": len(raw)}
        if nums and len(nums) >= len(raw) * 0.6:
            entry.update(kind="numeric", min=min(nums), max=max(nums), mean=sum(nums) / len(nums), sum=sum(nums))
            entry["max_key"] = next(record.key for record in records if _numeric(record.values.get(column, "")) == max(nums))
            entry["min_key"] = next(record.key for record in records if _numeric(record.values.get(column, "")) == min(nums))
        else:
            distinct = list(dict.fromkeys(raw))
            entry.update(kind="text", distinct=len(distinct), top=distinct[:20])
        output.append(entry)
    return output


SPEC_MARK = "<!-- table-spec: {} -->"
SPEC_RE = re.compile(r"^<!-- table-spec: (\{.*\}) -->\s*$", re.MULTILINE)


def spec_comment(sheet: str, spec: TableSpec) -> str:
    return SPEC_MARK.format(json.dumps({"sheet": sheet, **spec.model_dump()}, ensure_ascii=False))


def specs_in_page(body: str) -> list[dict[str, Any]]:
    return [json.loads(match.group(1)) for match in SPEC_RE.finditer(body)]


def grids_in_page(body: str) -> list[Grid]:
    html = [grid_from_html(match.group(0), origin=_table_origin(body)) for match in re.finditer(r"<table>.*?</table>", body, re.DOTALL)]
    if html:
        return html
    tables: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines() + [""]:
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            if len(current) >= 2:
                tables.append(current)
            current = []
    return [grid_from_gfm(table) for table in tables]


def records_from_page(body: str) -> list[tuple[dict[str, Any], list[str], list[Record]]]:
    grids = grids_in_page(body)
    if not grids:
        table = [line for line in body.splitlines() if line.strip().startswith("|")]
        grids = [grid_from_gfm(table)] if table else []
    output = []
    for index, spec_dict in enumerate(specs_in_page(body)):
        spec = TableSpec.model_validate({k: v for k, v in spec_dict.items() if k != "sheet"})
        # Rendered pages may contain a statistics table before the original
        # GFM source; the last table is the source table in that format.
        grid = grids[index] if "<table>" in body and index < len(grids) else (grids[-1] if grids else Grid(cells={}))
        columns, records = records_for(grid, spec)
        output.append((spec_dict, columns, records))
    return output


def query_records(columns: Sequence[str], records: Sequence[Record], sql: str, *, limit: int = 200) -> str:
    conn = sqlite3.connect(":memory:")
    quoted = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in ["key", "label", *columns])
    conn.execute(f"CREATE TABLE t ({quoted})")
    rows = []
    for record in records:
        values = []
        for column in columns:
            value = record.values.get(column, "")
            values.append(_numeric(value) if _numeric(value) is not None else value)
        rows.append([record.key, record.label, *values])
    conn.executemany(f"INSERT INTO t VALUES ({', '.join('?' * (len(columns) + 2))})", rows)

    def authorizer(action, arg1, *_args):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 == "t" else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    statement = sql.strip().rstrip(";")
    if ";" in statement:
        return "error: one statement only"
    if not re.search(r"\blimit\b", statement, re.IGNORECASE):
        statement += f" LIMIT {limit}"
    try:
        cursor = conn.execute(statement)
        output = io.StringIO()
        writer = _csv.writer(output)
        writer.writerow([description[0] for description in cursor.description or []])
        writer.writerows(cursor.fetchmany(limit))
        return output.getvalue()[:8000]
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        conn.close()


def render_table_page(sheet: str, structure: SheetStructure, tables: list[tuple[TableSpec, list[str], list[Record], list[dict]]], table_html: str, grid: Grid) -> str:
    output = [f"# {sheet}", "", structure.summary, ""]
    for spec, columns, records, stats in tables:
        output += [f"## {spec.title}", "", spec_comment(sheet, spec), "", f"- レコード: {'行' if spec.orientation == 'rows' else '列'}（{len(records)} 件）", f"- データ範囲: 行 {spec.data_rows[0]}-{spec.data_rows[1]}, 列 {grid.letter(spec.data_cols[0])}-{grid.letter(spec.data_cols[1])}", "", "### 列の定義", ""]
        output += [f"- **{column}**" for column in columns] + ["", "### 統計", "", "| 列 | 種類 | 値 |", "|---|---|---|"]
        for stat in stats:
            summary = (f"min {stat['min']:g}（{stat['min_key']}） / max {stat['max']:g}（{stat['max_key']}） / mean {stat['mean']:.4g} / sum {stat['sum']:g}" if stat["kind"] == "numeric" else f"{stat['distinct']} 種類: {', '.join(map(str, stat['top'][:8]))}")
            output.append(f"| {stat['column']} | {stat['kind']} | {summary} |")
        output.append("")
    return "\n".join(output + ["## 元データ（原本）", "", table_html, ""])


def analysis_prompt(sheet: str, spec: TableSpec, columns: Sequence[str], records: Sequence[Record], stats: Sequence[dict], *, language: str, feedback: Sequence[str] = ()) -> str:
    table = ["| key | label | " + " | ".join(columns) + " |", "|" + "---|" * (len(columns) + 2)]
    table += [f"| {record.key} | {record.label} | " + " | ".join(record.values.get(column, "") for column in columns) + " |" for record in records]
    feedback_text = ("\n\n前回の指摘:\n" + "\n".join(f"- {item}" for item in feedback)) if feedback else ""
    return f"シート「{sheet}」の表「{spec.title}」です。統計:\n{json.dumps(stats, ensure_ascii=False)}\n\nレコード:\n" + "\n".join(table) + f"\n\nこの表から読み取れることをMarkdownで書いてください。数値や行に言及するときは必ず `行 12` / `列 D` 形式で引用してください。{feedback_text}\n出力言語: {language}。"


CITE_RE = re.compile(r"(行|列)\s*([A-Z]+|\d+)")


def check_citations(markdown: str, records: Sequence[Record]) -> list[str]:
    keys = {record.key.replace(" ", "") for record in records}
    bad = sorted({f"{kind}{number}" for kind, number in CITE_RE.findall(markdown) if f"{kind}{number}" not in keys})
    return [f"存在しないキーを引用しています: {', '.join(bad[:10])}"] if bad else []


async def describe_vba(sheet: str, body: str, *, model: Any, language: str) -> VbaDescription:
    from langchain_core.messages import HumanMessage

    result = await model.structured(VbaDescription, [
        HumanMessage(content=(
            f"次のVBAコード「{sheet}」を、ワークブック内での役割が分かるように説明してください。"
            "目的、起動元のボタンまたはセル、参照シート、更新シート、最終出力への影響を、"
            "確認できる事実だけで簡潔にまとめてください。titleは処理内容を表す短い日本語名にし、"
            "モジュール名やプロシージャ名をそのまま使わないでください。コードは再掲しないでください。\n\n"
            f"{body}\n\n出力言語: {language}。"
        ))
    ])
    result.title = re.sub(r"^マクロ[-：:\s]*", "", result.title.strip())
    if not re.search(r"[ぁ-んァ-ヶ一-龯々]", result.title) or re.search(r"[A-Za-z]", result.title):
        result.title = "宣言" if sheet.endswith("-宣言") else "処理"
    return result


async def write_tables(
    *,
    sheets: list[tuple[str, str, tuple[int, int]]],
    run_dir,
    model,
    config,
    on_progress=None,
    stop_check=None,
    generate_analyses: bool = True,
) -> list[dict[str, Any]]:
    from langchain_core.messages import HumanMessage
    from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic

    docs = Path(run_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    prepared = []
    link_renames: dict[str, str] = {}
    progress_stage = "excel-source" if config.source_kind == "xlsx" else "tabular"
    if on_progress:
        on_progress({"stage": progress_stage, "step": "start", "total": len(sheets)})
    semaphore = asyncio.Semaphore(
        max(1, int(getattr(config, "rewrite_concurrency", app_concurrency())))
    )
    completed = 0

    async def prepare(number: int, item: tuple[str, str, tuple[int, int]]):
        nonlocal completed
        sheet, table_text, source_range = item
        async with semaphore:
            if stop_check and stop_check():
                raise RuntimeError("tabular cancelled")
            grid = grid_from_html(table_text, origin=_table_origin(table_text)) if table_text.lstrip().startswith("<table") else grid_from_gfm(table_text.splitlines())
            is_vba = "<!-- vba-id:" in table_text
            if on_progress:
                on_progress({
                    "stage": progress_stage,
                    "step": "describe",
                    "current": number,
                    "total": len(sheets),
                    "kind": "vba" if is_vba else "sheet",
                    "sheet": sheet,
                })
            regions = [] if is_vba else find_regions(grid)
            # An unchanged sheet reuses its LLM answers, so a workbook update pays only for
            # the sheets that changed. The cache lives in the run state a full rebuild deletes.
            # Another policy's answers are keyed apart so a standard run never reuses them.
            digest = hashlib.sha256(f"{sheet}\0{table_text}{policy_of(config).cache_key('')}".encode("utf-8")).hexdigest()[:32]
            cache = Path(config.run_dir) / "sheet-cache" / f"{digest}.json" if getattr(config, "run_dir", None) else None
            cached = read_json(cache, default={}) if cache else {}
            if cached:
                description = VbaDescription.model_validate(cached["vba"]) if cached.get("vba") else None
                structure = SheetStructure.model_validate(cached["structure"])
            else:
                description = await describe_vba(
                    sheet,
                    table_text,
                    model=model,
                    language=config.output_language,
                ) if is_vba else None
                structure = SheetStructure(
                    summary=description.summary,
                    tables=[],
                ) if description else SheetStructure(summary="（大きすぎるため原本のみ）", tables=[]) if not regions or "_Sparse cell view" in table_text else await decide_structure(sheet, grid, regions, model=model, config=config)
                if cache:
                    write_json_atomic(cache, {"vba": description.model_dump() if description else None, "structure": structure.model_dump()})
            tables = []
            for spec in structure.tables:
                columns, records = records_for(grid, spec)
                if records:
                    tables.append((spec, columns, records, stats_for(columns, records)))
            page_title = f"マクロ-{description.title}" if description else source_page_title(sheet, is_vba=False) if config.source_kind == "xlsx" else sheet
            name = f"{number:03d}-{_slug(page_title)}.md"
            old_name = f"{number:03d}-{_slug(source_page_title(sheet, is_vba=True))}.md" if is_vba else name
            write_text_atomic(docs / name, render_table_page(page_title if is_vba else sheet, structure, tables, table_text, grid))
            completed += 1
            if on_progress:
                on_progress({
                    "stage": progress_stage,
                    "step": "done",
                    "current": completed,
                    "total": len(sheets),
                    "kind": "vba" if is_vba else "sheet",
                    "sheet": sheet,
                    "filename": name,
                })
            return (
                {"filename": name, "title": page_title if is_vba else sheet, "kind": "vba" if is_vba else "table", "source_ranges": [list(source_range)], "summary": structure.summary},
                (sheet, source_range, name, tables),
                (old_name, name) if old_name != name else None,
            )

    results = await asyncio.gather(*(prepare(number, item) for number, item in enumerate(sheets, 1)))
    for file, prepared_item, rename in results:
        files.append(file)
        prepared.append(prepared_item)
        if rename:
            link_renames[rename[0]] = rename[1]

    if link_renames:
        for path in docs.glob("*.md"):
            body = path.read_text(encoding="utf-8")
            rewritten = body
            for old_name, new_name in link_renames.items():
                rewritten = rewritten.replace(f"({old_name})", f"({new_name})")
            if rewritten != body:
                write_text_atomic(path, rewritten)

    number = len(prepared)
    for sheet, source_range, name, tables in prepared:
        if not generate_analyses:
            continue
        for spec, columns, records, stats in tables:
            slices = [records] if config.tabular_slice_records <= 0 else [records[i:i + config.tabular_slice_records] for i in range(0, len(records), config.tabular_slice_records)]
            targets = [("分析", records if len(records) <= 60 else records[:30] + records[-10:])]
            if len(slices) > 1:
                targets += [(str(i + 1), chunk) for i, chunk in enumerate(slices)]
            for suffix, subset in targets:
                feedback: list[str] = []
                text = ""
                for _ in range(3):
                    text = await model.text([HumanMessage(content=analysis_prompt(sheet, spec, columns, subset, stats, language=config.output_language, feedback=feedback))])
                    feedback = check_citations(text, subset)
                    if not feedback:
                        break
                if feedback:
                    text = "> 引用チェックに失敗したため、統計のみを掲載します。\n\n" + json.dumps(stats, ensure_ascii=False, indent=1)
                number += 1
                prefix = "解説" if config.source_kind == "xlsx" else "series"
                filename = f"{number:03d}-{prefix}-{_slug(sheet)}-{_slug(spec.title)}-{suffix}.md"
                write_text_atomic(docs / filename, f"# {spec.title} — {suffix}\n\n元の表: [{sheet}]({name})（{spec.title}）\n\n{text}\n")
                files.append({"filename": filename, "title": f"{spec.title} — {suffix}", "kind": "analysis", "source_ranges": [list(source_range)], "summary": text.strip().splitlines()[0][:120] if text.strip() else ""})
        if on_progress:
            on_progress({"stage": "tabular", "sheet": sheet, "tables": len(tables)})
    if on_progress:
        on_progress({"stage": progress_stage, "step": "complete", "current": len(sheets), "total": len(sheets)})
    planning = Path(run_dir) / "_planning"
    planning.mkdir(exist_ok=True)
    write_json_atomic(planning / "manifest.json", {"planning": {"ingest_mode": "wiki", "strategy": "tabular"}, "files": files})
    write_json_atomic(planning / "coverage.json", {"files": [{"title": item["title"], "filename": re.sub(r"^\d+-", "", item["filename"]), "summary": item["summary"], "header": "VBA" if item["kind"] == "vba" else "表", "source_start": item["source_ranges"][0][0], "source_end": item["source_ranges"][0][1]} for item in files]})
    write_json_atomic(planning / "metadata.json", {"files": [{"name": re.sub(r"^\d+-", "", item["filename"]), "header": "VBA" if item["kind"] == "vba" else "表"} for item in files]})
    return files


def _slug(text: str) -> str:
    from graph.wiki.ids import slugify

    return slugify(text, fallback="シート")


def source_page_title(sheet: str, *, is_vba: bool) -> str:
    title = re.sub(r"-part(\d+)$", r"-部分\1", sheet, flags=re.IGNORECASE)
    if is_vba:
        return re.sub(r"^vba-", "マクロ-", title, flags=re.IGNORECASE)
    return f"シート-{title}"
