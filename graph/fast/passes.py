"""Work-conserving concurrency for fast-mode model passes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TypeVar


Input = TypeVar("Input")
Output = TypeVar("Output")


async def bounded_map(
    items: Sequence[Input],
    limit: int,
    operation: Callable[[Input], Awaitable[Output]],
) -> list[Output]:
    """Run one whole role pass with a sliding, bounded in-flight window."""

    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def one(item: Input) -> Output:
        async with semaphore:
            return await operation(item)

    return await asyncio.gather(*(one(item) for item in items))


async def bounded_as_completed(
    items: Sequence[Input],
    limit: int,
    operation: Callable[[Input], Awaitable[Output]],
) -> AsyncIterator[Output]:
    """Yield results promptly while continuously refilling the bounded window."""

    iterator = iter(items)
    pending: set[asyncio.Task[Output]] = set()

    def fill() -> None:
        while len(pending) < max(1, int(limit)):
            try:
                item = next(iterator)
            except StopIteration:
                return
            pending.add(asyncio.create_task(operation(item)))

    fill()
    try:
        while pending:
            done, pending_now = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            pending = set(pending_now)
            fill()
            for task in done:
                yield task.result()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["bounded_as_completed", "bounded_map"]
