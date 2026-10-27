"""The only place the phase facades are composed."""

from __future__ import annotations

from typing import Any


def convert(settings: Any, *, on_progress: Any = None) -> dict[str, Any]:
    from convert import run_project

    return run_project(settings, on_progress=on_progress)


def build(settings: Any, phase: str = "all", *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from common.legacy import build_all
    from linker import run_project as run_linker
    from wiki import run_project as run_wiki

    # "all" links only the documents whose wiki build succeeded (historical build_raw).
    runners = {"wiki": run_wiki, "link": run_linker, "all": build_all}
    if phase not in runners:
        raise ValueError("phase must be wiki, link, or all")
    return runners[phase](settings, only=only, force=force, on_progress=on_progress)


def index(settings: Any, *, only: list[str] | None = None, publish: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from index import run_project

    return run_project(settings, only=only, publish=publish, on_progress=on_progress)


__all__ = ["build", "convert", "index"]
