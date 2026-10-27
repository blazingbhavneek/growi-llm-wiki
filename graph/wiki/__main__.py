from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import WikiConfig
from .pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lossless wiki from Markdown")
    parser.add_argument("source", help="source Markdown document")
    parser.add_argument("--output", default=".wiki/pages", help="output root")
    parser.add_argument("--slug", default="", help="output slug")
    parser.add_argument("--kind", default=None, help="source format override")
    args = parser.parse_args()
    from ..formats import kind_of

    config = WikiConfig(
        output_root=args.output,
        document_slug=args.slug,
        source_kind=args.kind or kind_of(args.source),
    )
    def progress(event: dict) -> None:
        stage = event.get("stage", "work")
        step = event.get("step", "")
        current = event.get("current")
        total = event.get("total")
        count = f" {current}/{total}" if current is not None and total is not None else ""
        detail = event.get("batch") or event.get("page") or event.get("output") or ""
        if stage == "observe" and step == "window":
            detail = (
                f"window={event.get('window', '?')} "
                f"lines={event.get('source_start', '?')}-{event.get('source_end', '?')} "
                f"observations={event.get('observations', 0)}"
            )
            if event.get("live_output"):
                detail += f" -> {event['live_output']}"
        elif stage == "plan" and step in {"compile_retry", "compile_done"}:
            pieces = []
            if event.get("attempt") is not None:
                pieces.append(f"attempt={event['attempt']}")
            if event.get("pages") is not None:
                pieces.append(f"pages={event['pages']}")
            if event.get("repairs"):
                pieces.append(f"boundary_repairs={event['repairs']}")
            if event.get("prompt"):
                pieces.append(f"prompt={event['prompt']}")
            if event.get("response"):
                pieces.append(f"response={event['response']}")
            detail = " ".join(pieces)
        elif stage == "plan" and step == "region":
            detail = (
                f"lines={event.get('source_start', '?')}-{event.get('source_end', '?')} "
                f"candidates={event.get('pages', 0)}"
            )
        elif stage == "plan" and step == "semantic":
            detail = f"attempt={event.get('attempt', '?')}"
        elif stage == "write":
            pieces = [str(event.get("page", "")), f"section={event.get('section', '?')}"]
            for key in ("attempt", "score", "missing"):
                if event.get(key) is not None:
                    pieces.append(f"{key}={event[key]}")
            detail = " ".join(pieces)
        elif stage == "research":
            detail = f"{event.get('page', '')} references={event.get('references') or event.get('reference', '')}"
        elif stage == "rewrite" and step == "page_done":
            pieces = [str(event.get("page", ""))]
            if event.get("attempts") is not None:
                pieces.append(f"attempts={event['attempts']}")
            if event.get("score") is not None:
                pieces.append(f"score={event['score']}")
            detail = " ".join(pieces)
        elif stage == "link" and step == "page_done":
            detail = f"{event.get('page', '')} attempts={event.get('attempts', 0)}"
        if event.get("error"):
            detail = f"{detail} error={event['error']}".strip()
        fallback = " [fallback]" if event.get("fallback") else ""
        cached = " [cached]" if event.get("cached") else ""
        print(
            f"[wiki] {stage}: {step}{count}{fallback}{cached} {detail}".rstrip(),
            file=sys.stderr,
            flush=True,
        )

    result = asyncio.run(run_pipeline(args.source, config=config, on_progress=progress))
    print(json.dumps({"run": str(result), "wiki": str(result / "wiki")}, indent=2))


if __name__ == "__main__":
    main()
