from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m index")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("scope", nargs="*")
    args = parser.parse_args()
    settings = load_settings(args.config)
    result = run(Config.from_settings(settings, publish=not args.no_publish), Input(Path(args.out), tuple(args.scope)), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
