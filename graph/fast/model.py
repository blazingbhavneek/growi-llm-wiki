"""Fast-only writer/judge clients with one process-wide alternating role gate."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from graph.wiki.config import WikiConfig
from graph.wiki.model import ChatModelPort, ModelPair


JUDGE_MAX_OUTPUT_TOKENS = 16_000
FAST_WRITER_MAX_OUTPUT_TOKENS = 64_000


class _OutputCappedLlm:
    """Apply a hard output cap to both structured and JSON-fallback calls."""

    def __init__(self, llm: Any, limit: int) -> None:
        self.llm = llm
        self.limit = limit
        self.request_timeout = getattr(llm, "request_timeout", None)

    def bind(self, **kwargs: Any) -> Any:
        kwargs["max_tokens"] = min(
            self.limit, int(kwargs.get("max_tokens") or self.limit)
        )
        return self.llm.bind(**kwargs)

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any) -> Any:
        kwargs["max_tokens"] = min(
            self.limit, int(kwargs.get("max_tokens") or self.limit)
        )
        return self.llm.with_structured_output(schema, **kwargs)


class FastModelPort(ChatModelPort):
    """Shared fast client behavior for writer and judge endpoint instances."""

    async def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Cap the writer's total completion (reasoning plus page output)."""

        writer_limit = (
            min(
                FAST_WRITER_MAX_OUTPUT_TOKENS,
                max(1, int(max_output_tokens or FAST_WRITER_MAX_OUTPUT_TOKENS)),
            )
            if self.role == "writer"
            else max_output_tokens
        )
        return await super().text(
            messages,
            max_output_tokens=writer_limit,
            temperature=temperature,
            _enforce_max_output_tokens=self.role == "writer",
        )

    async def structured(
        self,
        schema: type[BaseModel],
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> BaseModel:
        from graph.clients.chat import structured_ainvoke

        requested = int(max_output_tokens or JUDGE_MAX_OUTPUT_TOKENS)
        limit = min(JUDGE_MAX_OUTPUT_TOKENS, max(1, requested))

        async def call() -> BaseModel:
            return await structured_ainvoke(
                _OutputCappedLlm(self.llm, limit),
                schema,
                list(messages),
                temperature=(
                    self.config.temperature if temperature is None else temperature
                ),
            )

        return await self._run(call)


class FastRoleGate:
    """Bound concurrency and alternate writer/judge batches across event loops."""

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self._lock = threading.Lock()
        self._waiting = {"writer": 0, "judge": 0}
        self._active = 0
        self._active_role: str | None = None
        self._last_role: str | None = None

    def _preferred_role(self) -> str | None:
        writers = self._waiting["writer"]
        judges = self._waiting["judge"]
        if writers and judges:
            return "judge" if self._last_role == "writer" else "writer"
        if writers:
            return "writer"
        if judges:
            return "judge"
        return None

    async def run(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        role: str = "writer",
    ) -> Any:
        if role not in self._waiting:
            raise ValueError(f"unknown model role: {role}")
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.001)
        self._waiting[role] += 1
        self._lock.release()
        registered = True
        try:
            while True:
                admitted = False
                if self._lock.acquire(blocking=False):
                    try:
                        if self._active_role == role:
                            admitted = self._active < self.max_concurrency
                        elif self._active_role is None:
                            admitted = self._preferred_role() == role
                        if admitted:
                            self._waiting[role] -= 1
                            self._active += 1
                            self._active_role = role
                            registered = False
                    finally:
                        self._lock.release()
                if admitted:
                    break
                await asyncio.sleep(0.001)
        except BaseException:
            if registered:
                with self._lock:
                    self._waiting[role] -= 1
            raise

        try:
            return await operation()
        finally:
            with self._lock:
                self._active -= 1
                if self._active == 0:
                    self._last_role = self._active_role
                    self._active_role = None


class FastModelPair(ModelPair):
    """Use the judge as the default client; writer use must be explicit."""

    async def structured(self, *args: Any, **kwargs: Any) -> BaseModel:
        return await self.judge.structured(*args, **kwargs)

    async def text(self, *args: Any, **kwargs: Any) -> str:
        return await self.judge.text(*args, **kwargs)


_GATES: dict[tuple[str, str, str, str, int], FastRoleGate] = {}
_GATES_LOCK = threading.Lock()


def _gate(
    writer_config: WikiConfig,
    judge_config: WikiConfig,
    concurrency: int,
) -> FastRoleGate:
    key = (
        writer_config.chat_base_url.rstrip("/"),
        writer_config.chat_model,
        judge_config.chat_base_url.rstrip("/"),
        judge_config.chat_model,
        concurrency,
    )
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = FastRoleGate(concurrency)
            _GATES[key] = gate
        return gate


def model_pair(config: WikiConfig) -> ModelPair:
    """Build the fast writer/judge pair; standard policy never imports this module."""

    judge_config = config.model_copy(
        update={
            "chat_base_url": config.judge_base_url or config.chat_base_url,
            "chat_api_key": config.judge_api_key or config.chat_api_key,
            "chat_model": config.judge_model or config.chat_model,
        }
    )
    # An unconfigured writer is the judge, not the chat default.
    writer_config = config.model_copy(
        update={
            "chat_base_url": config.writer_base_url or judge_config.chat_base_url,
            "chat_api_key": config.writer_api_key or judge_config.chat_api_key,
            "chat_model": config.writer_model or judge_config.chat_model,
        }
    )
    concurrency = max(1, int(config.rewrite_concurrency))
    gate = _gate(writer_config, judge_config, concurrency)
    return FastModelPair(
        FastModelPort(writer_config, gate=gate, role="writer"),
        FastModelPort(judge_config, gate=gate, role="judge"),
    )


__all__ = [
    "FastModelPort",
    "FastModelPair",
    "FastRoleGate",
    "JUDGE_MAX_OUTPUT_TOKENS",
    "model_pair",
]
