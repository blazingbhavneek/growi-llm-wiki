"""OpenAI-compatible chat helpers shared by wiki and linker calls."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel


def make_llm(model: str, base_url: str, api_key: str, temperature: float = 0.7, timeout: int = 300) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        reasoning_effort="low",
    )


# Models write LaTeX into JSON strings unescaped ("\hat{m}_t"); the strict parser rejects
# it and a math-heavy chunk then fails every retry. After a failure only, keep such
# backslashes literal. Kept as is: \\, \", \/, \uXXXX and a lone \b \f \n \r \t.
_ESCAPE_RE = re.compile(r'\\(\\|"|/|u[0-9a-fA-F]{4}|[A-Za-z]+|.)', re.S)


def _literal_backslashes(text: str) -> str:
    def fix(match: re.Match[str]) -> str:
        body = match.group(1)
        valid = body in {"\\", '"', "/", "b", "f", "n", "r", "t"} or (
            len(body) == 5 and body[0] == "u" and all(c in "0123456789abcdefABCDEF" for c in body[1:])
        )
        return match.group(0) if valid else "\\\\" + body

    return _ESCAPE_RE.sub(fix, text)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        repaired = _literal_backslashes(text)
        if repaired == text:
            raise
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            raise error from None


def extract_json_from_text(text: str) -> Any:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if match:
        text = match.group(1).strip()
    try:
        return _loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return _loads(text[start : end + 1])
        raise ValueError("could not extract valid JSON from model response")


async def structured_ainvoke(
    llm: Any,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None = None,
    *,
    thinking: bool = True,
    temperature: float | None = None,
) -> BaseModel:
    """One bounded structured call with the same fallback policy the chunk writer used.

    Thinking is enabled by default so structured decisions retain the model's
    full reasoning capability.
    """

    request_timeout = getattr(llm, "request_timeout", None)
    try:
        hard_timeout = float(request_timeout) if request_timeout is not None else None
    except (TypeError, ValueError):
        hard_timeout = None

    async def invoke(runnable: Any, payload: list[Any]) -> Any:
        operation = runnable.ainvoke(payload)
        if hard_timeout is None or hard_timeout <= 0:
            return await operation
        return await asyncio.wait_for(operation, timeout=hard_timeout)

    limits: dict[str, Any] = {}
    # TEMPORARILY DISABLED: restore this cap when large structured responses
    # should be bounded again.
    # if max_output_tokens is not None:
    #     limits["max_tokens"] = max_output_tokens
    if temperature is not None:
        limits["temperature"] = temperature
    call_llm = llm.bind(**limits, extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})

    try:
        # Output-token limiting is temporarily disabled above. Thinking stays
        # at the server default on this attempt, as before.
        structured = llm.with_structured_output(schema_cls, **limits)
        result = await invoke(structured, messages)
        if isinstance(result, schema_cls):
            return result
        return schema_cls.model_validate(result)
    except Exception as structured_error:
        # A timed-out prompt will time out again; re-sending it twice more only
        # triples the loss. Let the caller's retry policy decide.
        if isinstance(structured_error, TimeoutError) or "Timeout" in type(structured_error).__name__:
            raise
        schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
        last_error: Exception = structured_error
        # A streaming model can still return malformed JSON after the
        # structured-output call fails. Give it two bounded, concise requests;
        # do not create an unbounded retry loop.
        for retry in range(2):
            fallback_messages = list(messages)
            fallback_messages.append(
                HumanMessage(
                    content=(
                        (
                            "The previous response was invalid. Return the smallest "
                            "valid JSON object that satisfies this schema. Use empty "
                            "arrays or strings when uncertain."
                            if retry
                            else "Return ONLY valid JSON matching this JSON Schema. "
                            "Be concise. Do not include extra prose."
                        )
                        + "\n\n"
                        + schema_json
                    )
                )
            )
            try:
                raw = await invoke(call_llm, fallback_messages)
                text = raw.content if hasattr(raw, "content") else str(raw)
                return schema_cls.model_validate(extract_json_from_text(str(text)))
            except Exception as fallback_error:
                last_error = fallback_error
        raise last_error from structured_error


__all__ = ["extract_json_from_text", "make_llm", "structured_ainvoke"]
