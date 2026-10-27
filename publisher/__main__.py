from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.settings import load_settings

from .phase import Config, Input, assemble, run


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
        return 0
    if not args.config:
        parser.error("publish requires --config")
    settings = load_settings(args.config)
    if args.dry_run:
        print(json.dumps({"action": "publish", "dry_run": True, "out": str(root)}, ensure_ascii=False))
        return 0
    result = run(Config(action="publish", publish=True, settings=settings), Input(root), root)
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
