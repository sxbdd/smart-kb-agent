"""提问接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.models.schemas import AskRequest, AskResponse

router = APIRouter(tags=["智能问答"])


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="知识库问答",
    description="提交问题，由 Router 自动分发到普通对话 / RAG / Agent，并返回回答与引用来源。",
)
def ask(request: Request, body: AskRequest, user_id: int = Depends(get_current_user)) -> AskResponse:
    container = request.app.state.container
    return container.conversation.ask(body.question, body.conversation_id, body.top_k)