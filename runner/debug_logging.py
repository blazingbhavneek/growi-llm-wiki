"""Readable, self-contained diagnostic records for debug.log."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any


def progress_event_name(event: Mapping[str, Any]) -> str:
    stage = str(event.get("stage") or "work")
    step = str(event.get("step") or "")
    return f"{stage}.{step}" if step else stage


def _field_lines(key: str, value: Any, *, indent: str = "  ") -> list[str]:
    """Keep field names and complete values, expanding collections vertically."""
    if "\n" in key or "\r" in key:
        key = json.dumps(key, ensure_ascii=False)
    heading = f"{indent}{key}" + ("" if key == "-" else ":")
    if isinstance(value, Mapping) and value:
        lines = [heading]
        for child_key, child_value in value.items():
            lines.extend(_field_lines(str(child_key), child_value, indent=indent + "  "))
        return lines
    if isinstance(value, (list, tuple)) and value:
        lines = [heading]
        for item in value:
            lines.extend(_field_lines("-", item, indent=indent + "  "))
        return lines
    if isinstance(value, str):
        # Quote empty/multiline strings so data cannot create apparent record boundaries.
        rendered = json.dumps(value, ensure_ascii=False) if not value or "\n" in value or "\r" in value else value
    else:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    separator = " " if key == "-" else ": "
    return [f"{indent}{key}{separator}{rendered}"]


class DebugLogFormatter(logging.Formatter):
    """Format one whole block per emit, including fields and chained exceptions.

    This formatter has no document/session state: every record remains useful when
    concurrent jobs interleave or a reader opens a rotated file halfway through a run.
    """

    def __init__(self, *, run_id: str = "", redact: Callable[[str], str] | None = None) -> None:
        super().__init__("%(message)s")
        self.run_id = run_id
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        entry = copy.copy(record)  # format() and exception caches must not affect live.log.
        stamp = self.formatTime(entry, "%Y-%m-%d %H:%M:%S") + f".{int(entry.msecs):03d}"
        header = f"{stamp} {entry.levelname:<5}"
        if self.run_id:
            header += f" run={self.run_id}"
        header += f" {entry.name}"
        event = getattr(entry, "progress_event", None)
        fields = getattr(entry, "debug_fields", {})
        lines = []
        if isinstance(event, Mapping):
            header += f" | {progress_event_name(event)}"
            # stage/step form the event code. Retain every other original field,
            # including zero counts, false flags and unknown future diagnostics.
            fields = {key: value for key, value in event.items() if key not in {"stage", "step"}}
        else:
            lines.extend("  " + line for line in entry.getMessage().splitlines())
        if isinstance(fields, Mapping):
            # Put document/page context before the technical details without
            # shortening filenames or mutating the original event dictionary.
            keys = [key for key in ("document", "file", "page") if key in fields]
            keys.extend(key for key in fields if key not in keys)
            for key in keys:
                lines.extend(_field_lines(str(key), fields[key]))
        if entry.exc_info:
            lines.append("  例外:")
            lines.extend("    " + line for line in self.formatException(entry.exc_info).splitlines())
        elif entry.exc_text:
            lines.append("  例外:")
            lines.extend("    " + line for line in entry.exc_text.splitlines())
        if entry.stack_info:
            lines.append("  呼び出し元:")
            lines.extend("    " + line for line in self.formatStack(entry.stack_info).splitlines())
        text = "\n".join([header, *lines]) + "\n"  # handler terminator adds the blank separator.
        # Redact the complete block, including structured fields and chained traces.
        return self.redact(text) if self.redact is not None else text
