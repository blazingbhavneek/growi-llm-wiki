from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run, status


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m linker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", choices=("legacy", "neo"))
    parser.add_argument("--policy", choices=("standard", "fast"), default="standard")
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(Path(args.out)), ensure_ascii=False))
        return 0
    settings = load_settings(args.config)
    if args.mode:
        settings.wiki_linker_mode = args.mode
    result = run(Config.from_settings(settings, policy=args.policy), Input(Path(args.out)), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
