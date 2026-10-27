"""Execution context shared by standalone phases and the runner."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal, TypedDict


Stage = Literal["convert", "wiki", "linker", "index", "publisher", "runner"]


class Event(TypedDict, total=False):
    stage: Stage | str
    step: str
    document: str
    current: int
    total: int
    elapsed_seconds: float
    calls: int
    error: str


@dataclass
class Context:
    llm: object | None = None
    embedder: object | None = None
    emit: Callable[[Event], None] | None = None
    cancel: Callable[[], bool] | None = None
    calls: dict[str, int] = field(default_factory=dict)

    def event(self, stage: Stage | str, step: str, **fields: object) -> Event:
        event: Event = {"stage": stage, "step": step}
        event.update(fields)  # type: ignore[arg-type]
        if self.emit is not None:
            self.emit(event)
        return event

    def count_call(self, stage: Stage | str, count: int = 1) -> None:
        self.calls[str(stage)] = self.calls.get(str(stage), 0) + count

    def cancelled(self) -> bool:
        return bool(self.cancel and self.cancel())

    @contextmanager
    def stage(self, stage: Stage | str, document: str = "") -> Iterator[None]:
        started = time.monotonic()
        self.event(stage, "start", document=document)
        try:
            yield
        except BaseException as exc:
            self.event(stage, "error", document=document, error=f"{type(exc).__name__}: {exc}", elapsed_seconds=round(time.monotonic() - started, 3))
            raise
        else:
            self.event(stage, "done", document=document, elapsed_seconds=round(time.monotonic() - started, 3), calls=self.calls.get(str(stage), 0))


__all__ = ["Context", "Event", "Stage"]
