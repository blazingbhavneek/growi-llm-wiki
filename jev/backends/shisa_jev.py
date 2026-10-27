"""Shisa DE-2 adapter for the existing Jev engine contract."""

import json
import math
from concurrent.futures import ThreadPoolExecutor

from ..types import JevResult, JevUnavailable
from . import Prepared


class ShisaJevBackend:
    """Score Jev questions through a hosted Shisa DE-2 OpenAI endpoint.

    Each question is independent, so one bounded pool is shared across the whole
    engine batch.  This gives vLLM concurrent requests without multiplying the
    configured concurrency across states and questions.
    """

    name = "shisa"
    pads_batches = False

    def __init__(self, config):
        if not config.base_url:
            raise JevUnavailable("Shisa backend requires WIKI_JEV_BASE_URL")
        if not config.model:
            raise JevUnavailable("Shisa backend requires WIKI_JEV_MODEL")
        try:
            from shisa_de import DecisionModel, calibration_for
        except ImportError as exc:
            raise JevUnavailable(
                "Shisa backend requires shisa-de (uv pip install --python .venv/bin/python shisa-de)"
            ) from exc

        self.config = config
        self._concurrency = max(1, int(getattr(config, "http_concurrency", 8)))
        self._pool = ThreadPoolExecutor(
            max_workers=self._concurrency, thread_name_prefix="jev-shisa",
        )
        self._client = DecisionModel.from_endpoint(
            config.base_url,
            model=config.model,
            tokenizer=config.tokenizer or "shisa-ai/shisa-de-2-FP8-dynamic",
            api_key=config.api_key or "",
            timeout=float(config.timeout),
            max_workers=1,
            family="de2",
            policy="repeat",
            # This is the official FP8 quantization of DE-2, so retain DE-2's
            # probability calibration used by the linker's configured thresholds.
            calibration=calibration_for("de2"),
        )

    @staticmethod
    def _question(question):
        from shisa_de import Choice, Noul, Score

        kind = question.kind or "noul"
        options = question.options
        if kind == "noul":
            criteria = None
            if isinstance(options, dict):
                criteria = {
                    "yes": str(options.get("true", options.get("yes", "Yes"))),
                    "no": str(options.get("false", options.get("no", "No"))),
                }
            return Noul(question.text, criteria=criteria)
        if kind == "choice":
            if isinstance(options, dict):
                criteria = {str(key): value for key, value in options.items()}
            elif options:
                criteria = {str(value): None for value in options}
            else:
                raise ValueError("choice questions need criteria")
            return Choice(question.text, criteria=criteria)
        if kind == "score":
            levels = list(options) if options else []
            return Score(question.text, criteria=levels)
        raise NotImplementedError(f"Shisa backend does not support question kind {kind!r}")

    def prepare(self, request):
        self._question(request.question)
        state = request.state
        raw = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        return Prepared(request, max(1, len(raw.encode()) // 2), None)

    @staticmethod
    def count_tokens(text):
        return max(1, len(str(text).encode()) // 2)

    def run(self, batch):
        if not batch:
            return []
        if len(batch) == 1:
            return [self._score_one(batch[0])]
        return list(self._pool.map(self._score_one, batch))

    def _score_one(self, prepared):
        request = prepared.request
        try:
            decision = self._client.decide(
                request.state,
                {"answer": self._question(request.question)},
                probability=True,
            )
            answer = decision.answers["answer"]
            return self._result(request.question, answer, decision.usage)
        except Exception as exc:
            raise JevUnavailable(f"Shisa DE-2 request failed: {exc}") from exc

    @staticmethod
    def _result(question, answer, usage):
        if question.kind == "noul":
            p_yes = float(answer.noul)
            probabilities = {"false": 1.0 - p_yes, "true": p_yes}
            chosen = "true" if p_yes >= 0.5 else "false"
        else:
            probabilities = {str(key): float(value)
                             for key, value in answer.probabilities.items()}
            chosen = str(answer.choice if question.kind == "choice" else answer.level)
        top = max(probabilities.values())
        entropy = -sum(p * math.log(p) for p in probabilities.values() if p > 0)
        concentration = 1.0 if len(probabilities) < 2 else max(
            0.0, min(1.0, 1.0 - entropy / math.log(len(probabilities))),
        )
        tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        return JevResult(
            question.key,
            chosen,
            probabilities,
            top,
            concentration,
            int(tokens or 0),
        )

    def close(self):
        self._pool.shutdown(wait=True)
        self._client.close()
