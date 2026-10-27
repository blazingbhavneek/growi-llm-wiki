import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..types import JevResult, JevUnavailable
from . import Prepared

MAX_QUESTIONS = 64  # /v1/systemone rejects requests outside 1..64 questions


class HostedBackend:
    """Any server with the vanilla ``POST /v1/systemone`` API (e.g. stock llm2jev).

    Stock llm2jev fronting an sglang/vllm engine, per the JPT-4B card (the engine holds
    the weights, llm2jev reads the option logprobs off it)::

        python -m sglang.launch_server --model-path kirp/jpt-4b --port 30000 \
          --context-length 32768 --mamba-scheduler-strategy extra_buffer &
        llm2jev --model kirp/jpt-4b --backend sglang --url http://127.0.0.1:30000 \
          --port 8080 --temperature 1.036

    then ``WIKI_JEV_BACKEND=hosted`` and ``WIKI_JEV_BASE_URL=http://127.0.0.1:8080``. The
    request ``model`` is omitted: the server serves the single model it was started with.

    One HTTP call carries exactly one state (``{"state", "questions"}``, the plain
    contract, no batch wrapping); states fly concurrently up to ``http_concurrency``
    (default 20). For the custom batched scorer use the ``llm2jev`` backend instead.
    """

    name = "hosted"
    pads_batches = False  # one state per call; the pool provides the concurrency

    def __init__(self, config, transport=None):
        self.config = config
        base = (config.base_url or "").rstrip("/")
        self.url = base if base.endswith("/systemone") else base + "/v1/systemone"
        self._client = httpx.Client(timeout=config.timeout, transport=transport)
        self._concurrency = max(1, int(getattr(config, "http_concurrency", 20)))
        self._pool = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="jev-hosted")

    @staticmethod
    def _question(question):
        kind = question.kind or "noul"
        text, opts = question.text, question.options
        if kind == "noul":
            criteria = {}
            if isinstance(opts, dict):
                criteria = {key: value for key, value in opts.items() if key in ("true", "false")}
            return {"type": "noul", "instructions": text, "criteria": criteria}
        if kind == "choice":
            if isinstance(opts, dict):
                criteria = {str(key): value for key, value in opts.items()}
            elif opts:
                criteria = {str(name): None for name in opts}
            else:
                raise ValueError("choice questions need criteria")
            if not 2 <= len(criteria) <= 255:
                raise ValueError("choice needs 2..255 criteria")
            return {"type": "choice", "instructions": text, "criteria": criteria}
        if kind == "score":
            levels = list(opts) if opts else []
            if not 2 <= len(levels) <= 255:
                raise ValueError("score needs 2..255 levels")
            return {"type": "score", "instructions": text, "criteria": levels}
        raise NotImplementedError(f"hosted backend supports noul, choice and score, got {kind!r}")

    def prepare(self, request):
        self._question(request.question)  # fail fast on unsupported kinds/criteria
        state = request.state
        cost = len(state.encode()) // 2 if isinstance(state, str) else len(json.dumps(state)) // 2
        return Prepared(request, cost, None)

    def count_tokens(self, text): return len(text.encode()) // 2

    def run(self, batch):
        if not batch: return []
        groups = {}
        for prepared in batch: groups.setdefault(prepared.request.state_id, []).append(prepared)
        jobs = [group[start:start + MAX_QUESTIONS]
                for group in groups.values() for start in range(0, len(group), MAX_QUESTIONS)]
        # One HTTP call per state chunk; httpx.Client is thread-safe, so
        # states fly concurrently up to the configured server capacity.
        scored = self._map(self._score_state, jobs)
        out = {}
        for chunk, results in zip(jobs, scored):
            for p, result in zip(chunk, results): out[id(p)] = result
        return [out[id(p)] for p in batch]

    def _map(self, func, items):
        if len(items) == 1: return [func(items[0])]
        return list(self._pool.map(func, items))

    def _send(self, payload):
        headers = {"Content-Type": "application/json"}
        if self.config.api_key: headers["Authorization"] = f"Bearer {self.config.api_key}"
        for attempt in range(2):
            try: response = self._client.post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                if not attempt: continue
                raise JevUnavailable(f"hosted request failed: {exc}") from exc
            if response.status_code >= 500 and not attempt: continue
            return response
        raise JevUnavailable("hosted request failed after retry")

    @staticmethod
    def _share(usage, count):
        total = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        return int(total) // count if isinstance(total, (int, float)) and total else 0

    def _score_state(self, chunk):
        questions = {f"q{i}": self._question(p.request.question) for i, p in enumerate(chunk)}
        response = self._send({"state": chunk[0].request.state, "questions": questions})
        if response.is_error: raise JevUnavailable(f"hosted endpoint HTTP {response.status_code}")
        try:
            body = response.json()
            answers = body.get("answers") if isinstance(body, dict) else None
            if not isinstance(answers, dict): raise ValueError("missing answers")
            share = self._share(body.get("usage"), len(chunk))
            return [self._parse(chunk[i].request.question, answers[f"q{i}"], share) for i in range(len(chunk))]
        except JevUnavailable: raise
        except Exception as exc: raise JevUnavailable(f"invalid hosted response: {exc}") from exc

    @staticmethod
    def _prob(value):
        if isinstance(value, bool): raise ValueError("boolean probability")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError("invalid probability")
        return value

    @staticmethod
    def _confidence(provided, probs):
        try:
            value = float(provided)
            if math.isfinite(value) and 0 <= value <= 1: return value
        except (TypeError, ValueError): pass
        entropy = -sum(p * math.log(p) for p in probs if p > 0)
        if len(probs) < 2: return 1.0 if probs and probs[0] >= 1 else 0.0
        return min(1.0, max(0.0, 1 - entropy / math.log(len(probs))))

    @classmethod
    def _parse(cls, question, answer, input_tokens):
        kind = question.kind or "noul"
        key = question.key
        if kind == "noul":
            p = cls._prob(answer.get("noul") if isinstance(answer, dict) else answer)
            return JevResult(key, "true" if p >= .5 else "false", {"false": 1 - p, "true": p},
                             max(p, 1 - p), cls._confidence(None, [p, 1 - p]), input_tokens)
        if not isinstance(answer, dict): raise ValueError(f"invalid {kind} answer")
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or not probs: raise ValueError("missing probabilities")
        clean = {str(k): cls._prob(v) for k, v in probs.items()}
        top = max(clean.values())
        confidence = cls._confidence(answer.get("confidence"), list(clean.values()))
        if kind == "choice":
            label = answer.get("choice")
            if label not in probs: raise ValueError("choice answer not in probabilities")
            return JevResult(key, str(label), clean, top, confidence, input_tokens)
        if kind == "score":
            label = max(clean, key=clean.__getitem__)
            return JevResult(key, str(label), clean, top, confidence, input_tokens)
        raise NotImplementedError(f"hosted backend supports noul, choice and score, got {kind!r}")

    def close(self):
        self._pool.shutdown(wait=True)
        self._client.close()
