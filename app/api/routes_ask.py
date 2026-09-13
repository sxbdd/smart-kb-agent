"""提问接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_role
from app.models.schemas import AskRequest, AskResponse
from app.services.tenancy import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, Principal

router = APIRouter(tags=["智能问答"])

#: 提问对**所有**角色开放（viewer / user / admin 均可），见本任务冻结的角色矩阵。
#: 注意：docs/v2-plan.md §6.2 的表格把"POST /ask、读自己的会话"整行标为三列 ✅，
#: 不要误读成"仅 user 及以上"。
require_asker = require_role(ROLE_VIEWER, ROLE_USER, ROLE_ADMIN)


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="知识库问答",
    description="提交问题，由 Router 自动分发到普通对话 / RAG / Agent，并返回回答与引用来源。",
)
def ask(
    request: Request,
    body: AskRequest,
    principal: Principal = Depends(require_asker),
) -> AskResponse:
    container = request.app.state.container
    # 租户维度必须下传：检索与落库都按它隔离
    return container.conversation.ask(
        body.question,
        body.conversation_id,
        body.top_k,
        tenant_id=principal.tenant_id,
    )
