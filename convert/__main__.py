from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m convert")
    parser.add_argument("--config", default="", help="existing project INI or config name")
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--relative", default="")
    args = parser.parse_args()
    settings = load_settings(args.config) if args.config else None
    result = run(Config.from_settings(settings) if settings is not None else Config(), Input(Path(args.source), args.relative), Path(args.out))
    print(json.dumps({"raw": str(result.raw_path), "relative": result.relative, "changed": result.changed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
