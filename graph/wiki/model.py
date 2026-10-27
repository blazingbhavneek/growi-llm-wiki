"""The single seam through which bounded structured model calls happen.

``ChatModelPort`` wraps the shared chat client, so the pipeline needs no new
model client. Callers can inject any object with the same
``structured`` coroutine, which is why nothing here imports a test helper.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Protocol, Sequence, runtime_checkable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from .config import WikiConfig


@runtime_checkable
class ModelPort(Protocol):
    """One bounded structured call. Implementations must respect timeouts."""

    name: str
    provider: str

    async def structured(
        self,
        schema: type[BaseModel],
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> BaseModel:  # pragma: no cover - protocol declaration
        ...

    async def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> str:  # pragma: no cover - protocol declaration
        ...


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


# TEMPORARILY DISABLED: restore this default together with the max_tokens
# bindings below when output limits should be enforced again.
# DEFAULT_MAX_OUTPUT_TOKENS = 16000

class ChatModelPort:
    """OpenAI-compatible chat endpoint via langchain structured output."""

    def __init__(
        self,
        config: WikiConfig,
        *,
        llm: Any | None = None,
        gate: Any | None = None,
        role: str = "writer",
    ) -> None:
        from graph.clients.chat import make_llm

        self.config = config
        self.gate = gate
        self.role = role
        self.llm = llm if llm is not None else make_llm(
            model=config.chat_model,
            base_url=config.chat_base_url,
            api_key=config.chat_api_key,
            temperature=config.temperature,
            timeout=config.request_timeout,
        )
        self.name = config.chat_model
        self.provider = config.chat_base_url

    async def structured(
        self,
        schema: type[BaseModel],
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> BaseModel:
        from graph.clients.chat import structured_ainvoke

        async def call() -> BaseModel:
            return await structured_ainvoke(
                self.llm, schema, list(messages), max_output_tokens=None,
                temperature=self.config.temperature if temperature is None else temperature,
            )

        return await self._run(call)

    async def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        _enforce_max_output_tokens: bool = False,
    ) -> str:
        """One bounded plain-text completion; the caller validates the content."""

        async def call() -> str:
            kwargs: dict[str, Any] = {}
            # Standard calls intentionally leave max_tokens unset; fast writer
            # opts in through _enforce_max_output_tokens.
            if _enforce_max_output_tokens and max_output_tokens is not None:
                kwargs["max_tokens"] = max(1, int(max_output_tokens))
            kwargs["temperature"] = self.config.temperature if temperature is None else temperature
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": self.config.text_thinking}
            }
            llm = self.llm.bind(**kwargs)
            reply = await asyncio.wait_for(
                llm.ainvoke(list(messages)), timeout=self.config.request_timeout
            )
            content = getattr(reply, "content", reply)
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            return _THINK_RE.sub("", str(content))

        return await self._run(call)

    async def _run(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self.gate is None:
            return await operation()
        return await self.gate.run(operation, role=self.role)


class ModelPair:
    """Writer and judge ports sharing one role-batched model-call gate.

    The pair deliberately delegates the generic ModelPort methods to the
    writer.  Existing planner/linker code therefore needs no role plumbing;
    explicit judge call sites select ``.judge``.
    """

    def __init__(self, writer: ModelPort, judge: ModelPort) -> None:
        self.writer = writer
        self.judge = judge
        self.name = judge.name
        self.provider = judge.provider

    async def structured(self, *args: Any, **kwargs: Any) -> BaseModel:
        return await self.writer.structured(*args, **kwargs)

    async def text(self, *args: Any, **kwargs: Any) -> str:
        return await self.writer.text(*args, **kwargs)


def writer_model(model: Any) -> Any:
    return getattr(model, "writer", model)


def judge_model(model: Any) -> Any:
    return getattr(model, "judge", model)
