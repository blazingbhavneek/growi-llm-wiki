"""The only place the phase facades are composed."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def convert(settings: Any, *, on_progress: Any = None) -> dict[str, Any]:
    from convert import run_project

    log.info("phase started phase=convert project=%s", getattr(settings, "target_name", ""))
    return run_project(settings, on_progress=on_progress)


def build(settings: Any, phase: str = "all", *, only: list[str] | None = None, force: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from common.legacy import build_all
    from linker import run_project as run_linker
    from wiki import run_project as run_wiki

    # "all" links only the documents whose wiki build succeeded (historical build_raw).
    runners = {"wiki": run_wiki, "link": run_linker, "all": build_all}
    if phase not in runners:
        log.error(
            "build phase value is not a known phase phase=%s project=%s error=phase must be wiki, link, or all",
            phase,
            getattr(settings, "target_name", ""),
        )
        raise ValueError("phase must be wiki, link, or all")
    log.info(
        "phase started phase=%s project=%s documents=%d force=%s",
        phase,
        getattr(settings, "target_name", ""),
        len(only or ()),
        force,
    )
    return runners[phase](settings, only=only, force=force, on_progress=on_progress)


def index(settings: Any, *, only: list[str] | None = None, publish: bool = False, on_progress: Any = None) -> dict[str, Any]:
    from index import run_project

    log.info(
        "phase started phase=index project=%s documents=%d publish=%s",
        getattr(settings, "target_name", ""),
        len(only or ()),
        publish,
    )
    return run_project(settings, only=only, publish=publish, on_progress=on_progress)


__all__ = ["build", "convert", "index"]
