import argparse
import json
import logging
import statistics

log = logging.getLogger(__name__)


def compare(reference_results, engine_results, max_abs=0.002):
    deltas, flips, hard = [], 0, 0
    for ref, got in zip(reference_results, engine_results):
        rp, gp = ref.probabilities, got.probabilities
        deltas.extend(abs(float(rp[k]) - float(gp[k])) for k in rp)
        if ref.answer != got.answer:
            flips += 1
            ordered = sorted(rp.values(), reverse=True)
            threshold = .51 if set(rp) == {"false", "true"} else .02
            if (ordered[0] >= threshold if threshold == .51 else ordered[0] - ordered[1] >= threshold): hard += 1
    return {"items": len(reference_results), "max_abs": max(deltas, default=0),
            "mean_abs": statistics.fmean(deltas) if deltas else 0, "answer_flips": flips,
            "hard_flips": hard, "passed": hard == 0 and max(deltas, default=0) <= max_abs}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check"); check.add_argument("--fixture", required=True)
    check.add_argument("--limit", type=int); check.add_argument("--max-abs", type=float, default=.002)
    args = parser.parse_args()
    from . import get_engine
    from .types import JevQuestion
    from .backends.torch import TorchBackend, _snapshot
    engine = get_engine()
    config = engine.config
    path = _snapshot(config)
    import importlib.util
    spec = importlib.util.spec_from_file_location("jev_reference", path / "jev_style_decision.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    runtime = module.JevStyleDecision(str(path), device=None if config.device == "auto" else config.device, dtype="float32")
    try:
        rows = [json.loads(line) for line in open(args.fixture, encoding="utf-8") if line.strip()]
    except Exception as exc:
        # json's own message carries the line and column; it never names the file.
        log.error("Jevの比較用データを読み込めません: ファイル=%s、エラー=%s: %s", args.fixture, type(exc).__name__, exc, exc_info=True)
        raise
    if args.limit: rows = rows[:args.limit]
    reference, actual = [], []
    for row_number, row in enumerate(rows, start=1):
        try:
            q = row["question"]
            ref = runtime.decide(row["state"], q["text"], options=q.get("options"), qtype=q.get("kind", "noul"))
            from .types import JevResult
            reference.append(JevResult("", ref["answer"], ref["probabilities"], ref["top_probability"], ref["entropy_concentration"], ref.get("input_tokens", 0)))
            actual.append(engine.decide(row["state"], JevQuestion(q["text"], q.get("kind", "noul"), q.get("options"))))
        except Exception as exc:
            # A KeyError names the missing fixture key; blank lines are filtered out, so the
            # identity here is the fixture row number, not the raw file line.
            log.error("Jevの比較用データの行処理に失敗しました: ファイル=%s、行=%d、エラー=%s: %s", args.fixture, row_number, type(exc).__name__, exc, exc_info=True)
            raise
    summary = compare(reference, actual, args.max_abs)
    print(json.dumps(summary, indent=2)); engine.close()
    if not summary["passed"]:
        log.error("Jevの基準結果との比較に失敗しました。参照実装から結果が悪化しています: 検証データ=%s、項目数=%d、最大絶対差=%s、回答の反転数=%d、大きな反転数=%d、しきい値=%s（終了コードは1です）", args.fixture, summary["items"], summary["max_abs"], summary["answer_flips"], summary["hard_flips"], args.max_abs)
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
