from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run, status

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m linker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", choices=("legacy", "neo"))
    parser.add_argument("--policy", choices=("standard", "fast"), default="standard")
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    args = parser.parse_args()
    log.info(
        "linker command started command=%s config=%s out=%s mode=%s policy=%s",
        args.command,
        args.config,
        args.out,
        args.mode or "(from config)",
        args.policy,
    )
    if args.command == "status":
        catalog = status(Path(args.out))
        print(json.dumps(catalog, ensure_ascii=False))
        log.info(
            "linker catalog status out=%s mode=%s documents=%s chunks=%s edges=%s",
            args.out,
            catalog.get("mode"),
            catalog.get("documents"),
            catalog.get("chunks"),
            catalog.get("edges"),
        )
        return 0
    settings = load_settings(args.config)
    if args.mode:
        settings.wiki_linker_mode = args.mode
    result = run(Config.from_settings(settings, policy=args.policy), Input(Path(args.out)), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    log.info(
        "linker result touched=%d policy=%s calls=%d out=%s project=%s",
        len(result.touched_documents),
        result.policy,
        result.calls,
        args.out,
        getattr(settings, "target_name", ""),
    )
    return 0


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
