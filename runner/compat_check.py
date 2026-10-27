"""G2: an unchanged project must be a no-op for this code.

    python -m runner.compat_check --project configs/<project>.ini [--data-root data]

Copies ``<data_root>/<target_name>`` to a temporary directory and runs ``sync``,
``build all`` and ``index --no-publish`` there through the normal CLI.  Every LLM,
embedding, parser and GROWI write call fails loudly and is recorded; reads from the
real mount and GROWI are allowed.  Exits 1 on any recorded call or changed byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

COMMANDS = ("sync", "build all", "index --no-publish")


def tree_hash(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tree_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(name for name in set(before) & set(after) if before[name] != after[name]),
    }


def _blocked(calls: list[str], name: str):
    # A plain function: it raises at call time, so it also stops async methods and
    # async generators before they reach the network.
    def call(*_args, **_kwargs):
        calls.append(name)
        raise RuntimeError(f"compat check: {name} called on an unchanged project")

    return call


def fail_fast(calls: list[str]) -> ExitStack:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    from graph.growi.client import GrowiClient

    targets = [
        *((ChatOpenAI, name, "llm") for name in ("_generate", "_agenerate", "_stream", "_astream")),
        *((OpenAIEmbeddings, name, "embed") for name in ("embed_documents", "embed_query", "aembed_documents", "aembed_query")),
        *((GrowiClient, name, f"growi.{name}") for name in ("create_page", "update_page", "rename_page", "delete_pages", "upload_attachment")),
    ]
    stack = ExitStack()
    for owner, attribute, label in targets:
        stack.enter_context(mock.patch.object(owner, attribute, _blocked(calls, label)))
    # The parser function is imported by name, so patch every module that holds it.
    for module in ("graph.workspace.parser_client", "graph.workspace.convert", "publisher.pipeline"):
        stack.enter_context(mock.patch(f"{module}.parse_document", _blocked(calls, "parser")))
    return stack


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m runner.compat_check", description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True, help="config name from configs/ or INI path")
    parser.add_argument("--data-root", default="", help="override the INI data_root")
    parser.add_argument("--command", action="append", help=f"CLI command to run (repeatable; default {list(COMMANDS)})")
    args = parser.parse_args()

    from common.settings import load_settings
    from runner import cli

    settings = load_settings(args.project)
    data_root = Path(args.data_root or settings.data_root)
    source = data_root / settings.target_name
    if not source.is_dir():
        parser.error(f"project data not found: {source}")
    calls: list[str] = []
    report: dict[str, object] = {"project": str(source), "commands": {}}
    with tempfile.TemporaryDirectory(prefix="llm-wiki-compat-") as temporary:
        copy = Path(temporary) / settings.target_name
        shutil.copytree(source, copy, symlinks=True)
        before = tree_hash(copy)
        with fail_fast(calls):
            for command in args.command or COMMANDS:
                argv = ["main.py", *shlex.split(command), "--project", args.project, "--data-root", temporary]
                with mock.patch.object(sys, "argv", argv):
                    try:
                        report["commands"][command] = cli.main()
                    except BaseException as exc:  # noqa: BLE001 - report every command
                        report["commands"][command] = f"{type(exc).__name__}: {exc}"[:500]
        report["calls"] = calls
        report["tree"] = tree_diff(before, tree_hash(copy))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    changed = any(report["tree"].values())
    return 1 if calls or changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
