"""Rerank A/B：用排序质量（Recall@1 / MRR）+ 延迟成本来回答"是否值得启用重排"。

背景：ADR-011 最初的结论（Noop 与 bge-reranker-base 持平 → V1 不启用）建立在
"单文档单 chunk、重排无发挥空间"的前提上。语料现已扩充为 6 文档 / 13 chunk，
但**评测指标已经饱和在 100%**，两边都满分就没有区分度 ——
所以本脚本直接测检索排序：正确答案排在第几名、以及重排多花多少时间。

用法：
    python scripts/rerank_ab.py                     # 生产配置：候选 5 -> 最终 3
    python scripts/rerank_ab.py --candidates 8 --final 8   # 纯排序质量（不截断）
    python scripts/rerank_ab.py --candidates 5 --final 3 --json out.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.evaluation.runner import DEFAULT_TEST_SET  # noqa: E402


def _measure(container, items, candidates: int, final: int) -> dict:
    ranks: list[int] = []
    latencies: list[float] = []
    for item in items:
        question = item["question"]
        expected = item["expected_source_doc"]
        t0 = time.perf_counter()
        hits = container.rag.search(question, top_k=candidates)
        hits = container.rag.reranker.rerank(question, hits, final)
        latencies.append((time.perf_counter() - t0) * 1000)
        names = [h.metadata.get("document_name", "") for h in hits]
        ranks.append(names.index(expected) + 1 if expected in names else 0)

    n = len(ranks) or 1
    found = [r for r in ranks if r > 0]
    return {
        "reranker": type(container.rag.reranker).__name__,
        "queries": len(ranks),
        "recall_at_1": round(sum(1 for r in ranks if r == 1) / n, 4),
        "recall_at_k": round(len(found) / n, 4),
        "mrr": round(sum(1.0 / r for r in found) / n, 4),
        "mean_rank": round(statistics.fmean(found), 3) if found else None,
        "not_found": n - len(found),
        "latency_ms_p50": round(statistics.median(latencies), 1),
        "latency_ms_mean": round(statistics.fmean(latencies), 1),
        "latency_ms_p95": round(sorted(latencies)[min(len(latencies) - 1, int(0.95 * len(latencies)))], 1),
        "ranks": ranks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerank A/B 排序质量对比")
    parser.add_argument("--test-set", default=str(DEFAULT_TEST_SET))
    parser.add_argument("--candidates", type=int, default=5, help="召回候选数")
    parser.add_argument("--final", type=int, default=3, help="重排后保留数")
    parser.add_argument("--json", default=None, help="把结果写到 JSON")
    args = parser.parse_args()

    items = [i for i in json.loads(Path(args.test_set).read_text(encoding="utf-8")) if i["answerable"]]

    print(f"可答题 {len(items)} 道   候选 {args.candidates} -> 最终 {args.final}")
    print("=" * 78)

    results = []
    for enable in (False, True):
        cfg = dataclasses.replace(Settings(), enable_rerank=enable)
        container = build_container(cfg)
        label = "B 组（启用 bge-reranker-base）" if enable else "A 组（Noop，V1 现状）"
        try:
            stats = _measure(container, items, args.candidates, args.final)
        finally:
            container.db.close()
        stats["group"] = label
        stats["enable_rerank"] = enable
        results.append(stats)

        print(f"{label}")
        print(f"    重排器            {stats['reranker']}")
        print(f"    Recall@1          {stats['recall_at_1'] * 100:.1f}%")
        print(f"    Recall@{args.final}（最终集合） {stats['recall_at_k'] * 100:.1f}%")
        print(f"    MRR               {stats['mrr']:.4f}")
        print(f"    平均命中排名      {stats['mean_rank']}")
        print(f"    未命中            {stats['not_found']} 道")
        print(f"    延迟 p50/p95      {stats['latency_ms_p50']} / {stats['latency_ms_p95']} ms")
        print(f"    延迟均值          {stats['latency_ms_mean']} ms")
        print("-" * 78)

    a, b = results[0], results[1]
    print("结论：")
    print(f"    Recall@1 变化     {(b['recall_at_1'] - a['recall_at_1']) * 100:+.1f}pp")
    print(f"    MRR 变化          {b['mrr'] - a['mrr']:+.4f}")
    print(f"    延迟变化          {b['latency_ms_mean'] - a['latency_ms_mean']:+.1f} ms/次"
          f"（{b['latency_ms_mean'] / max(a['latency_ms_mean'], 0.01):.1f}x）")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"candidates": args.candidates, "final": args.final, "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"    详细结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
