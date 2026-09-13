"""pytest 全局配置：默认离线、可隔离、不依赖 MySQL / 外网 / 本地模型。

三个关键设计：

1. **回退组件必须在 import app.* 之前**通过环境变量设定，否则 `Settings()` 会取到
   真实配置。历史坑：`settings` 曾是"import 时固化"的单例，用 pytest 一次收集
   多个文件时谁先 import 谁说了算（见 docs/review-v1-audit.md §2.3）。
   现在 `Settings` 改为实例化时读环境，测试可显式构造。
2. **FakeDatabase 替换真实 MySQL**，让 API 层的鉴权 / 会话 / 评测都能离线跑。
   真实环境（MySQL + Chroma + bge + DeepSeek）的用例放在 test_integration_real.py，
   默认跳过。
3. **FakeDatabase 必须复刻租户隔离语义**（V2）：`tenant_id` 过滤、跨租户同名用户、
   "别人的会话按不存在处理"。如果这里比真实 DAO 宽松，测试就会给出**假绿**——
   多租户改造最容易栽在"测试用假库通过、真库泄漏"上，所以这里的每个查询都带 tenant 条件。
"""
from __future__ import annotations

import dataclasses
import datetime
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

#: 测试里 bootstrap admin 的用户名（fixture `admin_auth` 用它注册）
BOOTSTRAP_ADMIN = "root"
#: 第二个租户标识（fixture `other_tenant_auth` 用）
OTHER_TENANT = "tenant-b"


class FakeDatabase:
    """内存版 Database，签名与 app.models.database.Database 对齐（含租户过滤）。"""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.evaluation_runs: dict[str, dict[str, Any]] = {}
        self.invites: dict[str, dict[str, Any]] = {}
        self._next_user_id = 1
        self.closed = False

    # ---- users ----
    def init(self) -> None:
        return None

    def create_user(
        self,
        username: str,
        password_hash: str,
        tenant_id: str = "default",
        role: str = "user",
    ) -> int:
        uid = self._next_user_id
        self._next_user_id += 1
        self.users[uid] = {
            "id": uid,
            "username": username,
            "password_hash": password_hash,
            "tenant_id": tenant_id,
            "role": role,
            "created_at": "2026-01-01 00:00:00",
        }
        return uid

    def get_user_by_username(self, username: str, tenant_id: str = "default") -> Optional[dict[str, Any]]:
        """用户名在**租户内**唯一：同名但不同租户互不影响。"""
        return next(
            (
                u
                for u in self.users.values()
                if u["username"] == username and u["tenant_id"] == tenant_id
            ),
            None,
        )

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        return self.users.get(user_id)

    def list_users(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        return [
            {
                "user_id": u["id"],
                "username": u["username"],
                "role": u["role"],
                "tenant_id": u["tenant_id"],
                "created_at": u["created_at"],
            }
            for u in sorted(self.users.values(), key=lambda x: x["id"])
            if u["tenant_id"] == tenant_id
        ]

    # ---- documents ----
    def save_document(
        self,
        doc_id: str,
        filename: str,
        file_type: str,
        file_size: int,
        chunk_count: int,
        tenant_id: str = "default",
    ) -> None:
        self.documents[doc_id] = {
            "id": doc_id, "filename": filename, "file_type": file_type,
            "file_size": file_size, "chunk_count": chunk_count,
            "tenant_id": tenant_id, "uploaded_at": "2026-01-01 00:00:00",
        }

    def list_documents(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        return [d for d in self.documents.values() if d["tenant_id"] == tenant_id]

    def get_document(self, doc_id: str, tenant_id: str = "default") -> Optional[dict[str, Any]]:
        doc = self.documents.get(doc_id)
        if doc is None or doc["tenant_id"] != tenant_id:
            return None
        return doc

    def delete_document(self, doc_id: str, tenant_id: str = "default") -> None:
        doc = self.documents.get(doc_id)
        if doc is not None and doc["tenant_id"] == tenant_id:
            del self.documents[doc_id]

    def delete_all_documents(self, tenant_id: str = "default") -> int:
        targets = [k for k, v in self.documents.items() if v["tenant_id"] == tenant_id]
        for key in targets:
            del self.documents[key]
        return len(targets)

    # ---- conversations ----
    def create_conversation(self, title: str = "", tenant_id: str = "default") -> str:
        cid = str(uuid.uuid4())
        self.conversations[cid] = {
            "id": cid, "title": title, "tenant_id": tenant_id,
            "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        }
        return cid

    def set_conversation_title(self, conv_id: str, title: str, tenant_id: str = "default") -> None:
        c = self.conversations.get(conv_id)
        if c is not None and c["tenant_id"] == tenant_id:
            c["title"] = title

    def list_conversations(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        out = []
        for c in self.conversations.values():
            if c["tenant_id"] != tenant_id:
                continue
            row = dict(c)
            row["message_count"] = sum(1 for m in self.messages if m["conversation_id"] == c["id"])
            out.append(row)
        return out

    def get_conversation(self, conv_id: str, tenant_id: str = "default") -> Optional[dict[str, Any]]:
        c = self.conversations.get(conv_id)
        # 不属于本租户的会话一律按"不存在"处理（与真实实现一致，避免暴露存在性）
        if c is None or c["tenant_id"] != tenant_id:
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

    def delete_conversation(self, conv_id: str, tenant_id: str = "default") -> None:
        c = self.conversations.get(conv_id)
        if c is None or c["tenant_id"] != tenant_id:
            return
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
    def save_evaluation_run(self, metrics: dict, tenant_id: str = "default") -> str:
        rid = str(uuid.uuid4())
        self.evaluation_runs[rid] = {
            "id": rid, "metrics": metrics, "tenant_id": tenant_id, "created_at": "2026-01-01 00:00:00",
        }
        return rid

    def list_evaluation_runs(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        return [r for r in self.evaluation_runs.values() if r["tenant_id"] == tenant_id]

    # ---- invites ----
    @staticmethod
    def _fmt(moment: datetime.datetime) -> str:
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def create_invite(
        self,
        code: str,
        tenant_id: str = "default",
        role: str = "user",
        created_by: Optional[int] = None,
        max_uses: int = 1,
        expires_in_hours: int = 0,
    ) -> None:
        expires = (
            self._fmt(datetime.datetime.now() + datetime.timedelta(hours=int(expires_in_hours)))
            if expires_in_hours and int(expires_in_hours) > 0
            else None
        )
        self.invites[code] = {
            "code": code, "tenant_id": tenant_id, "role": role, "created_by": created_by,
            "max_uses": int(max_uses), "used_count": 0, "expires_at": expires,
            "created_at": "2026-01-01 00:00:00",
        }

    def get_invite_by_code(self, code: str) -> Optional[dict[str, Any]]:
        """按码全局查（注册时还不知道租户，租户正是由码决定的）。"""
        return self.invites.get(code)

    def get_invite(self, code: str, tenant_id: str = "default") -> Optional[dict[str, Any]]:
        row = self.invites.get(code)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        return row

    def list_invites(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        return [r for r in self.invites.values() if r["tenant_id"] == tenant_id]

    def delete_invite(self, code: str, tenant_id: str = "default") -> None:
        row = self.invites.get(code)
        if row is not None and row["tenant_id"] == tenant_id:
            del self.invites[code]

    def consume_invite(self, code: str, tenant_id: str = "default") -> Optional[dict[str, Any]]:
        """与真实实现同语义：不存在 / 跨租户 / 已过期 / 已用尽 → None，成功则额度 +1。"""
        row = self.invites.get(code)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= self._fmt(datetime.datetime.now()):
            return None
        if int(row["max_uses"]) != 0 and int(row["used_count"]) >= int(row["max_uses"]):
            return None
        row["used_count"] += 1
        return dict(row)

    def close(self) -> None:
        self.closed = True


# ---------- fixtures ----------

@pytest.fixture
def settings(tmp_path) -> Settings:
    """所有可写路径指向 tmp_path，避免测试污染仓库 data/ 目录。

    `bootstrap_admin_username` 设为 `root`：新注册的 `root` 是该租户 admin，
    供 `admin_auth` fixture 使用（其余注册用户一律是 `user`）。
    """
    return dataclasses.replace(
        Settings(),
        data_dir=str(tmp_path),
        documents_dir=str(tmp_path / "documents"),
        chroma_persist_dir=str(tmp_path / "chroma_db"),
        bootstrap_admin_username=BOOTSTRAP_ADMIN,
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


def _register(client: TestClient, username: str, tenant: str | None = None) -> dict:
    payload = {"username": username, "password": "test123456"}
    if tenant is not None:
        payload["tenant"] = tenant
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def token(client) -> str:
    return _register(client, "tester")["token"]


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_auth(client) -> dict[str, str]:
    """默认租户的管理员：`BOOTSTRAP_ADMIN_USERNAME` 指定的账号注册即 admin。"""
    data = _register(client, BOOTSTRAP_ADMIN)
    assert data["role"] == "admin", f"bootstrap 账号应为 admin，实际 {data}"
    return {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture
def viewer_auth(client) -> dict[str, str]:
    """只读角色。自助注册一律给 `user`，所以 viewer 只能由 admin 代建。"""
    container = client.app.state.container
    container.auth.create_user("a-viewer", "test123456", "default", "viewer")
    resp = client.post("/auth/login", json={"username": "a-viewer", "password": "test123456"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def other_tenant() -> str:
    """第二个租户的标识（供测试断言租户边界，避免在测试里硬编码字符串导致漂移）。"""
    return OTHER_TENANT


@pytest.fixture
def other_tenant_auth(client) -> dict[str, str]:
    """另一个租户的普通用户，**用户名与 `auth` 相同**（跨租户重名必须被允许）。"""
    data = _register(client, "tester", tenant=OTHER_TENANT)
    assert data["tenant_id"] == OTHER_TENANT, data
    return {"Authorization": f"Bearer {data['token']}"}


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
