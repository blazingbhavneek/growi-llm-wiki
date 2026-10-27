"""Small, phase neutral adapters for model and embedding clients.

The existing clients remain the source of truth.  Keeping this import lazy is
intentional: the standalone phase commands can still be used for passthrough
and offline work when optional model dependencies are not installed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping, TypeVar

log = logging.getLogger(__name__)

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
    name = getattr(schema, "__name__", schema)
    try:
        value = await structured_ainvoke(llm, schema, list(messages))
    except Exception as exc:
        log.error("モデルから指定形式の回答を取得できません: 形式=%s、エラー=%s: %s", name, type(exc).__name__, exc, exc_info=True)
        raise
    if validate is None:
        return value
    try:
        return validate(value)
    except Exception as exc:
        log.error("モデルの回答が検証に通りません: 形式=%s、エラー=%s: %s", name, type(exc).__name__, exc, exc_info=True)
        raise


async def embed(embedder: Any, texts: list[str], *, ctx: Any = None) -> list[list[float]]:
    """Delegate embeddings to the configured embedder with call accounting."""

    if ctx is not None:
        ctx.count_call("embed")
    try:
        result = await embedder.aembed_documents(texts)
    except Exception as exc:
        log.error("検索用ベクトルを作成できません: テキスト数=%d、エラー=%s: %s", len(texts), type(exc).__name__, exc, exc_info=True)
        raise
    return [list(vector) for vector in result]


__all__ = ["embed", "structured_call"]
