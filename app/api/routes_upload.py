"""文档上传接口。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.api.deps import require_role
from app.models.schemas import DocumentUploadResponse
from app.services.tenancy import ROLE_ADMIN, ROLE_USER, Principal
from app.utils.exceptions import AppError

router = APIRouter(tags=["文档管理"])

#: 上传是写操作：viewer 只能读（403），user / admin 可上传（见 docs/v2-plan.md §6.2）
require_uploader = require_role(ROLE_USER, ROLE_ADMIN)


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
    principal: Principal = Depends(require_uploader),
) -> DocumentUploadResponse:
    app = request.app
    safe_name = Path(file.filename or "document.txt").name
    dest_dir = Path(app.state.container.settings.documents_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}_{safe_name}"

    # 分块落盘 + 大小上限：避免把整个文件读进内存（历史问题见 docs/review-v1-audit.md §2.9）
    max_bytes = app.state.container.settings.max_upload_bytes
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise AppError(
                        f"文件超过上限 {app.state.container.settings.max_upload_mb} MB",
                        413,
                    )
                out.write(chunk)
        if written == 0:
            raise AppError("上传文件为空", 400)
        # 租户维度随文件一起下传：metadata 与 documents 表都按它落库（隔离强制点在服务层 / DAO）
        return app.state.container.ingestion.ingest(str(dest), safe_name, tenant_id=principal.tenant_id)
    except Exception:
        # 上传/入库失败就清掉半截文件，不在磁盘留下无人认领的残留
        dest.unlink(missing_ok=True)
        raise
