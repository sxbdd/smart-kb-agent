"""路由层自测（Role-R）：角色矩阵 + 租户透传 + `/admin/users` 行为。

设计取舍（为什么不用 `tests/conftest.py` 的 `client` / `token` fixture）：

- 本文件只验证**路由层自身**的正确性，所以直接用 `FastAPI()` 挂上被测路由，
  并**只覆盖 `get_current_user` 这一个依赖**——`require_role` / `require_admin`
  是真实实现，因此角色判断、403 语义、依赖链都是真的被跑到的。
- `db` / `conversation` / `ingestion` / `auth` 用内存替身：`AuthService` 用**真实实现**
  （账号唯一性 409、角色归一化都走真代码），只替换它依赖的 db。
  这样不依赖 Lead 正在改造的 `app/models/database.py` 与 `tests/conftest.py`，
  本文件在重构期间也能独立给出信号。
- 租户隔离的断言方式：让替身 db 记录**收到的 tenant_id**，断言等于调用者租户。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import (
    routes_admin,
    routes_ask,
    routes_auth,
    routes_conversations,
    routes_documents,
    routes_evaluation,
    routes_upload,
)
from app.api.deps import get_current_user
from app.models.schemas import AskResponse, DocumentUploadResponse
from app.services.auth import AuthService
from app.services.tenancy import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, Principal
from app.utils.exceptions import AppError

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


# --------------------------------------------------------------------------- #
# 替身：内存 DB / 服务
# --------------------------------------------------------------------------- #

class StubDB:
    """满足路由层用到的 DAO 签名，并记录租户参数以断言"透传到位"。"""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.evaluation_runs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._next_user_id = 1

    # ---- users ----
    def create_user(self, username: str, password_hash: str, tenant_id: str, role: str) -> int:
        self.calls.append(("create_user", (username, tenant_id, role)))
        uid = self._next_user_id
        self._next_user_id += 1
        self.users[uid] = {
            "id": uid, "user_id": uid, "username": username, "password_hash": password_hash,
            "tenant_id": tenant_id, "role": role, "created_at": "2026-01-01 00:00:00",
        }
        return uid

    def get_user_by_username(self, username: str, tenant_id: Optional[str] = None) -> Optional[dict]:
        self.calls.append(("get_user_by_username", (username, tenant_id)))
        for u in self.users.values():
            if u["username"] == username and (tenant_id is None or u["tenant_id"] == tenant_id):
                return u
        return None

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        return self.users.get(user_id)

    def list_users(self, tenant_id: str) -> list[dict]:
        self.calls.append(("list_users", (tenant_id,)))
        return [u for u in self.users.values() if u["tenant_id"] == tenant_id]

    # ---- documents ----
    def list_documents(self, tenant_id: str) -> list[dict]:
        self.calls.append(("list_documents", (tenant_id,)))
        return [d for d in self.documents.values() if d["tenant_id"] == tenant_id]

    def get_document(self, doc_id: str, tenant_id: str) -> Optional[dict]:
        self.calls.append(("get_document", (doc_id, tenant_id)))
        doc = self.documents.get(doc_id)
        return doc if doc and doc["tenant_id"] == tenant_id else None

    def delete_document(self, doc_id: str, tenant_id: str) -> None:
        self.calls.append(("delete_document", (doc_id, tenant_id)))
        self.documents.pop(doc_id, None)

    # ---- conversations ----
    def create_conversation(self, title: str = "", tenant_id: str = "default") -> str:
        cid = str(uuid.uuid4())
        self.conversations[cid] = {
            "id": cid, "title": title, "tenant_id": tenant_id,
            "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00", "messages": [],
        }
        return cid

    def list_conversations(self, tenant_id: str) -> list[dict]:
        self.calls.append(("list_conversations", (tenant_id,)))
        return [c for c in self.conversations.values() if c["tenant_id"] == tenant_id]

    def get_conversation(self, conv_id: str, tenant_id: str) -> Optional[dict]:
        self.calls.append(("get_conversation", (conv_id, tenant_id)))
        c = self.conversations.get(conv_id)
        if c is None or c["tenant_id"] != tenant_id:
            return None
        return {
            "conversation_id": conv_id, "title": c["title"],
            "created_at": c["created_at"], "updated_at": c["updated_at"], "messages": c["messages"],
        }

    def set_conversation_title(self, conv_id: str, title: str, tenant_id: str) -> None:
        self.calls.append(("set_conversation_title", (conv_id, title, tenant_id)))
        if conv_id in self.conversations:
            self.conversations[conv_id]["title"] = title

    def delete_conversation(self, conv_id: str, tenant_id: str) -> None:
        self.calls.append(("delete_conversation", (conv_id, tenant_id)))
        self.conversations.pop(conv_id, None)

    # ---- evaluation ----
    def save_evaluation_run(self, metrics: dict, tenant_id: str) -> str:
        rid = str(uuid.uuid4())
        self.evaluation_runs[rid] = {"id": rid, "metrics": metrics, "tenant_id": tenant_id}
        return rid

    def list_evaluation_runs(self, tenant_id: str) -> list[dict]:
        self.calls.append(("list_evaluation_runs", (tenant_id,)))
        return [r for r in self.evaluation_runs.values() if r["tenant_id"] == tenant_id]


class StubConversation:
    """对话服务替身：只为让 `/ask`、`/conversations` 走到路由的返回语句。"""

    def __init__(self, db: StubDB) -> None:
        self.db = db
        self.seen_tenant: Optional[str] = None

    def ask(self, question, conversation_id=None, top_k=None, tenant_id="default"):
        self.seen_tenant = tenant_id
        cid = conversation_id or str(uuid.uuid4())
        # 用真实响应模型，保证路由的 response_model 校验也一起被跑到
        return AskResponse(
            answer="stub", sources=[], conversation_id=cid, mode="chat",
            timestamp="2026-01-01T00:00:00Z",
        )

    def list_conversations(self, tenant_id="default"):
        self.seen_tenant = tenant_id
        rows = self.db.list_conversations(tenant_id)
        # 补上路由要用的 message_count（真实 DAO 由 SQL 聚合出来）
        return [{**r, "message_count": len(r.get("messages") or [])} for r in rows]


class StubIngestion:
    def __init__(self) -> None:
        self.seen_tenant: Optional[str] = None

    def ingest(self, file_path, filename, tenant_id="default"):
        self.seen_tenant = tenant_id
        return DocumentUploadResponse(document_id="doc-1", filename=filename, chunk_count=3,
                                      status="success", message=None)

    def delete(self, document_id, tenant_id="default"):
        self.seen_tenant = tenant_id


class StubRAG:
    def answer(self, question, top_k=None, history=None):
        return "", []


class StubLimiter:
    window = 60
    limit = 1000

    def check(self, key) -> bool:
        return True


def build_client(tmp_path: Path) -> tuple[TestClient, StubDB, StubConversation, StubIngestion]:
    """组装"只含被测路由"的应用；真实 `create_app()` 由 Lead 挂载 routes_admin。"""
    db = StubDB()
    conversation = StubConversation(db)
    ingestion = StubIngestion()
    # settings 只需上传路由用到的三项（落盘目录 + 大小上限）
    settings = SimpleNamespace(
        documents_dir=str(tmp_path / "documents"),
        max_upload_bytes=8 * 1024 * 1024,
        max_upload_mb=8,
    )
    container = SimpleNamespace(
        db=db,
        conversation=conversation,
        ingestion=ingestion,
        auth=AuthService(db=db, jwt_secret="unit-test-secret", jwt_expire_minutes=30),
        auth_limiter=StubLimiter(),
        rag=StubRAG(),
        settings=settings,
    )
    app = FastAPI()
    app.state.container = container

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    for module in (routes_auth, routes_upload, routes_ask, routes_documents,
                   routes_conversations, routes_evaluation, routes_admin):
        app.include_router(module.router)

    client = TestClient(app)
    return client, db, conversation, ingestion


def as_principal(client: TestClient, principal: Principal) -> None:
    """只覆盖身份依赖，其余依赖（require_role / require_admin）保持真实实现。"""
    client.app.dependency_overrides[get_current_user] = lambda: principal


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def ctx(tmp_path):
    client, db, conversation, ingestion = build_client(tmp_path)
    # 每租户各造一个 admin / user / viewer，便于直接按角色发请求
    for tenant in (TENANT_A, TENANT_B):
        for role in (ROLE_VIEWER, ROLE_USER, ROLE_ADMIN):
            db.create_user(f"{role}-{tenant}", "x", tenant, role)
    db.users[100] = {"id": 100, "user_id": 100, "username": "a-docowner", "tenant_id": TENANT_A,
                     "role": ROLE_ADMIN, "created_at": "2026-01-01 00:00:00"}
    db.documents["doc-a"] = {"id": "doc-a", "tenant_id": TENANT_A, "filename": "a.md",
                             "file_type": "md", "file_size": 1, "chunk_count": 1,
                             "uploaded_at": "2026-01-01 00:00:00"}
    db.conversations["conv-a"] = {"id": "conv-a", "title": "t", "tenant_id": TENANT_A,
                                  "created_at": "2026-01-01 00:00:00",
                                  "updated_at": "2026-01-01 00:00:00", "messages": []}
    return SimpleNamespace(client=client, db=db, conversation=conversation, ingestion=ingestion)


def make_principal(role: str, tenant_id: str = TENANT_A, user_id: int = 1) -> Principal:
    return Principal(user_id=user_id, username=f"{role}-{tenant_id}", tenant_id=tenant_id, role=role)


# --------------------------------------------------------------------------- #
# 角色矩阵（见 docs/v2-plan.md §6.2，逐条对应）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER, ROLE_ADMIN])
def test_ask_allows_every_role(ctx, role):
    as_principal(ctx.client, make_principal(role))
    resp = ctx.client.post("/ask", json={"question": "hi"})
    assert resp.status_code == 200
    assert ctx.conversation.seen_tenant == TENANT_A, "必须把调用者租户透传给服务层"


def test_upload_rejects_viewer(ctx):
    as_principal(ctx.client, make_principal(ROLE_VIEWER))
    resp = ctx.client.post("/upload", files={"file": ("a.txt", b"hello", "text/plain")})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_upload_allows_user_and_admin(ctx, role):
    as_principal(ctx.client, make_principal(role))
    resp = ctx.client.post("/upload", files={"file": ("a.txt", b"hello", "text/plain")})
    assert resp.status_code == 200
    assert ctx.ingestion.seen_tenant == TENANT_A


@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER, ROLE_ADMIN])
def test_documents_list_allows_every_role_and_passes_tenant(ctx, role):
    as_principal(ctx.client, make_principal(role))
    resp = ctx.client.get("/documents")
    assert resp.status_code == 200
    assert ("list_documents", (TENANT_A,)) in ctx.db.calls


@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER])
def test_delete_document_rejects_non_admin(ctx, role):
    as_principal(ctx.client, make_principal(role))
    assert ctx.client.delete("/documents/doc-a").status_code == 403


def test_delete_document_allows_admin(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN))
    resp = ctx.client.delete("/documents/doc-a")
    assert resp.status_code == 200
    assert ("get_document", ("doc-a", TENANT_A)) in ctx.db.calls


def test_delete_document_other_tenant_is_404(ctx):
    """跨租户删除：DAO 带租户条件 → 表现为"不存在"，不泄漏它是否存在。"""
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_B))
    assert ctx.client.delete("/documents/doc-a").status_code == 404


@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER, ROLE_ADMIN])
def test_conversation_endpoints_allow_every_role(ctx, role):
    """矩阵：读/改/删**自己的**会话三行都是 ✅；本版本归属只做到租户级。"""
    as_principal(ctx.client, make_principal(role))
    assert ctx.client.get("/conversations").status_code == 200
    assert ctx.client.get("/conversations/conv-a").status_code == 200
    assert ctx.client.patch("/conversations/conv-a", json={"title": "new"}).status_code == 200
    assert ctx.client.delete("/conversations/conv-a").status_code == 200
    assert ("delete_conversation", ("conv-a", TENANT_A)) in ctx.db.calls


def test_conversation_other_tenant_is_404(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_B))
    assert ctx.client.get("/conversations/conv-a").status_code == 404


@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER])
@pytest.mark.parametrize("method_path", [("post", "/evaluation/run"), ("get", "/evaluation/runs")])
def test_evaluation_rejects_non_admin(ctx, role, method_path):
    as_principal(ctx.client, make_principal(role))
    method, path = method_path
    resp = getattr(ctx.client, method)(path, json={}) if method == "post" else getattr(ctx.client, method)(path)
    assert resp.status_code == 403


def test_evaluation_runs_allows_admin_and_scopes_tenant(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN))
    resp = ctx.client.get("/evaluation/runs")
    assert resp.status_code == 200
    assert ("list_evaluation_runs", (TENANT_A,)) in ctx.db.calls


# --------------------------------------------------------------------------- #
# /admin/users
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_USER])
def test_admin_users_rejects_non_admin(ctx, role):
    as_principal(ctx.client, make_principal(role))
    assert ctx.client.get("/admin/users").status_code == 403
    assert ctx.client.post("/admin/users", json={"username": "newbie", "password": "pw123456"}).status_code == 403


def test_admin_users_lists_only_own_tenant(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    resp = ctx.client.get("/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body, "本租户应有用户"
    assert {u["tenant_id"] for u in body} == {TENANT_A}
    assert ("list_users", (TENANT_A,)) in ctx.db.calls


def test_admin_creates_user_in_own_tenant(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    resp = ctx.client.post(
        "/admin/users",
        json={"username": "newbie", "password": "pw123456", "role": ROLE_USER},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "newbie"
    assert body["tenant_id"] == TENANT_A
    assert body["role"] == ROLE_USER
    assert body["user_id"] > 0
    # 响应绝不回显密码
    assert "password" not in body and "password_hash" not in body
    # 账号真的落到了调用者租户
    assert any(u["username"] == "newbie" and u["tenant_id"] == TENANT_A for u in ctx.db.users.values())


def test_admin_create_rejects_other_tenant(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    resp = ctx.client.post(
        "/admin/users",
        json={"username": "sneaky", "password": "pw123456", "tenant": TENANT_B},
    )
    assert resp.status_code == 403
    assert not any(u["username"] == "sneaky" for u in ctx.db.users.values())


def test_admin_create_same_tenant_explicitly_is_ok(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    resp = ctx.client.post(
        "/admin/users",
        json={"username": "a-twin", "password": "pw123456", "tenant": TENANT_A},
    )
    assert resp.status_code == 200, resp.text


def test_admin_create_duplicate_is_409(ctx):
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    payload = {"username": "dupe", "password": "pw123456"}
    assert ctx.client.post("/admin/users", json=payload).status_code == 200
    assert ctx.client.post("/admin/users", json=payload).status_code == 409


def test_admin_create_normalizes_unknown_role_to_viewer(ctx):
    """未知角色必须降级为最低权限（fail-safe），不能默默升级。"""
    as_principal(ctx.client, make_principal(ROLE_ADMIN, tenant_id=TENANT_A))
    resp = ctx.client.post(
        "/admin/users",
        json={"username": "weird", "password": "pw123456", "role": "superuser"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == ROLE_VIEWER


# --------------------------------------------------------------------------- #
# 认证路由：租户透传
# --------------------------------------------------------------------------- #

def test_register_and_login_pass_tenant_through(ctx):
    resp = ctx.client.post(
        "/auth/register",
        json={"username": "alice", "password": "pw123456", "tenant": TENANT_B},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == TENANT_B
    assert body["role"] == ROLE_USER, "非 bootstrap 用户名默认角色应为 user"
    assert body["token"]

    resp = ctx.client.post(
        "/auth/login",
        json={"username": "alice", "password": "pw123456", "tenant": TENANT_B},
    )
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == TENANT_B


def test_register_without_tenant_falls_back_to_default(ctx):
    resp = ctx.client.post("/auth/register", json={"username": "bob", "password": "pw123456"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "default"


# --------------------------------------------------------------------------- #
# 未登录
# --------------------------------------------------------------------------- #

def test_missing_token_is_401(ctx):
    assert ctx.client.get("/documents").status_code == 401
