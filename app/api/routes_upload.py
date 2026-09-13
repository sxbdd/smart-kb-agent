"""文档上传接口。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.api.deps import get_current_user
from app.models.schemas import DocumentUploadResponse

router = APIRouter(tags=["文档管理"])


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    summary="上传文档并建立索引",
    description="上传 PDF / Markdown / TXT 文档，自动完成解析、切分、向量化并写入知识库。",
    response_description="上传与索引结果",
)
async def upload(
    request: Request,
    file: UploadFile = File(..., description="要上传的文档文件"),
    user_id: int = Depends(get_current_user),
) -> DocumentUploadResponse:
    app = request.app
    safe_name = Path(file.filename or "document.txt").name
    dest_dir = Path(app.state.container.settings.documents_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}_{safe_name}"

    dest.write_bytes(await file.read())
    try:
        return app.state.container.ingestion.ingest(str(dest), safe_name)
    except Exception:
        dest.unlink(missing_ok=True)
        raise