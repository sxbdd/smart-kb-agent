"""对话管理：列表 / 查询 / 重命名 / 删除。

V2 隔离边界（重要）：本版本的"会话归属"只做到**租户级** —— 同一租户内用户互相
可见、可改、可删；按 `user_id` 的跨用户隔离不在本次范围
（见 docs/v2-plan.md §6.5 的诚实声明）。因此这里**不要**额外发明 user_id 过滤。

角色边界按本任务冻结的角色矩阵：`POST /ask`、读自己的会话、改/删自己的会话
三行 viewer / user / admin **全部为 ✅**（viewer 即"只读角色"是相对文档上传与
删除而言，会话管理与提问对它开放）。注意 docs/v2-plan.md §6.2 的表格把这三行
渲染成"三列 ✅"，不要误读成"仅 user 及以上"。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import require_role
from app.models.schemas import ConversationHistory, ConversationInfo, RenameConversationRequest
from app.services.tenancy import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, Principal
from app.utils.exceptions import NotFoundError

router = APIRouter(tags=["对话管理"])

#: 读自己的会话 / 改删自己的会话：按冻结的角色矩阵，viewer、user、admin **都放行**
require_member = require_role(ROLE_VIEWER, ROLE_USER, ROLE_ADMIN)


@router.get(
    "/conversations",
    response_model=List[ConversationInfo],
    summary="对话列表",
    description="列出本租户的全部对话（含标题、消息数，按最后更新时间倒序）。",
    response_description="对话摘要列表",
)
def list_conversations(
    request: Request,
    principal: Principal = Depends(require_member),
) -> List[dict]:
    rows = request.app.state.container.conversation.list_conversations(tenant_id=principal.tenant_id)
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
    principal: Principal = Depends(require_member),
) -> dict:
    db = request.app.state.container.db
    # 归属判断交给 DAO 的租户条件：查不到即"不存在"，不额外做"先查再判断"
    conv = db.get_conversation(conversation_id, principal.tenant_id)
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
    principal: Principal = Depends(require_member),
) -> dict:
    db = request.app.state.container.db
    if db.get_conversation(conversation_id, principal.tenant_id) is None:
        raise NotFoundError("对话不存在")
    db.set_conversation_title(conversation_id, body.title.strip(), tenant_id=principal.tenant_id)
    return db.get_conversation(conversation_id, principal.tenant_id)


@router.delete(
    "/conversations/{conversation_id}",
    summary="删除对话",
    description="删除指定对话及其全部消息。",
    response_description="删除结果",
)
def delete_conversation(
    request: Request,
    conversation_id: str = Path(..., description="对话 ID（UUID）"),
    principal: Principal = Depends(require_member),
) -> dict:
    db = request.app.state.container.db
    if db.get_conversation(conversation_id, principal.tenant_id) is None:
        raise NotFoundError("对话不存在")
    db.delete_conversation(conversation_id, principal.tenant_id)
    return {"status": "deleted", "conversation_id": conversation_id}
