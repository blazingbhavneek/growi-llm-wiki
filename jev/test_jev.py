import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from jev import JevConfig, JevEngine, JevInputTooLong, JevQuestion, JevRequest, JevResult, JevUnavailable
from jev.engine import plan_batches
from jev.parity import compare
from jev.backends import Prepared
from jev.backends.hosted import HostedBackend
from jev.backends.llm2jev import Llm2JevBackend
from jev.backends.torch import TorchBackend, _snapshot, batch_layout, cache_prefix_ids
import httpx


def result(req, p=.9):
    return JevResult(req.question.key, "true" if p >= .5 else "false", {"false": 1-p, "true": p}, max(p, 1-p), .5, 1)


class FakeBackend:
    name = "fake"
    def __init__(self): self.calls = []
    def prepare(self, req):
        if req.question.text == "long": raise JevInputTooLong("long")
        return Prepared(req, 10, None)
    def run(self, batch):
        self.calls.append(batch)
        return [result(p.request, .9 if "yes" in str(p.request.state) else .1) for p in batch]
    def count_tokens(self, text): return len(text)
    def close(self): pass


class EngineTests(unittest.TestCase):
    def setUp(self): self.backend = FakeBackend(); self.engine = JevEngine(self.backend, JevConfig(batch_wait_ms=20))
    def tearDown(self): self.engine.close()

    def test_results_keep_input_order(self):
        requests = [JevRequest({"yes": i % 2}, JevQuestion(str(i), key=str(i))) for i in range(10)]
        self.assertEqual([x.key for x in self.engine.decide_batch(requests)], [str(i) for i in range(10)])

    def test_same_state_requests_run_together(self):
        self.engine.decide_many("yes", [JevQuestion("q", key=str(i)) for i in range(5)])
        self.assertEqual([len(c) for c in self.backend.calls], [5])

    def test_failures_are_per_future(self):
        class Broken(FakeBackend):
            def run(self, batch):
                if "bad" in str(batch[0].request.state): raise JevUnavailable("bad")
                return super().run(batch)
        e = JevEngine(Broken(), JevConfig(batch_wait_ms=10))
        try:
            out = e.decide_batch([JevRequest("bad", JevQuestion("q")), JevRequest("good", JevQuestion("q"))], return_exceptions=True)
            self.assertIsInstance(out[0], JevUnavailable); self.assertIsInstance(out[1], JevResult)
        finally: e.close()

    def test_one_states_questions_reach_the_backend_together(self):
        import time
        class LlamaLike(FakeBackend):
            max_batch_tokens = 1 << 30  # no padded batches: the torch budget must not split a state
            def prepare(self, req):
                time.sleep(.005)  # tokenizing a long state outlasts the 2 ms batch window
                return Prepared(req, 16000, None)
        backend = LlamaLike(); engine = JevEngine(backend, JevConfig())
        try:
            engine.decide_many("yes", [JevQuestion("q", key=str(i)) for i in range(20)])
            self.assertEqual([len(c) for c in backend.calls], [20])
        finally: engine.close()

    def test_cancelled_future_does_not_kill_worker(self):
        release = threading.Event()
        class Slow(FakeBackend):
            def run(self, batch):
                release.wait(5)
                return super().run(batch)
        engine = JevEngine(Slow(), JevConfig(batch_wait_ms=0))
        try:
            first = engine._submit(JevRequest("yes", JevQuestion("a")))
            cancelled = engine._submit(JevRequest("no", JevQuestion("b")))
            self.assertTrue(cancelled.cancel())  # still queued behind the slow batch
            release.set()
            self.assertEqual(first.result(5).p_yes, .9)
            self.assertEqual(engine._submit(JevRequest("yes", JevQuestion("c"))).result(5).p_yes, .9)
        finally: engine.close()

    def test_input_too_long_raises_before_queueing(self):
        with self.assertRaises(JevInputTooLong): self.engine.decide("x", JevQuestion("long"))
        self.assertFalse(self.backend.calls)

    def test_async_api(self):
        expected = self.engine.decide_many("yes", [JevQuestion("q", key="a")])
        self.assertEqual(asyncio.run(self.engine.adecide_many("yes", [JevQuestion("q", key="a")])), expected)

    def test_async_return_exceptions_keeps_item_order(self):
        class Broken(FakeBackend):
            def run(self, batch):
                if any(item.request.state == "bad" for item in batch): raise JevUnavailable("bad")
                return super().run(batch)
        engine = JevEngine(Broken(), JevConfig())
        try:
            results = asyncio.run(engine.adecide_batch([
                JevRequest("good", JevQuestion("q", key="a")),
                JevRequest("bad", JevQuestion("q", key="b")),
            ], return_exceptions=True))
            self.assertIsInstance(results[0], JevResult)
            self.assertIsInstance(results[1], JevUnavailable)
        finally: engine.close()

    def test_score_many_shim(self):
        self.assertEqual(self.engine.score_many("yes", [JevQuestion("q", key="a")]), [.9])
        class Broken(FakeBackend):
            def run(self, batch): raise JevUnavailable("no")
        e = JevEngine(Broken(), JevConfig())
        try:
            with self.assertRaises(RuntimeError): e.score_many("x", [JevQuestion("q")])
        finally: e.close()

    def test_close_stops_worker(self):
        self.engine.close(); self.assertFalse(self.engine._thread.is_alive())
        with self.assertRaises(JevUnavailable): self.engine.decide("x", JevQuestion("q"))

    def test_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "record.jsonl"
            e = JevEngine(FakeBackend(), JevConfig(record_path=str(path), record_max=2))
            try: e.decide_many("state", [JevQuestion("q1"), JevQuestion("q2"), JevQuestion("q3")])
            finally: e.close()
            rows = [json.loads(s) for s in path.read_text().splitlines()]
            self.assertEqual(len(rows), 2); self.assertEqual(rows[0]["state"], "state")

    def test_window_collects_concurrent_submitters(self):
        barrier = threading.Barrier(8)
        def submit(i):
            barrier.wait()
            return self.engine.decide(str(i), JevQuestion("q", key=str(i)))
        with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(submit, range(8)))
        self.assertLessEqual(len(self.backend.calls), 2)

    def test_oom_splits_and_shrinks(self):
        class OOMBackend(FakeBackend):
            def run(self, batch):
                if len(batch) > 2:
                    from jev import JevOutOfMemory
                    raise JevOutOfMemory("fake OOM")
                return super().run(batch)
        backend = OOMBackend(); engine = JevEngine(backend, JevConfig(batch_wait_ms=20))
        try:
            out = engine.decide_many("yes", [JevQuestion("q", key=str(i)) for i in range(8)])
            self.assertEqual(len(out), 8)
            self.assertEqual(engine.stats()["budget"], int(65536 * .8))
            self.assertGreaterEqual(engine.stats()["out_of_memory_events"], 2)
        finally: engine.close()


class PlanTests(unittest.TestCase):
    def test_budget_and_request_caps(self):
        self.assertTrue(all(len(b) <= 4 for b in plan_batches([(i, 1000) for i in range(10)], 4000, 64)))
        self.assertTrue(all(len(b) <= 2 for b in plan_batches([(i, 1000) for i in range(10)], 64000, 2)))
    def test_oversized_item_runs_alone(self):
        batches = plan_batches([(0, 100000), (1, 1000)], 64000, 10)
        self.assertIn([0], batches)
    def test_buckets_not_mixed_when_avoidable(self):
        self.assertTrue(all(len(b) == 1 for b in plan_batches([(0, 500), (1, 20000)], 64000, 10)))
    def test_fifo_across_buckets(self):
        self.assertEqual(plan_batches([(0, 20000), (1, 500)], 64000, 10)[0], [0])


class TorchLayoutTests(unittest.TestCase):
    def test_batch_layout(self):
        self.assertEqual(batch_layout([3, 5], [[2], [3, 4]]), (5, [(0, 2), (1, 3), (1, 4)], [1, 2]))


class TokenCacheTests(unittest.TestCase):
    def test_prefix_ids_cached_by_serialized_state_and_evicted(self):
        class Renderer:
            def __init__(self): self.calls = 0
            def prefix_ids(self, state): self.calls += 1; return [len(state["s"]), 7]
        renderer = Renderer(); stats = {}
        prefix = cache_prefix_ids(renderer, lambda state: json.dumps(state, sort_keys=True), 8, stats)
        prefix({"s": "a"}); got = prefix({"s": "a"}); got[0] = 999
        self.assertEqual(prefix({"s": "b"}), [1, 7])
        prefix({"s": "a"})
        self.assertEqual(renderer.calls, 3)
        self.assertEqual(stats, {"token_cache_hits": 1, "token_cache_misses": 3})


def _live_fixture():
    path = Path(__file__).resolve().parents[1] / "data" / "jev" / "parity.jsonl"
    if not path.is_file():
        # Keep opt-in live checks runnable in a fresh checkout. These are synthetic
        # smoke cases, not a substitute for parity against recorded traffic.
        paragraph = "対象ページには日本語の技術文書があり、設計と設定の説明が含まれます。"
        return [
            {"state": "対象ページ: " + paragraph * (1 + i),
             "question": {"text": "対象ページに設計の説明がありますか？"}}
            for i in range(20)
        ]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@unittest.skipUnless(os.environ.get("WIKI_JEV_LIVE_TEST") == "1", "set WIKI_JEV_LIVE_TEST=1 for model tests")
class LiveTorchTests(unittest.TestCase):
    def test_three_items_match_runtime_decide_exactly(self):
        from jev import get_engine, reset_engine
        rows = _live_fixture()
        if len(rows) < 3: self.skipTest("parity fixture needs at least 3 entries")
        engine = get_engine()
        if engine.backend.name != "torch": self.skipTest("live exact parity requires torch backend")
        try:
            for row in rows[:3]:
                q = row["question"]
                expected = engine.backend.runtime.decide(row["state"], q["text"], options=q.get("options"), qtype=q.get("kind", "noul"))
                actual = engine.decide(row["state"], JevQuestion(q["text"], q.get("kind", "noul"), q.get("options")))
                self.assertEqual(actual.answer, expected["answer"])
                self.assertEqual(actual.probabilities, expected["probabilities"])
        finally: reset_engine()


@unittest.skipUnless(os.environ.get("WIKI_JEV_LIVE_TEST") == "1", "set WIKI_JEV_LIVE_TEST=1 for model tests")
class LiveTorchBatchingTests(unittest.TestCase):
    def test_mixed_lengths_match_single_item_path(self):
        from jev import get_engine, reset_engine
        rows = _live_fixture()
        if len(rows) < 20: self.skipTest("parity fixture needs at least 20 entries")
        engine = get_engine()
        if engine.backend.name != "torch": self.skipTest("live batching parity requires torch backend")
        try:
            # Hold the worker's fill window open so this check exercises one real
            # multi-item `_scores_many` call instead of relying on scheduler timing.
            engine.batch_wait = 1.0
            fixture = rows[:20]
            reference, requests = [], []
            for row in fixture:
                q = row["question"]
                raw = engine.backend.runtime.decide(row["state"], q["text"], options=q.get("options"), qtype=q.get("kind", "noul"))
                reference.append(JevResult("", raw["answer"], raw["probabilities"], raw["top_probability"], raw["entropy_concentration"], raw.get("input_tokens", 0)))
                requests.append(JevRequest(row["state"], JevQuestion(q["text"], q.get("kind", "noul"), q.get("options"))))
            summary = compare(reference, engine.decide_batch(requests), .002)
            self.assertTrue(summary["passed"], summary)
            self.assertGreaterEqual(engine.stats()["batch_size_max"], 2)
        finally: reset_engine()


class ConfigTests(unittest.TestCase):
    def test_aliases_and_defaults(self):
        self.assertEqual(JevConfig.from_env({"WIKI_JEV_BACKEND": "local"}).backend, "torch")
        self.assertEqual(JevConfig.from_env({"WIKI_JEV_BACKEND": "auto", "WIKI_JEV_BASE_URL": "x"}).backend, "hosted")
        self.assertEqual(JevConfig.from_env({"WIKI_JEV_BACKEND": "auto", "WIKI_JEV_BASE_URL": "x", "WIKI_JEV_LOCAL_PATH": "/model"}).backend, "torch")
        self.assertEqual(JevConfig.from_env({}).max_batch_requests, 64)
    def test_validation_names_variable(self):
        with self.assertRaisesRegex(ValueError, "WIKI_JEV_BACKEND"):
            JevConfig.from_env({"WIKI_JEV_BACKEND": "bad"})
        for env, name in (({"WIKI_JEV_DEVICE": "tpu"}, "WIKI_JEV_DEVICE"),
                          ({"WIKI_JEV_DTYPE": "int8"}, "WIKI_JEV_DTYPE"),
                          ({"WIKI_JEV_GGUF_MANY_MODE": "fast"}, "WIKI_JEV_GGUF_MANY_MODE"),
                          ({"WIKI_JEV_MAX_BATCH_REQUESTS": "0"}, "WIKI_JEV_MAX_BATCH_REQUESTS")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name): JevConfig.from_env(env)

    def test_default_imports_do_not_load_ml_frameworks(self):
        import ast
        files = [Path(__file__).with_name("__init__.py"), Path(__file__).with_name("config.py"),
                 Path(__file__).with_name("engine.py"), Path(__file__).with_name("parity.py"),
                 Path(__file__).with_name("benchmark.py"), *Path(__file__).with_name("backends").glob("*.py")]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    names = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    if node.level: continue
                    names = {(node.module or "").split(".")[0]}
                else: continue
                self.assertFalse(names & {"torch", "transformers"}, f"{path}: top-level ML import")


class ParityTests(unittest.TestCase):
    def test_compare_flags_hard_flips_only(self):
        ref = [JevResult("", "true", {"true": .505, "false": .495}, .505, 0, 1), JevResult("", "true", {"true": .9, "false": .1}, .9, 0, 1)]
        got = [JevResult("", "false", {"true": .495, "false": .505}, .505, 0, 1), JevResult("", "false", {"true": .1, "false": .9}, .9, 0, 1)]
        summary = compare(ref, got, .02)
        self.assertEqual(summary["answer_flips"], 2); self.assertEqual(summary["hard_flips"], 1)
        self.assertFalse(summary["passed"])


class HostedBackendTests(unittest.TestCase):
    def test_wire_contract_state_passthrough_and_order(self):
        seen = {}
        def handler(request):
            seen.update(path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
            return httpx.Response(200, json={"answers": {
                "q0": {"type": "noul", "noul": .9}, "q1": {"type": "noul", "noul": .2}},
                "usage": {"input_tokens": 100, "output_tokens": 2}})
        config = JevConfig(backend="hosted", base_url="http://jev.test", api_key="secret")
        backend = HostedBackend(config, transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest({"doc": "s"}, JevQuestion("q", key=k), "same")) for k in ("a", "b")]
        try:
            values = backend.run(batch)
            self.assertEqual([v.p_yes for v in values], [.9, .2])
            self.assertEqual([v.key for v in values], ["a", "b"])
            self.assertEqual([v.input_tokens for v in values], [50, 50])
            self.assertEqual(seen["path"], "/v1/systemone"); self.assertEqual(seen["auth"], "Bearer secret")
            self.assertEqual(seen["body"]["state"], {"doc": "s"})
            self.assertEqual(seen["body"]["questions"], {
                "q0": {"type": "noul", "instructions": "q", "criteria": {}},
                "q1": {"type": "noul", "instructions": "q", "criteria": {}}})
            self.assertNotIn("model", seen["body"])
        finally: backend.close()

    def test_base_url_suffix_accepted(self):
        for base, expected in (("http://jev.test", "http://jev.test/v1/systemone"),
                               ("http://jev.test/v1/systemone", "http://jev.test/v1/systemone"),
                               ("http://jev.test/", "http://jev.test/v1/systemone")):
            backend = HostedBackend(JevConfig(base_url=base))
            try: self.assertEqual(backend.url, expected)
            finally: backend.close()

    def test_choice_and_score_mapping(self):
        def handler(request):
            return httpx.Response(200, json={"answers": {
                "q0": {"type": "choice", "choice": "b", "probabilities": {"a": .25, "b": .75}, "confidence": .4},
                "q1": {"type": "score", "score": 1.7, "probabilities": {"0": .1, "1": .2, "2": .7},
                       "legend": {"0": "low", "1": "mid", "2": "high"}, "confidence": .5}}})
        backend = HostedBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest("s", JevQuestion("pick", "choice", {"a": "A", "b": "B"}, key="c"), "s")),
                 backend.prepare(JevRequest("s", JevQuestion("level", "score", ["low", "mid", "high"], key="v"), "s"))]
        try:
            choice, score = backend.run(batch)
            self.assertEqual((choice.answer, choice.probabilities, choice.top_probability), ("b", {"a": .25, "b": .75}, .75))
            self.assertEqual((score.answer, score.top_probability), ("2", .7))
        finally: backend.close()

    def test_chunks_beyond_server_limit(self):
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(len(body["questions"]))
            n = len(body["questions"])
            return httpx.Response(200, json={"answers": {f"q{i}": {"type": "noul", "noul": .9} for i in range(n)}})
        backend = HostedBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest("s", JevQuestion("q", key=str(i)), "same")) for i in range(70)]
        try:
            values = backend.run(batch)
            self.assertEqual(len(values), 70); self.assertEqual(calls, [64, 6])
            self.assertEqual([v.key for v in values], [str(i) for i in range(70)])
        finally: backend.close()

    def test_rejects_bad_shapes_and_probabilities(self):
        cases = ([{"answers": {"q0": {"type": "noul", "noul": 1.2}}}],
                 [{"answers": {"q0": {"type": "noul", "noul": True}}}],
                 [{"answers": {}}],
                 [{"unexpected": True}])
        for body in cases:
            backend = HostedBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(
                lambda request, b=body[0]: httpx.Response(200, json=b)))
            try:
                with self.assertRaises(JevUnavailable): backend.run([backend.prepare(JevRequest({}, JevQuestion("q", key="a"), "s"))])
            finally: backend.close()

    def test_retry_only_transport_and_server_errors(self):
        for status, expected in ((400, 1), (422, 1), (503, 2)):
            calls = []
            backend = HostedBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(
                lambda request, s=status: (calls.append(1), httpx.Response(s))[1]))
            batch = [backend.prepare(JevRequest({}, JevQuestion("q"), "s"))]
            try:
                with self.assertRaises(JevUnavailable): backend.run(batch)
                self.assertEqual(len(calls), expected)
            finally: backend.close()

    def test_prepare_rejects_unknown_kind(self):
        backend = HostedBackend(JevConfig(base_url="http://jev.test"))
        try:
            with self.assertRaises(NotImplementedError):
                backend.prepare(JevRequest("s", JevQuestion("q", "rank", None)))
        finally: backend.close()

    def test_aliases_resolve_to_hosted(self):
        from jev.backends import make_backend
        for name in ("hosted", "systemone", "sglang", "vllm", "jpt"):
            self.assertEqual(JevConfig.from_env({"WIKI_JEV_BACKEND": name}).backend, "hosted")
            backend = make_backend(JevConfig(backend=name, base_url="http://jev.test"))
            try: self.assertIsInstance(backend, HostedBackend)
            finally: backend.close()
        self.assertEqual(JevConfig.from_env({"WIKI_JEV_BACKEND": "llm2jev"}).backend, "llm2jev")
        backend = make_backend(JevConfig(backend="llm2jev", base_url="http://jev.test"))
        try: self.assertIsInstance(backend, Llm2JevBackend)
        finally: backend.close()

    def test_singles_fly_concurrently_in_input_order(self):
        import threading, time
        active, peak = 0, 0
        lock = threading.Lock()
        def handler(request):
            nonlocal active, peak
            body = json.loads(request.content)
            assert "requests" not in body  # plain /v1/systemone: one state per call
            with lock:
                active += 1; peak = max(peak, active)
            try:
                time.sleep(.05)  # force overlap: sequential calls could never coincide here
                n = len(body["questions"])
                return httpx.Response(200, json={"answers": {
                    f"q{j}": {"type": "noul", "noul": .9} for j in range(n)}})
            finally:
                with lock: active -= 1
        backend = HostedBackend(JevConfig(base_url="http://jev.test", http_concurrency=20),
                                 transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest(f"state-{i}", JevQuestion("q", key=f"k{i}"), f"s{i}")) for i in range(35)]
        try:
            values = backend.run(batch)
            self.assertEqual([v.key for v in values], [f"k{i}" for i in range(35)])
            self.assertGreater(peak, 1)
        finally: backend.close()

    def test_concurrency_validation_names_variable(self):
        with self.assertRaisesRegex(ValueError, "WIKI_JEV_HTTP_CONCURRENCY"):
            JevConfig.from_env({"WIKI_JEV_HTTP_CONCURRENCY": "0"})
        self.assertEqual(JevConfig.from_env({"WIKI_JEV_LLM2JEV_CONCURRENCY": "7"}).http_concurrency, 7)  # old name

    def test_singles_keep_input_order(self):
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            assert "requests" not in body  # plain /v1/systemone: one state per call
            return httpx.Response(200, json={"answers": {
                k: {"type": "noul", "noul": .5} for k in body["questions"]}})
        backend = HostedBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest(f"s{i}", JevQuestion("q", key=f"k{i}"), f"s{i}")) for i in range(5)]
        try:
            values = backend.run(batch)
            self.assertEqual(len(calls), 5)  # one HTTP call per state
            self.assertTrue(all(set(c) == {"state", "questions"} for c in calls))
            self.assertEqual([v.key for v in values], [f"k{i}" for i in range(5)])
        finally: backend.close()


class Llm2JevBackendTests(unittest.TestCase):
    def test_wire_contract_and_keyed_results(self):
        seen = {}
        def handler(request):
            seen.update(path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
            return httpx.Response(200, json={"results": [
                {"key": "b", "probabilities": {"はい": .2}},
                {"key": "a", "probabilities": {"はい": .9}},
            ]})
        config = JevConfig(backend="llm2jev", model="model", base_url="http://jev.test", api_key="secret")
        backend = Llm2JevBackend(config, transport=httpx.MockTransport(handler))
        batch = [backend.prepare(JevRequest({}, JevQuestion("q", key=k), "same")) for k in ("a", "b")]
        try:
            values = backend.run(batch)
            self.assertEqual([v.p_yes for v in values], [.9, .2])
            self.assertEqual(seen["path"], "/score"); self.assertEqual(seen["auth"], "Bearer secret")
            self.assertEqual(seen["body"]["many_mode"], "batched")
            self.assertEqual(seen["body"]["options"], {"yes": "はい", "no": "いいえ"})
        finally: backend.close()

    def test_no_auth_without_key(self):
        auth = []
        backend = Llm2JevBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(
            lambda request: (auth.append(request.headers.get("authorization")), httpx.Response(200, json={"probabilities": [.5]}))[1]))
        try: backend.run([backend.prepare(JevRequest({}, JevQuestion("q"), "s"))])
        finally: backend.close()
        self.assertEqual(auth, [None])

    def test_rejects_bad_shapes_and_probabilities(self):
        cases = ([{"probabilities": []}], [{"probabilities": [1.2]}],
                 [{"results": [{"key": "x", "probabilities": {"yes": .5}}, {"key": "x", "probabilities": {"yes": .4}}]}],
                 [{"unexpected": True}])
        for body in cases:
            backend = Llm2JevBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(
                lambda request, b=body[0]: httpx.Response(200, json=b)))
            try:
                with self.assertRaises(JevUnavailable): backend.run([backend.prepare(JevRequest({}, JevQuestion("q", key="a"), "s"))])
            finally: backend.close()

    def test_retry_only_transport_and_server_errors(self):
        for status, expected in ((400, 1), (503, 2)):
            calls = []
            backend = Llm2JevBackend(JevConfig(base_url="http://jev.test"), transport=httpx.MockTransport(
                lambda request, s=status: (calls.append(1), httpx.Response(s))[1]))
            batch = [backend.prepare(JevRequest({}, JevQuestion("q"), "s"))]
            try:
                with self.assertRaises(JevUnavailable): backend.run(batch)
                self.assertEqual(len(calls), expected)
            finally: backend.close()


class TorchBackendTests(unittest.TestCase):
    def test_typed_question_and_probability_mapping(self):
        class Rendered:
            ids = [1, 2]
        class Module:
            TEMPLATE_VERSION = "macjev-render-v1"
            InputBudgetError = type("InputBudgetError", (Exception,), {})
            @staticmethod
            def make_question(q): return q
        class Runtime:
            def _render(self, state, q): self.typed = q; return Rendered()
            def _score_all(self, rs): return [.9]
            def _result(self, r, q, score): return {"answer": "true", "probabilities": {"false": .1, "true": score}, "top_probability": score, "entropy_concentration": .5, "input_tokens": 2}
        runtime = Runtime(); backend = TorchBackend(Module, runtime)
        prepared = backend.prepare(JevRequest("s", JevQuestion("q", key="k")))
        self.assertEqual(runtime.typed, {"t": "noul", "ins": "q", "crit": None})
        self.assertEqual(backend.run([prepared])[0].p_yes, .9)

    def test_count_tokens_uses_runtime_encode_callable(self):
        class Runtime:
            encode = staticmethod(lambda text: [*text])
        self.assertEqual(TorchBackend(None, Runtime()).count_tokens("abcd"), 4)

    def test_input_budget_maps(self):
        class Rendered: ids = []
        class Module:
            InputBudgetError = type("InputBudgetError", (Exception,), {})
            @staticmethod
            def make_question(q): return q
        class Runtime:
            def _render(self, state, q): raise Module.InputBudgetError()
        with self.assertRaises(JevInputTooLong): TorchBackend(Module, Runtime()).prepare(JevRequest("s", JevQuestion("q")))

    def test_template_version_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); (path / "jev_style_decision.py").write_text(
                "TEMPLATE_VERSION='old'\nclass JevStyleDecision: pass\n", encoding="utf-8")
            with mock.patch("jev.backends.torch._snapshot", return_value=path):
                with self.assertRaisesRegex(JevUnavailable, "old"):
                    TorchBackend.from_config(JevConfig(local_path=tmp))

    def test_local_snapshot_uses_complete_directory(self):
        from jev.backends.torch import REQUIRED
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name in REQUIRED: (path / name).touch()
            self.assertEqual(_snapshot(JevConfig(local_path=tmp)), path)

    def test_loads_fake_runtime_file_without_torch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "jev_style_decision.py").write_text(
                "TEMPLATE_VERSION='macjev-render-v1'\n"
                "def make_question(q): return q\n"
                "class InputBudgetError(Exception): pass\n"
                "def serialize_state(state): return str(state)\n"
                "class Renderer:\n"
                "    def prefix_ids(self, state): return [1, 2]\n"
                "class JevStyleDecision:\n"
                "    def __init__(self, path, device=None, dtype=None): self.args=(path,device,dtype); self.renderer=Renderer()\n",
                encoding="utf-8")
            with mock.patch("jev.backends.torch._snapshot", return_value=path):
                backend = TorchBackend.from_config(JevConfig(local_path=tmp))
            self.assertEqual(backend.runtime.args, (tmp, None, "bfloat16"))
            # The token cache hooks the runtime's module-level serialize_state.
            backend.runtime.renderer.prefix_ids("s"); backend.runtime.renderer.prefix_ids("s")
            self.assertEqual(backend.stats["token_cache_hits"], 1)


if __name__ == "__main__": unittest.main()
