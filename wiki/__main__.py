from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.policy import resolve_policy
from common.settings import load_settings

from . import Config, Input, run

log = logging.getLogger(__name__)


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
    for item in args.set:
        name, _separator, value = item.partition("=")
        if name != "policy" or not value.strip():
            log.warning(
                "--set entry is ignored here setting=%s config=%s reason=this entry point reads the policy key only (the value was not applied)",
                item,
                args.config or "(no config given)",
            )
    settings = load_settings(args.config) if args.config else None
    log.info(
        "wiki phase started config=%s project=%s source=%s out=%s mode=%s force=%s",
        args.config or "(no config given)",
        getattr(settings, "target_name", "") or "(no project selected)",
        args.source,
        args.out,
        args.mode or "wiki",
        args.force,
    )
    policy = next((value for item in args.set if item.startswith("policy=") for value in [item.split("=", 1)[1]]), "standard")
    if settings is not None and args.mode:
        settings.ingest_mode = args.mode
    cfg = Config.from_settings(settings, policy=policy) if settings is not None else Config(mode=args.mode or "wiki", policy=resolve_policy(policy))
    result = run(cfg, Input(Path(args.source), args.relative, args.force), Path(args.out))
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    log.info(
        "wiki result document=%s raw=%s tier=%s reason=%s pages=%d policy=%s",
        result.document,
        result.raw,
        result.tier,
        result.reason,
        len(result.changed_pages),
        result.policy,
    )
    return 0


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
