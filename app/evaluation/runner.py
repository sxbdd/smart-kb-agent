"""评测执行：关键词命中率 + 来源准确率 + 拒答准确率。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.services.tenancy import normalize_tenant

# 注意：必须与实际存在的测试集文件名一致，否则不传 test_set_path 时必然 500
# （历史 bug：这里曾指向不存在的 test_set.json，见 docs/review-v1-audit.md §2.1）
DEFAULT_TEST_SET = Path(__file__).resolve().parent.parent.parent / "data" / "evaluation" / "test_set_smart.json"

REFUSAL_PHRASES = (
    "无法回答",
    "没有相关",
    "未检索到",
    "知识库中没有",
    "知识库不包含",
    "无法提供",
    "不包含相关信息",
    "不存在",
)


def _is_refusal(answer: str) -> bool:
    return any(p in answer for p in REFUSAL_PHRASES)


def run_evaluation(
    rag_service,
    test_set_path: Optional[str] = None,
    top_k: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    """跑一轮评测。

    ``tenant_id`` 非 None 时，**每一道题**的检索都限定在该租户的库上：评测是 admin
    功能（路由层已限 admin），但"admin 能看到所有租户"不等于"指标应该混在一起算" ——
    混算出来的准确率哪个租户都代表不了。为 None 时保持 V1 的全局行为。
    """
    path = test_set_path or str(DEFAULT_TEST_SET)
    test_set = json.loads(Path(path).read_text(encoding="utf-8"))

    # 归一化一次，避免"空串租户"被下层当作无过滤，导致评测悄悄跑成全局口径
    tenant = None if tenant_id is None else normalize_tenant(tenant_id)

    results = []
    for item in test_set:
        # 逐题捕获异常：47 题的批量化评测里，单次 LLM 抖动不该让整轮评测崩掉。
        # 失败题记为不正确并保留原因，便于事后定位是"答错"还是"没答"。
        error = ""
        try:
            if tenant is None:
                answer, sources = rag_service.answer(question=item["question"], top_k=top_k)
            else:
                answer, sources = rag_service.answer(
                    question=item["question"], top_k=top_k, tenant_id=tenant
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            answer, sources = "", []

        answerable = bool(item.get("answerable", True))
        expected_keywords = item.get("expected_keywords") or []
        expected_source_doc = item.get("expected_source_doc") or ""

        keyword_hit = all(kw in answer for kw in expected_keywords) if expected_keywords else None
        source_hit = (
            any(expected_source_doc in s.document_name for s in sources)
            if expected_source_doc
            else None
        )
        refusal_hit = _is_refusal(answer)

        if answerable:
            correct = bool(keyword_hit) if keyword_hit is not None else False
            if source_hit is not None:
                correct = correct and bool(source_hit)
        else:
            correct = refusal_hit

        results.append(
            {
                "id": item["id"],
                "category": item.get("category", ""),
                "answerable": answerable,
                "question": item["question"],
                "reference": item.get("reference", ""),
                "gold_evidence": item.get("gold_evidence", ""),
                "expected_keywords": expected_keywords,
                "answer": answer,
                "keyword_hit": keyword_hit,
                "source_hit": source_hit,
                "refusal_hit": refusal_hit,
                "correct": correct,
                "error": error,
                "sources": [s.document_name for s in sources],
            }
        )

    answerable = [r for r in results if r["answerable"]]
    non_answerable = [r for r in results if not r["answerable"]]
    total = len(results) or 1

    def _acc(rows: list[dict], key: str) -> Optional[float]:
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "total": len(results),
        "answerable_count": len(answerable),
        "non_answerable_count": len(non_answerable),
        "error_count": sum(1 for r in results if r["error"]),
        "keyword_accuracy": _acc(answerable, "keyword_hit"),
        "source_accuracy": _acc(answerable, "source_hit"),
        "refusal_accuracy": _acc(non_answerable, "refusal_hit"),
        "overall_accuracy": round(sum(r["correct"] for r in results) / total, 4),
        "details": results,
    }
