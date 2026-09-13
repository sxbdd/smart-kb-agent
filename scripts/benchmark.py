"""性能基准：检索链路延迟与并发吞吐（默认不调用 LLM，零费用）。

为什么单独做：评测回答的是"答得准不准"，性能回答的是"扛不扛得住"。
检索链路（向量化 + Chroma 查询）是每次问答的固定开销，且完全本地，可以放心压。
端到端问答需要调用 LLM（有费用、且受上游限速影响），因此默认关闭，用 --with-llm 显式开启。

用法：
    python scripts/benchmark.py                                  # 检索链路基准
    python scripts/benchmark.py --queries 47 --top-k 5
    python scripts/benchmark.py --concurrency 1,4,8              # 多档并发对比
    python scripts/benchmark.py --with-llm --llm-samples 5       # 附带端到端延迟（有费用）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.container import build_container  # noqa: E402
from app.evaluation.runner import DEFAULT_TEST_SET  # noqa: E402


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def _summary(label: str, latencies: list[float], wall: float) -> None:
    if not latencies:
        print(f"  {label}: 无样本")
        return
    print(f"  {label}")
    print(f"      样本 {len(latencies)}   墙钟 {wall:.2f}s   吞吐 {len(latencies) / wall:.1f} QPS")
    print(f"      延迟 mean {statistics.fmean(latencies):7.1f}ms   "
          f"p50 {_percentile(latencies, 50):7.1f}ms   "
          f"p95 {_percentile(latencies, 95):7.1f}ms   "
          f"max {max(latencies):7.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="检索/问答性能基准")
    parser.add_argument("--test-set", default=str(DEFAULT_TEST_SET))
    parser.add_argument("--queries", type=int, default=0, help="串行测试的查询数（0=用测试集全部）")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--concurrency", default="1,4,8", help="并发档位，逗号分隔")
    parser.add_argument("--concurrent-total", type=int, default=64, help="每档并发的总请求数")
    parser.add_argument("--with-llm", action="store_true", help="附带端到端问答延迟（会调用 LLM，产生费用）")
    parser.add_argument("--llm-samples", type=int, default=5)
    args = parser.parse_args()

    items = json.loads(Path(args.test_set).read_text(encoding="utf-8"))
    questions = [i["question"] for i in items]
    if args.queries:
        questions = questions[: args.queries]

    container = build_container()
    top_k = args.top_k or container.settings.top_k
    print(f"向量库 chunk = {container.vector_store.count()}")
    print(f"查询数 = {len(questions)}   top_k = {top_k}")
    print("=" * 72)

    # 冷启动一次（模型/索引首次访问），不计入统计
    container.rag.search(questions[0], top_k=top_k)
    print("  （已预热一次，冷启动不计入统计）")
    print()

    # 串行延迟
    latencies: list[float] = []
    start = time.perf_counter()
    for q in questions:
        t0 = time.perf_counter()
        container.rag.search(q, top_k=top_k)
        latencies.append((time.perf_counter() - t0) * 1000)
    _summary("检索链路 · 串行", latencies, time.perf_counter() - start)

    # 并发吞吐
    for level in (int(x) for x in str(args.concurrency).split(",") if x.strip()):
        def one(i: int) -> float:
            t0 = time.perf_counter()
            container.rag.search(questions[i % len(questions)], top_k=top_k)
            return (time.perf_counter() - t0) * 1000

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=level) as pool:
            conc_latencies = list(pool.map(one, range(args.concurrent_total)))
        _summary(f"检索链路 · 并发 {level}", conc_latencies, time.perf_counter() - start)

    if args.with_llm:
        print()
        print("端到端问答（含 LLM，会产生费用）：")
        sample = questions[: args.llm_samples]
        latencies = []
        for q in sample:
            t0 = time.perf_counter()
            answer, _ = container.rag.answer(q, top_k=top_k)
            latencies.append((time.perf_counter() - t0) * 1000)
            print(f"      [{len(answer):4d} 字] {q[:24]}")
        _summary("问答链路 · 串行", latencies, sum(latencies) / 1000)

    container.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
