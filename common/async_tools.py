"""Small standard-library async adapters shared by phase facades."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine

log = logging.getLogger(__name__)


def run_async_blocking(awaitable: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:  # pragma: no cover - adapter boundary
            error.append(exc)

    thread = threading.Thread(target=runner)
    try:
        thread.start()
    except (RuntimeError, OSError) as exc:
        # "can't start new thread": thread or file limit reached, or the process is out of memory.
        log.error("async worker thread could not start error=%s: %s (thread or file limit, or out of memory)", type(exc).__name__, exc, exc_info=True)
        raise
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


__all__ = ["run_async_blocking"]
