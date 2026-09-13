"""文档列表 / 删除接口。"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import get_current_user
from app.models.schemas import DocumentInfo
from app.utils.exceptions import NotFoundError

router = APIRouter(tags=["文档管理"])


@router.get(
    "/documents",
    response_model=List[DocumentInfo],
    summary="文档列表",
    description="列出所有已建立索引的文档元数据。",
    response_description="文档元数据列表",
)
def list_documents(request: Request, user_id: int = Depends(get_current_user)) -> List[dict]:
    rows = request.app.state.container.db.list_documents()
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
    description="删除指定文档的元数据及对应向量索引。",
    response_description="删除结果",
)
def delete_document(
    request: Request,
    document_id: str = Path(..., description="文档 ID（UUID）"),
    user_id: int = Depends(get_current_user),
) -> dict:
    db = request.app.state.container.db
    if db.get_document(document_id) is None:
        raise NotFoundError("文档不存在")
    request.app.state.container.ingestion.delete(document_id)
    return {"status": "deleted", "document_id": document_id}