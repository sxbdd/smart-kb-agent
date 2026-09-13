"""评测接口：运行评测、保存历史、查询历史。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.evaluation.runner import run_evaluation
from app.models.schemas import EvaluationRequest

router = APIRouter(tags=["评测"])


@router.post(
    "/evaluation/run",
    summary="运行评测",
    description="运行测试集评测，输出关键词命中率与来源准确率，并保存历史。",
    response_description="评测结果",
)
def evaluate(
    request: Request,
    body: EvaluationRequest | None = None,
    user_id: int = Depends(get_current_user),
) -> dict:
    container = request.app.state.container
    body = body or EvaluationRequest()
    metrics = run_evaluation(container.rag, test_set_path=body.test_set_path, top_k=body.top_k)

    # 只存汇总指标，不存逐题 details（避免数据膨胀）
    summary = {k: v for k, v in metrics.items() if k != "details"}
    run_id = container.db.save_evaluation_run(summary)
    metrics["run_id"] = run_id
    return metrics


@router.get(
    "/evaluation/runs",
    summary="评测历史",
    description="查询评测历史记录（按时间倒序）。",
    response_description="评测历史列表",
)
def list_runs(request: Request, user_id: int = Depends(get_current_user)) -> list[dict]:
    rows = request.app.state.container.db.list_evaluation_runs()
    out = []
    for r in rows:
        m = r["metrics"]
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except json.JSONDecodeError:
                m = {}
        out.append({"run_id": r["id"], "metrics": m, "created_at": str(r["created_at"])})
    return out