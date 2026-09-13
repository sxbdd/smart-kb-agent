"""命令行运行评测：跑测试集 → 打印指标 → 存入 evaluation_runs。

为什么需要它：评测要花钱、耗时间，而"跑 2~3 次看稳定性"是判读前提之一。
把它做成命令，避免每次手写一长串 python -c。

用法：
    python scripts/run_evaluation.py                   # 默认测试集 + 默认 top_k
    python scripts/run_evaluation.py --top-k 3
    python scripts/run_evaluation.py --repeat 2        # 连跑 2 次，输出稳定性对比
    python scripts/run_evaluation.py --no-save         # 不写入 evaluation_runs
    python scripts/run_evaluation.py --details         # 打印逐题命中情况（含失败与报错）
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.container import build_container  # noqa: E402
from app.evaluation.runner import run_evaluation  # noqa: E402

METRICS = ("keyword_accuracy", "source_accuracy", "refusal_accuracy", "overall_accuracy")


def _fmt(value) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="运行知识库评测")
    parser.add_argument("--test-set", default=None, help="测试集路径（默认用 runner 的 DEFAULT_TEST_SET）")
    parser.add_argument("--top-k", type=int, default=None, help="检索片段数（默认用配置里的 TOP_K）")
    parser.add_argument("--repeat", type=int, default=1, help="重复运行次数（看稳定性用）")
    parser.add_argument("--no-save", action="store_true", help="不写入 evaluation_runs 表")
    parser.add_argument("--details", action="store_true", help="打印逐题结果")
    args = parser.parse_args()

    container = build_container()
    print(f"向量库 chunk 数 = {container.vector_store.count()}")
    print(f"top_k = {args.top_k or container.settings.top_k}   重复 = {args.repeat} 次")
    print("=" * 72)

    runs: list[dict] = []
    for i in range(1, args.repeat + 1):
        metrics = run_evaluation(container.rag, test_set_path=args.test_set, top_k=args.top_k)
        runs.append(metrics)

        if not args.no_save:
            summary = {k: v for k, v in metrics.items() if k != "details"}
            run_id = container.db.save_evaluation_run(summary)
            saved = f"（已存 run_id={run_id[:8]}）"
        else:
            saved = ""

        print(f"第 {i} 次：总题数 {metrics['total']}"
              f"（可答 {metrics['answerable_count']} / 拒答 {metrics['non_answerable_count']}）{saved}")
        for key in METRICS:
            print(f"    {key:20s} {_fmt(metrics.get(key))}")
        errors = [d for d in metrics["details"] if d.get("error")]
        if errors:
            print(f"    ⚠️ LLM 调用失败 {len(errors)} 题（已计为不正确）")

        if args.details:
            print("    " + "-" * 68)
            for d in metrics["details"]:
                flag = "OK " if d["correct"] else "MISS"
                extra = f"  error={d['error']}" if d.get("error") else ""
                print(f"    [{flag}] {d['id']:5s} {d['question'][:26]:28s}"
                      f" kw={d.get('keyword_hit')} src={d.get('source_hit')} ref={d.get('refusal_hit')}{extra}")
        print("-" * 72)

    if args.repeat > 1:
        print("稳定性（多次运行的极差）：")
        for key in METRICS:
            values = [r[key] for r in runs if r[key] is not None]
            if not values:
                print(f"    {key:20s} -")
                continue
            spread = (max(values) - min(values)) * 100
            print(f"    {key:20s} 均值 {_fmt(statistics.fmean(values))}"
                  f"  极差 {spread:.1f}pp  (min {_fmt(min(values))} / max {_fmt(max(values))})")

    container.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
