"""pytest 全局配置：默认离线、可隔离、不依赖 MySQL / 外网 / 本地模型。

两个关键设计：

1. **回退组件必须在 import app.* 之前**通过环境变量设定，否则 `Settings()` 会取到
   真实配置。历史坑：`settings` 曾是"import 时固化"的单例，用 pytest 一次收集
   多个文件时谁先 import 谁说了算（见 docs/review-v1-audit.md §2.3）。
   现在 `Settings` 改为实例化时读环境，测试可显式构造。
2. **FakeDatabase 替换真实 MySQL**，让 API 层的鉴权 / 会话 / 评测都能离线跑。
   真实环境（MySQL + Chroma + bge + DeepSeek）的用例放在 test_integration_real.py，
   默认跳过。
"""
from __future__ import annotations

import dataclasses
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

# ---- 必须在 import app.* 之前设置 ----
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-use")
os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["VECTOR_STORE"] = "memory"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["AUTH_RATE_LIMIT_PER_MINUTE"] = "1000"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.core.embedding import HashEmbedding  # noqa: E402
from app.core.llm_client import FakeLLMClient  # noqa: E402
from app.core.reranker import NoopReranker  # noqa: E402
from app.core.vector_store import InMemoryVectorStore  # noqa: E402


class FakeDatabase:
    """内存版 Database，签名与 app.models.database.Database 对齐。"""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.evaluation_runs: dict[str, dict[str, Any]] = {}
        self._next_user_id = 1
        self.closed = False

    # ---- users ----
    def init(self) -> None:
        return None

    def create_user(self, username: str, password_hash: str) -> int:
        uid = self._next_user_id
        self._next_user_id += 1
        self.users[uid] = {"id": uid, "username": username, "password_hash": password_hash}
        return uid

    def get_user_by_username(self, username: str) -> Optional[dict[str, Any]]:
        return next((u for u in self.users.values() if u["username"] == username), None)

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        return self.users.get(user_id)

    # ---- documents ----
    def save_document(self, doc_id: str, filename: str, file_type: str, file_size: int, chunk_count: int) -> None:
        self.documents[doc_id] = {
            "id": doc_id, "filename": filename, "file_type": file_type,
            "file_size": file_size, "chunk_count": chunk_count, "uploaded_at": "2026-01-01 00:00:00",
        }

    def list_documents(self) -> list[dict[str, Any]]:
        return list(self.documents.values())

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        return self.documents.get(doc_id)

    def delete_document(self, doc_id: str) -> None:
        self.documents.pop(doc_id, None)

    # ---- conversations ----
    def create_conversation(self, title: str = "") -> str:
        cid = str(uuid.uuid4())
        self.conversations[cid] = {
            "id": cid, "title": title, "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        }
        return cid

    def set_conversation_title(self, conv_id: str, title: str) -> None:
        if conv_id in self.conversations:
            self.conversations[conv_id]["title"] = title

    def list_conversations(self) -> list[dict[str, Any]]:
        out = []
        for c in self.conversations.values():
            row = dict(c)
            row["message_count"] = sum(1 for m in self.messages if m["conversation_id"] == c["id"])
            out.append(row)
        return out

    def get_conversation(self, conv_id: str) -> Optional[dict[str, Any]]:
        c = self.conversations.get(conv_id)
        if c is None:
            return None
        msgs = []
        for m in self.messages:
            if m["conversation_id"] != conv_id:
                continue
            item = {"role": m["role"], "content": m["content"]}
            if m.get("sources"):
                item["sources"] = m["sources"]
            msgs.append(item)
        return {
            "conversation_id": conv_id, "title": c["title"],
            "created_at": c["created_at"], "updated_at": c["updated_at"], "messages": msgs,
        }

    def delete_conversation(self, conv_id: str) -> None:
        # 与真实实现的修复后行为一致：连同消息一起删
        self.conversations.pop(conv_id, None)
        self.messages = [m for m in self.messages if m["conversation_id"] != conv_id]

    # ---- messages ----
    def add_message(self, conv_id: str, role: str, content: str, sources: Optional[list] = None) -> str:
        mid = str(uuid.uuid4())
        self.messages.append({"id": mid, "conversation_id": conv_id, "role": role, "content": content, "sources": sources})
        return mid

    def count_orphan_messages(self) -> int:
        return sum(1 for m in self.messages if m["conversation_id"] not in self.conversations)

    def purge_orphan_messages(self) -> int:
        before = len(self.messages)
        self.messages = [m for m in self.messages if m["conversation_id"] in self.conversations]
        return before - len(self.messages)

    # ---- evaluation ----
    def save_evaluation_run(self, metrics: dict) -> str:
        rid = str(uuid.uuid4())
        self.evaluation_runs[rid] = {"id": rid, "metrics": metrics, "created_at": "2026-01-01 00:00:00"}
        return rid

    def list_evaluation_runs(self) -> list[dict[str, Any]]:
        return list(self.evaluation_runs.values())

    def close(self) -> None:
        self.closed = True


# ---------- fixtures ----------

@pytest.fixture
def settings(tmp_path) -> Settings:
    """所有可写路径指向 tmp_path，避免测试污染仓库 data/ 目录。"""
    return dataclasses.replace(
        Settings(),
        data_dir=str(tmp_path),
        documents_dir=str(tmp_path / "documents"),
        chroma_persist_dir=str(tmp_path / "chroma_db"),
    )


@pytest.fixture
def fake_db(monkeypatch) -> FakeDatabase:
    db = FakeDatabase()
    monkeypatch.setattr("app.container.Database", lambda *a, **k: db)
    return db


@pytest.fixture
def app(settings: Settings, fake_db: FakeDatabase):
    from app.factory import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def token(client) -> str:
    resp = client.post("/auth/register", json={"username": "tester", "password": "test123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def services(settings: Settings):
    """直接组装服务（不经 API / DB），用于离线链路测试。"""
    from app.services.ingestion import IngestionService
    from app.services.rag import RAGService

    embedder = HashEmbedding(settings.hash_embedding_dim)
    store = InMemoryVectorStore()
    llm = FakeLLMClient()
    reranker = NoopReranker()
    ingestion = IngestionService(
        embedder=embedder, vector_store=store, db=FakeDatabase(),
        documents_dir=settings.documents_dir, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
    )
    rag = RAGService(
        embedder=embedder, vector_store=store, llm=llm, reranker=reranker,
        top_k=settings.top_k, rerank_top_k=settings.rerank_top_k,
        max_history_messages=settings.max_history_messages,
    )
    return {"settings": settings, "embedder": embedder, "store": store, "llm": llm,
            "ingestion": ingestion, "rag": rag}
