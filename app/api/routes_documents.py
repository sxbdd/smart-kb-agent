"""文档列表 / 删除接口。

V2 隔离要点：列表与删除都带 `principal.tenant_id`，且**删除只允许 admin**
（见 docs/v2-plan.md §6.2）—— 文档是租户共享资产，误删影响整个租户。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import require_admin, require_role
from app.models.schemas import DocumentInfo
from app.services.tenancy import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, Principal
from app.utils.exceptions import NotFoundError

router = APIRouter(tags=["文档管理"])

#: 文档列表是只读操作：viewer 即为"只读角色"，因此三个角色都放行
require_reader = require_role(ROLE_VIEWER, ROLE_USER, ROLE_ADMIN)


@router.get(
    "/documents",
    response_model=List[DocumentInfo],
    summary="文档列表",
    description="列出本租户已建立索引的文档元数据。",
    response_description="文档元数据列表",
)
def list_documents(
    request: Request,
    principal: Principal = Depends(require_reader),
) -> List[dict]:
    rows = request.app.state.container.db.list_documents(principal.tenant_id)
    return [
        {
            "document_id": r["id"],
            "filename": r["filename"],
            "file_type": r["file_type"],
            "file_size": r["file_size"],
            "chunk_count": r["chunk_count"],
            "uploaded_at": str(r["uploaded_at"]),
        }
        for r in rows
    ]


@router.delete(
    "/documents/{document_id}",
    summary="删除文档",
    description="删除指定文档的元数据及对应向量索引（仅 admin）。",
    response_description="删除结果",
)
def delete_document(
    request: Request,
    document_id: str = Path(..., description="文档 ID（UUID）"),
    principal: Principal = Depends(require_admin),
) -> dict:
    container = request.app.state.container
    # get_document 已带租户条件：跨租户的 ID 在这里就表现为"不存在"，不泄漏它是否存在
    if container.db.get_document(document_id, principal.tenant_id) is None:
        raise NotFoundError("文档不存在")
    container.ingestion.delete(document_id, tenant_id=principal.tenant_id)
    return {"status": "deleted", "document_id": document_id}
