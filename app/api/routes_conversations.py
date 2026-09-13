"""对话管理：列表 / 查询 / 重命名 / 删除。"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import get_current_user
from app.models.schemas import ConversationHistory, ConversationInfo, RenameConversationRequest
from app.utils.exceptions import NotFoundError

router = APIRouter(tags=["对话管理"])


@router.get(
    "/conversations",
    response_model=List[ConversationInfo],
    summary="对话列表",
    description="列出全部对话（含标题、消息数，按最后更新时间倒序）。",
    response_description="对话摘要列表",
)
def list_conversations(request: Request, user_id: int = Depends(get_current_user)) -> List[dict]:
    rows = request.app.state.container.conversation.list_conversations()
    return [
        {
            "conversation_id": r["id"],
            "title": r["title"],
            "created_at": str(r["created_at"]),
            "updated_at": str(r["updated_at"]),
            "message_count": r["message_count"],
        }
        for r in rows
    ]


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationHistory,
    summary="获取对话历史",
    description="返回指定对话的全部消息（按时间升序）。",
    response_description="对话历史",
)
def get_conversation(
    request: Request,
    conversation_id: str = Path(..., description="对话 ID（UUID）"),
    user_id: int = Depends(get_current_user),
) -> dict:
    conv = request.app.state.container.db.get_conversation(conversation_id)
    if conv is None:
        raise NotFoundError("对话不存在")
    return conv


@router.patch(
    "/conversations/{conversation_id}",
    summary="重命名对话",
    description="修改对话标题。",
    response_description="更新后的对话摘要",
)
def rename_conversation(
    request: Request,
    body: RenameConversationRequest,
    conversation_id: str = Path(..., description="对话 ID（UUID）"),
    user_id: int = Depends(get_current_user),
) -> dict:
    if request.app.state.container.db.get_conversation(conversation_id) is None:
        raise NotFoundError("对话不存在")
    request.app.state.container.db.set_conversation_title(conversation_id, body.title.strip())
    return request.app.state.container.db.get_conversation(conversation_id)


@router.delete(
    "/conversations/{conversation_id}",
    summary="删除对话",
    description="删除指定对话及其全部消息。",
    response_description="删除结果",
)
def delete_conversation(
    request: Request,
    conversation_id: str = Path(..., description="对话 ID（UUID）"),
    user_id: int = Depends(get_current_user),
) -> dict:
    if request.app.state.container.db.get_conversation(conversation_id) is None:
        raise NotFoundError("对话不存在")
    request.app.state.container.db.delete_conversation(conversation_id)
    return {"status": "deleted", "conversation_id": conversation_id}