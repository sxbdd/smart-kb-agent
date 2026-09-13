"""Rerank A/B：用排序质量（Recall@1 / MRR）+ 延迟成本来回答"是否值得启用重排"。

背景：ADR-011 最初的结论（Noop 与 bge-reranker-base 持平 → V1 不启用）建立在
"单文档单 chunk、重排无发挥空间"的前提上。语料现已扩充为 6 文档 / 13 chunk，
但**评测指标已经饱和在 100%**，两边都满分就没有区分度 ——
所以本脚本直接测检索排序：正确答案排在第几名、以及重排多花多少时间。

**跑大语料 AB 之前必须先做自检**：`scripts/rebuild_kb.py --kb-dir X` 会清空后
只灌 X。如果 X 里没有评测集引用到的那 6 篇原始文档，39 道题会全部 not_found，
Noop 和 Rerank 都是 0 分 —— 那是语料搭错，不是"重排没用"。
所以本脚本默认会用 `--require-docs` 校验出处文档是否在索引里，缺失就**拒绝出数**。

用法：
    python scripts/rerank_ab.py                     # 生产配置：候选 5 -> 最终 3
    python scripts/rerank_ab.py --candidates 8 --final 8   # 纯排序质量（不截断）
    python scripts/rerank_ab.py --candidates 5 --final 3 --json out.json

    # 大语料 AB（合并语料：6 篇原始 + 60 篇干扰）
    python scripts/rerank_ab.py --candidates 20 --final 5 --json data/rerank_ab_large.json

    # 只看"候选集里还有没有正确文档"（召回上限，不加载重排模型）
    python scripts/rerank_ab.py --presence-only --candidates 5 10 20
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
from app.core.embedding import get_embedding_provider  # noqa: E402
from app.core.reranker import get_reranker  # noqa: E402
from app.core.vector_store import get_vector_store  # noqa: E402
from app.evaluation.runner import DEFAULT_TEST_SET  # noqa: E402


class _Retriever:
    """最小检索器：embedder + vector_store + reranker。

    这是 `RAGService` 里跟检索有关的那一小块，**故意不经 `build_container()`** ——
    容器会连 MySQL，而本脚本只想量检索质量与延迟；
    DB 连不上（例如 V2 多租户迁移还没跑、老表缺 `tenant_id`）不应该让 A/B 跑不起来。
    """

    def __init__(self, cfg: Settings) -> None:
        self.embedder = get_embedding_provider(cfg)
        self.vector_store = get_vector_store(cfg)
        self.reranker = get_reranker(cfg)
        #: 与 `RAGService` 保持一致：0.0 表示不过滤
        self.min_score = getattr(cfg, "min_score", 0.0)

    def search(self, question: str, top_k: int):
        embedding = self.embedder.encode([question])[0]
        return self.vector_store.query(embedding, top_k, min_score=self.min_score)


def _measure(rag: _Retriever, items, candidates: int, final: int) -> dict:
    ranks: list[int] = []
    latencies: list[float] = []
    for item in items:
        question = item["question"]
        expected = item["expected_source_doc"]
        t0 = time.perf_counter()
        hits = rag.search(question, top_k=candidates)
        hits = rag.reranker.rerank(question, hits, final)
        latencies.append((time.perf_counter() - t0) * 1000)
        names = [h.metadata.get("document_name", "") for h in hits]
        ranks.append(names.index(expected) + 1 if expected in names else 0)

    n = len(ranks) or 1
    found = [r for r in ranks if r > 0]
    return {
        "reranker": type(rag.reranker).__name__,
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


def _indexed_names(rag: _Retriever) -> set[str]:
    """枚举索引里出现过的 `document_name`。

    Chroma 后端直接遍历集合元数据（`collection.get`），拿到的就是**索引里真实的 chunk**；
    内存后端退化为看 `db.list_documents()`。这样能识别出
    "文档元数据在、但向量库里没有"的半灌状态。
    """
    collection = getattr(rag.vector_store, "collection", None)
    if collection is not None:
        rows = collection.get(include=["metadatas"]).get("metadatas") or []
        return {m.get("document_name", "") for m in rows if m and m.get("document_name")}
    dim = len(rag.embedder.encode(["探测"])[0])
    return {r.metadata.get("document_name", "")
            for r in rag.vector_store.query([0.0] * dim, rag.vector_store.count())}


def _preflight(rag: _Retriever, items, required: list[str]) -> tuple[bool, list[str]]:
    """自检：问题集引用的出处文档是否真的在索引里。

    Returns:
        (是否通过, 缺失的文档名列表)
    """
    expected_docs = {i["expected_source_doc"] for i in items if i.get("expected_source_doc")}
    indexed = _indexed_names(rag)

    missing = sorted(d for d in (expected_docs | set(required)) if d not in indexed)
    if missing:
        print("[自检失败] 下列出处文档不在索引里，继续跑只会得到全 0 的假结论：")
        for name in missing:
            print(f"    - {name}")
        print("    提示：用合并语料重建索引（--include-base）后重试。")
        return False, missing

    print(f"[自检通过] 索引含文档 {len(indexed)} 篇，"
          f"问题集引用的 {len(expected_docs)} 篇出处全部在库内。")
    return True, []


def _presence(rag: _Retriever, items, candidates: int) -> dict:
    """只看"正确文档是否落在候选集里"（召回上限，不需加载重排模型）。

    这是回答"Rerank 有没有发挥空间"的关键指标：
    若正确文档压根不在候选集里，任何重排器都救不回来（no-op 或更差）。
    """
    present = 0
    latencies: list[float] = []
    for item in items:
        expected = item["expected_source_doc"]
        t0 = time.perf_counter()
        hits = rag.search(item["question"], top_k=candidates)
        latencies.append((time.perf_counter() - t0) * 1000)
        if expected in [h.metadata.get("document_name", "") for h in hits]:
            present += 1
    n = len(items) or 1
    return {
        "candidates": candidates,
        "gold_in_candidates": present,
        "queries": len(items),
        "candidate_recall": round(present / n, 4),
        "latency_ms_mean": round(statistics.fmean(latencies), 1),
    }


def _load_questions(path: Path) -> list[dict]:
    """读取问题集，只保留可答题。

    文件结构与 `data/evaluation/test_set_smart.json` 一致：每项至少包含
    `question` 与 `expected_source_doc` 两个字段，`answerable` 为 false 的条目跳过。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"问题集格式错误，期望 JSON 数组：{path}")
    items = []
    for i, item in enumerate(raw):
        if not item.get("answerable", True):
            continue
        missing = [k for k in ("question", "expected_source_doc") if not item.get(k)]
        if missing:
            raise ValueError(f"{path} 第 {i + 1} 项缺少字段 {missing}")
        items.append(item)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerank A/B 排序质量对比")
    parser.add_argument("--test-set", default=None, help="问题集 JSON（默认 data/evaluation/test_set_smart.json）")
    parser.add_argument("--questions-file", default=None, help="同 --test-set；显式指定问题集时用这个")
    parser.add_argument("--candidates", type=int, nargs="+", default=[5], help="召回候选数（可给多个）")
    parser.add_argument("--final", type=int, default=3, help="重排后保留数")
    parser.add_argument("--json", default=None, help="把结果写到 JSON")
    parser.add_argument(
        "--require-docs", nargs="*", default=None,
        help="自检要求索引里必须存在的文档名（默认自动用问题集引用的出处文档）",
    )
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help="跳过自检（不推荐：语料搭错时会得到全 0 的假结论）",
    )
    parser.add_argument(
        "--presence-only", action="store_true",
        help="只测「正确文档是否在候选集内」（召回上限），不加载重排模型",
    )
    args = parser.parse_args()

    questions_path = Path(args.questions_file or args.test_set or DEFAULT_TEST_SET)
    if not questions_path.is_file():
        print(f"[错误] 问题集不存在：{questions_path}", file=sys.stderr)
        return 1

    items = _load_questions(questions_path)
    if not items:
        print(f"[错误] 问题集里没有可答题：{questions_path}", file=sys.stderr)
        return 1

    candidates_list = args.candidates if isinstance(args.candidates, list) else [args.candidates]
    candidates_list = sorted(set(candidates_list))

    # 索引规模一并记录：回答"重排是否值得"必须知道当前库有多大
    probe = _Retriever(dataclasses.replace(Settings(), enable_rerank=False))
    chunk_count = probe.vector_store.count()
    print(f"问题集 {questions_path}   可答题 {len(items)} 道   索引 chunk 数 {chunk_count}")

    # 自检：出处文档必须在索引里，否则后面的对比毫无意义
    if not args.skip_preflight:
        ok, _ = _preflight(probe, items, args.require_docs or [])
        if not ok:
            return 1

    # 召回上限：正确文档还在不在候选集里（决定重排有没有发挥空间）
    presence = [_presence(probe, items, c) for c in candidates_list]
    print("\n召回上限（正确文档是否落在候选集内）：")
    for row in presence:
        print(f"    候选 {row['candidates']:>3}：{row['gold_in_candidates']}/{row['queries']} "
              f"= {row['candidate_recall'] * 100:.1f}%   "
              f"检索延迟均值 {row['latency_ms_mean']} ms")
    print("=" * 78)

    if args.presence_only:
        if args.json:
            Path(args.json).write_text(
                json.dumps({
                    "questions_file": str(questions_path),
                    "chunk_count": chunk_count,
                    "questions": len(items),
                    "presence": presence,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"    详细结果已写入 {args.json}")
        return 0

    candidates = candidates_list[0]
    print(f"候选 {candidates} -> 最终 {args.final}")
    print("=" * 78)

    results = []
    for enable in (False, True):
        rag = _Retriever(dataclasses.replace(Settings(), enable_rerank=enable))
        label = "B 组（启用 bge-reranker-base）" if enable else "A 组（Noop，V1 现状）"
        stats = _measure(rag, items, candidates, args.final)
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
            json.dumps({
                "questions_file": str(questions_path),
                "chunk_count": chunk_count,
                "questions": len(items),
                "candidates": candidates,
                "final": args.final,
                "candidate_presence": presence,
                "results": results,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"    详细结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
