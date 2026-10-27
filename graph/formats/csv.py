"""csv parser output: the complete table becomes one page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common.policy import policy_of
from .tabular import write_tables


async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    lines = source_path.read_text(encoding="utf-8").splitlines()
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), source_path.stem)
    body = "\n".join(lines)
    # The parser emits "# title" before the HTML table; the table must start the body
    # or it is read as a Markdown table, finds no regions and is labelled too large.
    if "<table" in body:
        body = body[body.find("<table"):body.rfind("</table>") + len("</table>")]
    return await write_tables(
        sheets=[(title, body, (1, len(lines)))], run_dir=run_dir, model=model, config=config,
        on_progress=on_progress, stop_check=stop_check,
        generate_analyses=policy_of(config).table_analyses and not getattr(config, "skip_excel_and_small", False),
    )
