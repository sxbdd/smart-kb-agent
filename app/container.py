"""依赖容器：按配置组装服务。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from app.config import Settings, settings
from app.core.embedding import get_embedding_provider
from app.core.llm_client import get_llm_client
from app.core.reranker import get_reranker
from app.core.tools import build_tools
from app.core.vector_store import get_vector_store
from app.models.database import Database
from app.services.agent import AgentEngine
from app.services.auth import AuthService
from app.services.chat import ChatService
from app.services.conversation import ConversationService
from app.services.ingestion import IngestionService
from app.services.rag import RAGService
from app.services.router import Router

logger = logging.getLogger("container")


def build_container(cfg: Settings | None = None) -> SimpleNamespace:
    cfg = cfg or settings

    db = Database(cfg.mysql_host, cfg.mysql_port, cfg.mysql_user, cfg.mysql_password, cfg.mysql_db)
    try:
        db.init()
    except Exception as exc:
        logger.warning("MySQL 初始化失败（服务仍可启动，DB 功能暂不可用）：%s", exc)

    embedder = get_embedding_provider(cfg)
    vector_store = get_vector_store(cfg)
    llm = get_llm_client(cfg)
    reranker = get_reranker(cfg)

    ingestion = IngestionService(
        embedder=embedder, vector_store=vector_store, db=db,
        documents_dir=cfg.documents_dir, chunk_size=cfg.chunk_size, chunk_overlap=cfg.chunk_overlap,
    )
    rag = RAGService(
        embedder=embedder, vector_store=vector_store, llm=llm, reranker=reranker,
        top_k=cfg.top_k, rerank_top_k=cfg.rerank_top_k, max_history_messages=cfg.max_history_messages,
    )
    agent = AgentEngine(llm=llm, tools=build_tools(rag), rag=rag, max_iterations=cfg.agent_max_iterations)
    chat = ChatService(llm=llm)
    router = Router(llm=llm, enable_llm=False)
    conversation = ConversationService(db=db, router=router, chat=chat, rag=rag, agent=agent)
    auth = AuthService(db=db, jwt_secret=cfg.jwt_secret, jwt_expire_minutes=cfg.jwt_expire_minutes)

    return SimpleNamespace(
        settings=cfg, db=db, embedder=embedder, vector_store=vector_store, llm=llm,
        reranker=reranker, ingestion=ingestion, rag=rag, agent=agent, chat=chat,
        router=router, conversation=conversation, auth=auth,
    )