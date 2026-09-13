"""评测接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.evaluation.runner import run_evaluation
from app.models.schemas import EvaluationRequest

router = APIRouter(tags=["评测"])


@router.post(
    "/evaluation/run",
    summary="运行评测",
    description="运行测试集评测，输出关键词命中率与来源准确率。",
    response_description="评测结果",
)
def evaluate(
    request: Request,
    body: EvaluationRequest | None = None,
    user_id: int = Depends(get_current_user),
) -> dict:
    container = request.app.state.container
    body = body or EvaluationRequest()
    return run_evaluation(container.rag, test_set_path=body.test_set_path, top_k=body.top_k)