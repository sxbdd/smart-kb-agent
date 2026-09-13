"""FastAPI 应用入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import routes_ask, routes_auth, routes_conversations, routes_documents, routes_evaluation, routes_upload
from app.config import settings
from app.container import build_container
from app.utils.exceptions import AppError

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

TAGS_METADATA = [
    {"name": "健康检查", "description": "服务存活与状态检查"},
    {"name": "认证", "description": "注册与登录"},
    {"name": "文档管理", "description": "上传文档、建立向量索引，以及文档的列表与删除"},
    {"name": "智能问答", "description": "Router 分发：普通对话 / RAG / Agent"},
    {"name": "对话管理", "description": "多轮对话的查询与删除"},
    {"name": "评测", "description": "运行评测并输出指标"},
]


def create_app(cfg=None) -> FastAPI:
    container = build_container(cfg)
    app = FastAPI(
        title="企业级 RAG + Agent 智能知识库系统 API",
        description=(
            "基于 RAG + Agent 的企业知识库问答服务。\n\n"
            "核心流程：上传文档 → 向量化入库 → Router 分发（普通对话 / RAG / Agent）→ 返回带引用的回答。"
        ),
        version="0.1.0",
        openapi_tags=TAGS_METADATA,
    )
    app.state.container = container

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/healthz", tags=["健康检查"], summary="健康检查")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.include_router(routes_auth.router)
    app.include_router(routes_upload.router)
    app.include_router(routes_ask.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_conversations.router)
    app.include_router(routes_evaluation.router)
    return app


app = create_app()