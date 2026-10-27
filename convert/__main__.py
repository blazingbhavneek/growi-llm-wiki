from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m convert")
    parser.add_argument("--config", default="", help="existing project INI or config name")
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--relative", default="")
    args = parser.parse_args()
    settings = load_settings(args.config) if args.config else None
    log.info(
        "convert phase started config=%s project=%s source=%s out=%s relative=%s",
        args.config or "(no config given)",
        getattr(settings, "target_name", "") or "(no project selected)",
        args.source,
        args.out,
        args.relative,
    )
    result = run(Config.from_settings(settings) if settings is not None else Config(), Input(Path(args.source), args.relative), Path(args.out))
    print(json.dumps({"raw": str(result.raw_path), "relative": result.relative, "changed": result.changed}, ensure_ascii=False))
    log.info(
        "convert result raw=%s relative=%s changed=%s",
        result.raw_path,
        result.relative,
        result.changed,
    )
    return 0


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
