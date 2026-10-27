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
        "リンク処理を開始します: コマンド=%s、設定=%s、出力先=%s、リンク方式=%s、処理方式=%s",
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
            "リンク情報の状態: 出力先=%s、方式=%s、文書数=%s、分割部分数=%s、リンク数=%s",
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
        "リンク処理の結果: 更新文書数=%d、処理方式=%s、呼び出し数=%d、出力先=%s、プロジェクト=%s",
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
