from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.settings import load_settings

from .phase import Config, Input, assemble, run

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m publisher")
    parser.add_argument("command", choices=("assemble", "publish"), default="assemble", nargs="?")
    parser.add_argument("--config", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(args.out)
    if args.command == "assemble":
        result = assemble(root)
        print({"action": result.action, "pages": list(result.pages)})
        log.info("assemble finished action=%s pages=%d", result.action, len(result.pages))
        return 0
    if not args.config:
        parser.error("publish requires --config")
    try:
        settings = load_settings(args.config)
    except Exception as exc:
        log.error("project config could not be loaded setting=%s error=%s: %s (nothing was published)",
                  args.config or "(not given)", type(exc).__name__, exc, exc_info=True)
        raise
    if args.dry_run:
        print(json.dumps({"action": "publish", "dry_run": True, "out": str(root)}, ensure_ascii=False))
        log.info("publish dry run out=%s (nothing was written)", root)
        return 0
    result = run(Config(action="publish", publish=True, settings=settings), Input(root), root)
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    log.info("publish finished action=publish out=%s failures=%d", root, len(result.failures))
    return 1 if result.failures else 0


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
