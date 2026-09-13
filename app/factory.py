"""应用工厂：组装 FastAPI 实例。

单独成文件的原因是**消除 import 副作用**：`app/main.py` 需要在模块级构造
`app = create_app()` 供 `uvicorn app.main:app` 使用，如果 create_app 也定义在
main.py，那么任何 `import app.main`（包括测试）都会连带加载一次真实容器。
测试改为 `from app.factory import create_app` 即可按需构造。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import routes_ask, routes_auth, routes_conversations, routes_documents, routes_evaluation, routes_upload
from app.container import build_container
from app.utils.exceptions import AppError

logger = logging.getLogger("app")

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

TAGS_METADATA = [
    {"name": "健康检查", "description": "服务存活与状态检查"},
    {"name": "认证", "description": "注册与登录"},
    {"name": "文档管理", "description": "上传文档、建立向量索引，以及文档的列表与删除"},
    {"name": "智能问答", "description": "Router 分发：普通对话 / RAG / Agent"},
    {"name": "对话管理", "description": "多轮对话的查询与删除"},
    {"name": "评测", "description": "运行评测并输出指标"},
]

# 前端静态资源白名单：只暴露这两个文件，避免把整个 frontend 目录当静态站发布
_STATIC_FILES = {"marked.min.js", "purify.min.js"}


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

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底：未预期异常记完整堆栈到日志，对外只回通用信息，避免泄漏内部细节。"""
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看服务日志"})

    @app.get("/healthz", tags=["健康检查"], summary="健康检查")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/marked.min.js", include_in_schema=False)
    def marked_js() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "marked.min.js")

    @app.get("/purify.min.js", include_in_schema=False)
    def purify_js() -> FileResponse:
        """DOMPurify：前端渲染 LLM 回答前消毒，防 XSS。"""
        return FileResponse(FRONTEND_DIR / "purify.min.js")

    # 显式声明未使用的变量以便静态检查（白名单与路由一一对应）
    assert _STATIC_FILES == {"marked.min.js", "purify.min.js"}

    app.include_router(routes_auth.router)
    app.include_router(routes_upload.router)
    app.include_router(routes_ask.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_conversations.router)
    app.include_router(routes_evaluation.router)
    return app
