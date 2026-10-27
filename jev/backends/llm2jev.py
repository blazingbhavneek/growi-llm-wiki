import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..types import JevResult, JevUnavailable
from . import Prepared

OPTIONS = {"yes": "はい", "no": "いいえ"}


class Llm2JevBackend:
    """The custom batched scorer: ``POST {base}/score``, all questions of a state in one call.

    Request::

        {"model", "state", "questions": [{"key", "text"}, ...], "options": {"yes": "はい", "no": "いいえ"},
         "category": "noul", "many_mode": "batched"}

    Response: ``{"probabilities": [p, ...]}`` in question order, or
    ``{"results": [{"key", "probabilities": {"はい": p}}, ...]}`` in any order.

    One call per state; different states fly concurrently up to ``http_concurrency``
    (default 20). For a server with the vanilla ``/v1/systemone`` API (e.g. stock llm2jev)
    use the ``hosted`` backend instead.
    """

    name = "llm2jev"
    pads_batches = False  # the server batches; nothing is padded here

    def __init__(self, config, transport=None):
        self.config = config
        base = (config.base_url or "").rstrip("/")
        self.url = base if base.endswith("/score") else base + "/score"
        self._client = httpx.Client(timeout=config.timeout, transport=transport)
        self._concurrency = max(1, int(getattr(config, "http_concurrency", 20)))
        self._pool = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="jev-llm2jev")

    def prepare(self, request):
        if request.question.kind != "noul": raise NotImplementedError("llm2jev backend supports noul only")
        state = request.state
        cost = len(state.encode()) // 2 if isinstance(state, str) else len(json.dumps(state)) // 2
        return Prepared(request, cost, None)

    def count_tokens(self, text): return len(text.encode()) // 2

    def run(self, batch):
        if not batch: return []
        groups = {}
        for prepared in batch: groups.setdefault(prepared.request.state_id, []).append(prepared)
        jobs = list(groups.values())
        scored = [self._score_state(jobs[0])] if len(jobs) == 1 else list(self._pool.map(self._score_state, jobs))
        out = {}
        for group, results in zip(jobs, scored):
            for p, result in zip(group, results): out[id(p)] = result
        return [out[id(p)] for p in batch]

    def _score_state(self, batch):
        req = batch[0].request
        payload = {"model": self.config.model, "state": req.state,
                   "questions": [{"key": p.request.question.key, "text": p.request.question.text} for p in batch],
                   "options": OPTIONS, "category": "noul", "many_mode": "batched"}
        headers = {"Content-Type": "application/json"}
        if self.config.api_key: headers["Authorization"] = f"Bearer {self.config.api_key}"
        for attempt in range(2):
            try: response = self._client.post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                if not attempt: continue
                raise JevUnavailable(f"llm2jev request failed: {exc}") from exc
            if response.status_code >= 500 and not attempt: continue
            if response.is_error: raise JevUnavailable(f"llm2jev endpoint HTTP {response.status_code}")
            try: values = self._parse(response.json(), batch)
            except Exception as exc: raise JevUnavailable(f"invalid llm2jev response: {exc}") from exc
            return [self._result(p, value) for p, value in zip(batch, values)]
        raise JevUnavailable("llm2jev request failed after retry")

    @staticmethod
    def _p(value):
        if isinstance(value, dict):
            for key in ("true", "はい", "yes", "Yes"):
                if key in value: value = value[key]; break
            else: raise ValueError("missing yes probability")
        if isinstance(value, bool): raise ValueError("boolean probability")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError("invalid probability")
        return value

    @classmethod
    def _parse(cls, body, batch):
        raw = body.get("probabilities") if isinstance(body, dict) else None
        if raw is None and isinstance(body, dict): raw = body.get("results")
        if not isinstance(raw, list) or len(raw) != len(batch): raise ValueError("result count mismatch")
        if raw and all(isinstance(x, dict) and x.get("key") is not None for x in raw):
            mapping = {x["key"]: x for x in raw}
            keys = [p.request.question.key for p in batch]
            if len(mapping) != len(raw) or set(mapping) != set(keys): raise ValueError("result keys mismatch")
            raw = [mapping[k] for k in keys]
        return [cls._p(x.get("probabilities", x) if isinstance(x, dict) else x) for x in raw]

    @staticmethod
    def _result(prepared, p):
        entropy = -(p * math.log(p) if p else 0) - ((1-p) * math.log(1-p) if p < 1 else 0)
        concentration = 1 - entropy / math.log(2)
        q = prepared.request.question
        return JevResult(q.key, "true" if p >= .5 else "false", {"false": 1-p, "true": p}, max(p, 1-p), concentration, 0)

    def close(self):
        self._pool.shutdown(wait=True)
        self._client.close()
