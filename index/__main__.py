from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.settings import load_settings

from . import Config, Input, run

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m index")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("scope", nargs="*")
    args = parser.parse_args()
    settings = load_settings(args.config)
    log.info(
        "index phase started config=%s project=%s out=%s scope=%d publish=%s",
        args.config,
        getattr(settings, "target_name", ""),
        args.out,
        len(args.scope),
        not args.no_publish,
    )
    result = run(Config.from_settings(settings, publish=not args.no_publish), Input(Path(args.out), tuple(args.scope)), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    log.info(
        "index result documents=%d failures=%d scope=%d out=%s",
        len(result.done),
        len(result.failures),
        len(result.scope),
        args.out,
    )
    if result.failures:
        # One ERROR for the exit-1 path; publisher/index.py already logs each failing document.
        log.error(
            "index run finished with %d failures out=%s failures=%s",
            len(result.failures),
            args.out,
            "; ".join(str(item)[:120] for item in result.failures[:5]),
        )
    return 1 if result.failures else 0


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
