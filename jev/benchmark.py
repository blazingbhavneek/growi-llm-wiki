import argparse
import concurrent.futures
import json
import logging
import statistics
import time

log = logging.getLogger(__name__)

PARAGRAPH = "これは日本語の技術文書です。対象のページには設計、手順、設定、用語の説明が含まれます。検索結果を評価するために使用します。"
QUESTION = "このページには、対象の質問に対する答えが具体的に記載されていますか？"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", type=int, default=200); ap.add_argument("--lengths", default="2000,8000,16000,24000")
    ap.add_argument("--questions", default="1,5,20"); ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--decide-many", action="store_true"); ap.add_argument("--json")
    args = ap.parse_args()
    from . import get_engine, JevQuestion
    engine = get_engine(); rows = []
    for length in map(int, args.lengths.split(",")):
        state = PARAGRAPH
        while engine.count_tokens(state) < length: state += PARAGRAPH
        for count in map(int, args.questions.split(",")):
            latencies = []; started = time.perf_counter()
            def work(i):
                t = time.perf_counter()
                qs = [JevQuestion(QUESTION, key=str(j)) for j in range(count)]
                sample = {"text": state, "sample": i}
                try:
                    result = engine.decide_many(sample, qs) if args.decide_many else engine.decide_batch([
                        __import__("jev").JevRequest(sample, q) for q in qs])
                except Exception as exc:
                    # A MemoryError here is the host running out of memory inside this cell; a
                    # kernel OOM kill (SIGKILL) cannot be logged from the dying process.
                    log.error("Jevの性能測定に失敗しました。この条件で測定を中断します: 長さ=%d、質問数=%d、標本数=%d、並列数=%d、エラー=%s: %s", length, count, i, args.concurrency, type(exc).__name__, exc, exc_info=True)
                    raise
                latencies.append(time.perf_counter() - t)
                return result
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(work, range(args.states)))
            elapsed = time.perf_counter() - started; latencies.sort()
            n = args.states * count
            rows.append({"length": length, "questions": count, "requests_s": args.states/elapsed,
                         "questions_s": n/elapsed, "input_tokens_s": n*length/elapsed,
                         "p50": statistics.median(latencies), "p95": latencies[int(.95*(len(latencies)-1))],
                         "p99": latencies[int(.99*(len(latencies)-1))], "stats": engine.stats()})
    peak_vram = 0
    try:
        import torch
        if torch.cuda.is_available(): peak_vram = torch.cuda.max_memory_allocated()
    except ImportError as exc:
        # peak_vram_bytes stays 0, so the table must say "torch is missing" and not "there was no GPU".
        log.error("torch がないため、Jev性能測定の最大VRAM使用量を取得できません。peak_vram_bytes は0のままです: エラー=%s: %s", type(exc).__name__, exc, exc_info=True)
    for row in rows: row["peak_vram_bytes"] = peak_vram
    headers = ("length", "questions", "requests_s", "questions_s", "input_tokens_s", "p50", "p95", "p99", "peak_vram_bytes")
    print("| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|")
    for row in rows: print("| " + " | ".join(str(row[k]) for k in headers) + " |")
    if args.json:
        with open(args.json, "w") as out: json.dump({"rows": rows, "stats": engine.stats()}, out, indent=2)
    engine.close()


if __name__ == "__main__":
    from runner.logging_support import run_standalone
    raise SystemExit(run_standalone(main))
