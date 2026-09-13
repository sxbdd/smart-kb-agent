"""评测接口：运行评测、保存历史、查询历史。

评测是**租户级的重操作**（会打满 LLM 配额，且结果对普通用户无意义），
因此两个端点都只允许 admin（见 docs/v2-plan.md §6.2）；
历史记录按 `principal.tenant_id` 存取，租户之间互不可见。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_admin
from app.models.schemas import EvaluationRequest
from app.services.evaluation import run_evaluation
from app.services.tenancy import Principal

router = APIRouter(tags=["评测"])


@router.post(
    "/evaluation/run",
    summary="运行评测",
    description="运行测试集评测，输出关键词命中率与来源准确率，并保存历史（仅 admin）。",
    response_description="评测结果",
)
def evaluate(
    request: Request,
    body: EvaluationRequest | None = None,
    principal: Principal = Depends(require_admin),
) -> dict:
    container = request.app.state.container
    body = body or EvaluationRequest()
    metrics = run_evaluation(
        container.rag,
        test_set_path=body.test_set_path,
        top_k=body.top_k,
        tenant_id=principal.tenant_id,
    )

    # 只存汇总指标，不存逐题 details（避免数据膨胀）
    summary = {k: v for k, v in metrics.items() if k != "details"}
    run_id = container.db.save_evaluation_run(summary, principal.tenant_id)
    metrics["run_id"] = run_id
    return metrics


@router.get(
    "/evaluation/runs",
    summary="评测历史",
    description="查询本租户的评测历史记录（按时间倒序，仅 admin）。",
    response_description="评测历史列表",
)
def list_runs(
    request: Request,
    principal: Principal = Depends(require_admin),
) -> list[dict]:
    rows = request.app.state.container.db.list_evaluation_runs(principal.tenant_id)
    out = []
    for r in rows:
        m = r["metrics"]
        # DAO 可能返回 JSON 字符串（旧行）或已解析的 dict，两种都兼容
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except json.JSONDecodeError:
                m = {}
        out.append({"run_id": r["id"], "metrics": m, "created_at": str(r["created_at"])})
    return out
