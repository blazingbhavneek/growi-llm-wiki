"""Small OpenAI-compatible embedding client used by the linker."""

from __future__ import annotations

from typing import Any


class Embedder:
    def __init__(self, settings: Any) -> None:
        from langchain_openai import OpenAIEmbeddings

        if str(getattr(settings, "embed_backend", "server")) != "server":
            raise ValueError("the linker requires WIKI_EMBED_BACKEND=server")
        self.model_name = str(settings.embed_model)
        self._client = OpenAIEmbeddings(
            model=self.model_name,
            base_url=str(settings.embed_base_url),
            api_key=str(getattr(settings, "embed_api_key", "local")),
            check_embedding_ctx_length=False,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)


__all__ = ["Embedder"]
