"""Small, phase neutral adapters for model and embedding clients.

The existing clients remain the source of truth.  Keeping this import lazy is
intentional: the standalone phase commands can still be used for passthrough
and offline work when optional model dependencies are not installed.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, TypeVar

T = TypeVar("T")


async def structured_call(
    llm: Any,
    schema: Any,
    messages: Iterable[Mapping[str, str]],
    *,
    validate: Callable[[T], T] | None = None,
    attempts: int = 3,
    ctx: Any = None,
) -> T:
    """Call the repository's structured client and optionally validate it.

    ``graph.clients.chat`` owns retry and provider details, so this adapter
    does not create a second implementation of those rules.  The validation
    hook is useful to phase code that needs a bounded repair loop.
    """

    if ctx is not None:
        ctx.count_call("llm")
    from graph.clients.chat import structured_ainvoke

    # ``structured_ainvoke`` already owns its bounded fallback attempts.  The
    # argument is retained here so phase callers can expose one consistent
    # contract while the compatibility client remains the implementation.
    del attempts
    value = await structured_ainvoke(llm, schema, list(messages))
    return validate(value) if validate is not None else value


async def embed(embedder: Any, texts: list[str], *, ctx: Any = None) -> list[list[float]]:
    """Delegate embeddings to the configured embedder with call accounting."""

    if ctx is not None:
        ctx.count_call("embed")
    result = await embedder.aembed_documents(texts)
    return [list(vector) for vector in result]


__all__ = ["embed", "structured_call"]
