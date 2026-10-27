from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.policy import resolve_policy
from common.settings import load_settings

from . import Config, Input, run


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m wiki")
    parser.add_argument("--config", default="")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--relative", default="")
    parser.add_argument("--mode", choices=("wiki", "chunks"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    settings = load_settings(args.config) if args.config else None
    policy = next((value for item in args.set if item.startswith("policy=") for value in [item.split("=", 1)[1]]), "standard")
    if settings is not None and args.mode:
        settings.ingest_mode = args.mode
    cfg = Config.from_settings(settings, policy=policy) if settings is not None else Config(mode=args.mode or "wiki", policy=resolve_policy(policy))
    result = run(cfg, Input(Path(args.source), args.relative, args.force), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
