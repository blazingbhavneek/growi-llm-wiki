"""Public publisher phase contract.

The existing publisher modules remain the implementation during migration;
this facade is the only entry point runner code needs for publish operations.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.context import Context
from common.legacy import publish
from common.paths import DataLayout
from common.settings import settings_for_root


@dataclass(frozen=True)
class Config:
    action: str = "assemble"
    publish: bool = False
    settings: Any = None
    allow_unlinked: bool = True
    link_pending: bool = False


@dataclass(frozen=True)
class Input:
    root: Path
    documents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    action: str
    pages: tuple[str, ...]
    failures: tuple[str, ...] = ()


def assemble(root: Path, documents: tuple[str, ...] = ()) -> Result:
    """Materialize missing final pages from the wiki generator state.

    Existing final pages are left byte-for-byte untouched.  The compatibility
    writer already assembles pages during normal builds, so this is deliberately
    conservative until the human/link renderers move here.
    """

    layout = DataLayout(Path(root)).ensure()
    wanted = set(documents)
    written: list[str] = []
    for state in sorted(layout.state.rglob("run.json")) if layout.state.exists() else ():
        document = state.parent.relative_to(layout.state).as_posix()
        if wanted and document not in wanted:
            continue
        generated = state.parent / "wiki"
        target = layout.wiki / document
        if not generated.is_dir() or target.exists() and any(target.glob("*.md")):
            continue
        target.mkdir(parents=True, exist_ok=True)
        for page in sorted(generated.glob("*.md")):
            shutil.copyfile(page, target / page.name)
            written.append((target / page.name).relative_to(layout.root).as_posix())
    return Result("assemble", tuple(written))


def run(
    cfg: Config,
    inp: Input,
    out: Path,
    ctx: Context | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> Result:
    if cfg.action == "assemble":
        with (ctx.stage("publisher", "assemble") if ctx is not None else _null_context()):
            return assemble(out, inp.documents)
    if cfg.settings is None:
        raise ValueError("publisher publish requires settings")
    settings = settings_for_root(cfg.settings, Path(out))
    with (ctx.stage("publisher", cfg.action) if ctx is not None else _null_context()):
        result = publish(
            settings,
            allow_unlinked=cfg.allow_unlinked,
            link_pending=cfg.link_pending,
            on_progress=on_progress,
        )
    return Result(cfg.action, tuple(str(row) for row in result.get("done", [])), tuple(result.get("failures", [])))


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


__all__ = ["Config", "Input", "Result", "assemble", "run"]
