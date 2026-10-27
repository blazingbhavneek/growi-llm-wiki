import asyncio
from collections import deque
import hashlib
import json
import logging
import queue
import threading
import time
from concurrent.futures import Future

from .backends import make_backend
from .types import JevOutOfMemory, JevRequest, JevUnavailable, JevQuestion

log = logging.getLogger(__name__)
BUCKETS = (2048, 4096, 8192, 12288, 18432, 26624)


def plan_batches(items, budget_tokens, max_requests):
    buckets = {}
    for arrival, cost in items:
        edge = next((b for b in BUCKETS if cost <= b), cost)
        buckets.setdefault(edge, []).append((arrival, cost))
    pending = sorted(buckets.items(), key=lambda pair: min(i for i, _ in pair[1]))
    batches = []
    while pending:
        edge, values = pending.pop(0)
        values.sort(key=lambda item: item[1])
        batch, rest, peak = [], [], 0
        for arrival, cost in values:
            if batch and (len(batch) >= max_requests or (max(peak, cost) * (len(batch) + 1) > budget_tokens)):
                rest.append((arrival, cost))
            elif not batch and cost > budget_tokens:
                batches.append([arrival])
            else:
                batch.append(arrival)
                peak = max(peak, cost)
        if batch:
            batches.append(batch)
        if rest:
            pending.append((edge, rest))
            pending.sort(key=lambda pair: min(i for i, _ in pair[1]))
    return batches


def stats_summary(s: dict, wall_seconds: float) -> str:
    """Engine counters are running totals; show averages next to the elapsed wall time.

    Requests arrive in bursts, so many wait at the same moment: per-request waits overlap
    and do not add up to wall time.
    """
    runs, done = max(1, s["runs"]), max(1, s["batch_size_sum"])
    lookups = max(1, s["token_cache_hits"] + s["token_cache_misses"])
    padding = s["padding_tokens"] / max(1, s["batch_tokens"] + s["padding_tokens"])
    return (f"{s['requests']} requests in {wall_seconds / 60:.1f} min, {s['runs']} batches "
            f"(avg {s['batch_size_sum'] / runs:.1f}/batch, max {s['batch_size_max']}) | "
            f"per request: wait {s['queue_wait_ms'] / done / 1000:.2f} s, GPU {s['run_ms'] / done / 1000:.2f} s | "
            f"per batch: {s['run_ms'] / runs / 1000:.2f} s | "
            f"GPU busy {s['run_ms'] / 1000:.0f} s ({100 * s['run_ms'] / 1000 / max(1.0, wall_seconds):.0f}% of the time) | "
            + (f"padding {100 * padding:.0f}% of tokens | " if padding else "")
            + f"token cache hits {100 * s['token_cache_hits'] / lookups:.0f}% | errors {s['errors']}, OOM {s['out_of_memory_events']}")


class JevEngine:
    def __init__(self, backend, config=None):
        self.backend, self.config = backend, config
        self.max_requests = getattr(config, "max_batch_requests", 64)
        # A backend without padded batches (llama.cpp) sets its own budget.
        self.budget = getattr(backend, "max_batch_tokens", None) or getattr(config, "max_batch_tokens", 65536)
        self.batch_wait = getattr(config, "batch_wait_ms", 2) / 1000
        self.stats_seconds = getattr(config, "stats_seconds", 60)
        self.record_path = getattr(config, "record_path", "")
        self.record_max = getattr(config, "record_max", 300)
        self._recorded = 0
        self._record_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._queue = queue.Queue()
        self._closed = False
        self._stats = {"requests": 0, "runs": 0, "errors": 0, "batch_size_sum": 0, "batch_size_max": 0,
                       "batch_tokens": 0, "padding_tokens": 0, "queue_wait_ms": 0.0, "run_ms": 0.0,
                       "out_of_memory_events": 0, "budget": self.budget, "token_cache_hits": 0,
                       "token_cache_misses": 0}
        self._oom_times = deque()
        self._last_stats_log = self._started = time.monotonic()
        self._thread = threading.Thread(target=self._work, name="jev-engine", daemon=True)
        self._thread.start()

    def _request(self, state, question, state_id=""):
        if not isinstance(question, JevQuestion):
            question = JevQuestion(text=question.text, key=getattr(question, "key", ""))
        request = JevRequest(state, question, state_id)
        return request

    def _record(self, req):
        if self.record_path:
            with self._record_lock:
                if self._recorded < self.record_max:
                    from pathlib import Path
                    Path(self.record_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(self.record_path, "a", encoding="utf-8") as out:
                        out.write(json.dumps({"state": req.state, "question": {"text": req.question.text, "kind": req.question.kind, "options": req.question.options}}, ensure_ascii=False) + "\n")
                    self._recorded += 1

    def _submit(self, req):
        return self._enqueue(self._prepare(req))

    def _prepare(self, req):
        if self._closed:
            raise JevUnavailable("engine is closed")
        if not req.state_id:
            raw = req.state if isinstance(req.state, str) else json.dumps(req.state, ensure_ascii=False, sort_keys=True)
            req = JevRequest(req.state, req.question, hashlib.sha256(raw.encode()).hexdigest())
        self._record(req)
        self._stats["requests"] += 1
        try: return self.backend.prepare(req)
        except Exception:
            self._stats["errors"] += 1
            raise

    def _enqueue(self, prepared):
        future = Future()
        with self._submit_lock:
            if self._closed:
                self._stats["errors"] += 1
                raise JevUnavailable("engine is closed")
            self._queue.put((prepared, future, time.monotonic(), prepared.request.state_id))
        return future

    def _submit_all(self, requests, return_exceptions):
        """Tokenize every request first, then enqueue them back to back, so the worker's short
        batch window sees one call as one batch (and one state's questions stay together)."""
        prepared, early = [], {}
        for i, req in enumerate(requests):
            try: prepared.append(self._prepare(req))
            except Exception as exc:
                if not return_exceptions: raise
                prepared.append(None); early[i] = exc
        futures = []
        for i, item in enumerate(prepared):
            if item is None:
                futures.append(None); continue
            try: futures.append(self._enqueue(item))
            except Exception as exc:
                if not return_exceptions: raise
                futures.append(None); early[i] = exc
        return futures, early

    def decide(self, state, question, *, state_id=""):
        return self._submit(self._request(state, question, state_id)).result()

    def decide_batch(self, requests, *, return_exceptions=False):
        futures, early = self._submit_all(requests, return_exceptions)
        results = []
        for i, f in enumerate(futures):
            if i in early:
                results.append(early[i]); continue
            try: results.append(f.result())
            except Exception as exc:
                if not return_exceptions: raise
                results.append(exc)
        return results

    def decide_many(self, state, questions, *, state_id="", return_exceptions=False):
        return self.decide_batch([JevRequest(state, q if isinstance(q, JevQuestion) else JevQuestion(text=q.text, key=getattr(q, "key", "")), state_id) for q in questions], return_exceptions=return_exceptions)

    async def adecide_batch(self, requests, *, return_exceptions=False):
        futures, errors = self._submit_all(requests, return_exceptions)
        indexes = [i for i, future in enumerate(futures) if future is not None]
        completed = await asyncio.gather(*(asyncio.wrap_future(futures[i]) for i in indexes), return_exceptions=True)
        errors.update(zip(indexes, completed))
        out = []
        for i in range(len(futures)):
            value = errors.get(i)
            if isinstance(value, BaseException):
                if not return_exceptions: raise value
                out.append(value)
            else:
                out.append(value)
        return out

    async def adecide_many(self, state, questions, *, state_id="", return_exceptions=False):
        return await self.adecide_batch([JevRequest(state, q if isinstance(q, JevQuestion) else JevQuestion(text=q.text, key=getattr(q, "key", "")), state_id) for q in questions], return_exceptions=return_exceptions)

    def score_many(self, state, questions):
        try:
            return [r.p_yes for r in self.decide_many(state, questions)]
        except Exception as exc:
            raise RuntimeError(f"Jev scoring failed: {exc}") from exc

    def count_tokens(self, text): return self.backend.count_tokens(text)

    def _work(self):
        pending = deque()
        while True:
            first = pending.popleft() if pending else self._queue.get()
            if first is None: break
            batch = [first]
            deadline = time.monotonic() + self.batch_wait
            while len(batch) < self.max_requests:
                if pending:
                    item = pending.popleft()
                    if item is None:
                        pending.appendleft(None); break
                    batch.append(item)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                try: item = self._queue.get(timeout=remaining)
                except queue.Empty: break
                if item is None:
                    pending.append(None); break
                batch.append(item)
            planned = plan_batches([(i, item[0].cost) for i, item in enumerate(batch)], self.budget, self.max_requests)
            selected_indexes = planned[0] if planned else []
            selected = set(selected_indexes)
            # Marks each future running; a caller that cancelled meanwhile is dropped, since
            # resolving a cancelled future raises and would kill this worker thread.
            picked = [batch[i] for i in selected_indexes if batch[i][1].set_running_or_notify_cancel()]
            pending.extendleft(reversed([item for i, item in enumerate(batch) if i not in selected]))
            self._run(picked)
        try: self.backend.close()
        except Exception: log.exception("closing Jev backend")

    def _run(self, group):
        if not group: return
        start = time.monotonic()
        try:
            results = self.backend.run([i[0] for i in group])
            if len(results) != len(group): raise JevUnavailable("backend returned misaligned results")
            for item, result in zip(group, results): item[1].set_result(result)
        except JevOutOfMemory as exc:
            now = time.monotonic()
            self._stats["out_of_memory_events"] += 1
            self._oom_times.append(now)
            while self._oom_times and now - self._oom_times[0] > 60: self._oom_times.popleft()
            if len(self._oom_times) >= 2:
                # ponytail: the budget only shrinks; restart to reset it.
                self.budget = max(8192, int(self.budget * .8))
                self._stats["budget"] = self.budget
                self._oom_times.clear()
            if len(group) > 1:
                middle = len(group) // 2
                self._run(group[:middle]); self._run(group[middle:])
            else:
                self._stats["errors"] += 1
                group[0][1].set_exception(exc)
        except Exception as exc:
            by_state = {}
            for item in group: by_state.setdefault(item[3], []).append(item)
            if len(by_state) > 1:
                for same_state in by_state.values(): self._run(same_state)
            else:
                self._stats["errors"] += len(group)
                for item in group: item[1].set_exception(exc)
        elapsed = (time.monotonic() - start) * 1000
        self._stats["runs"] += 1; self._stats["batch_size_sum"] += len(group)
        self._stats["batch_size_max"] = max(self._stats["batch_size_max"], len(group))
        self._stats["batch_tokens"] += sum(i[0].cost for i in group)
        if getattr(self.backend, "pads_batches", True):
            self._stats["padding_tokens"] += max(i[0].cost for i in group) * len(group) - sum(i[0].cost for i in group)
        self._stats["queue_wait_ms"] += sum((start - i[2]) * 1000 for i in group)
        self._stats["run_ms"] += elapsed
        backend_stats = getattr(self.backend, "stats", {})
        if isinstance(backend_stats, dict):
            self._stats["token_cache_hits"] = backend_stats.get("token_cache_hits", self._stats["token_cache_hits"])
            self._stats["token_cache_misses"] = backend_stats.get("token_cache_misses", self._stats["token_cache_misses"])
            if "fast_kernels" in backend_stats: self._stats["fast_kernels"] = backend_stats["fast_kernels"]
        if self.stats_seconds and time.monotonic() - self._last_stats_log >= self.stats_seconds:
            log.debug("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
            self._last_stats_log = time.monotonic()

    def stats(self):
        current = dict(self._stats)
        backend_stats = getattr(self.backend, "stats", {})
        if isinstance(backend_stats, dict): current.update(backend_stats)
        current["budget"] = self.budget
        return current

    def close(self):
        with self._submit_lock:
            if not self._closed:
                self._closed = True; self._queue.put(None)
        self._thread.join()
