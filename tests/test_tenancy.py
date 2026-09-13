"""多租户与 RBAC 验收（V2，见 docs/v2-plan.md §6.4）。

三层强制点**各自都要有断言**，缺一层就是一个漏洞：

1. **路由 / 角色层** —— 角色矩阵逐条；
2. **DAO 层** —— 跨租户查不到（文档 / 会话 / 评测记录）；
3. **向量库层** —— 跨租户检索不到（多租户 RAG 最容易漏的一环）。

这里全部跑在离线组件上（`FakeDatabase` + `InMemoryVectorStore`）。真实 MySQL + 真实 Chroma
的同类验证在 `data/` 下的验收脚本里单独跑 —— 因为假库的隔离语义是"照着真库写"的，
**假库通过不等于真库通过**（这一条是刻意的设计取舍，不是遗漏）。
"""
from __future__ import annotations

import dataclasses
import io

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app

PASSWORD = "test123456"


# ---------- 工具 ----------

def _upload(client: TestClient, headers: dict, name: str = "制度.txt", text: str = "员工考勤制度：年假 10 天。"):
    return client.post(
        "/upload",
        files={"file": (name, io.BytesIO(text.encode("utf-8")), "text/plain")},
        headers=headers,
    )


def _login(client: TestClient, username: str, tenant: str | None = None) -> dict[str, str]:
    payload = {"username": username, "password": PASSWORD}
    if tenant is not None:
        payload["tenant"] = tenant
    resp = client.post("/auth/login", json=payload)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def admin_b_auth(client, other_tenant) -> dict[str, str]:
    """`tenant-b` 租户自己的 admin（与默认租户的 admin 是两个不同的人）。"""
    client.app.state.container.auth.create_user("boss-b", PASSWORD, other_tenant, "admin")
    return _login(client, "boss-b", other_tenant)


# ---------- 1. 租户标识与账号 ----------

def test_same_username_allowed_in_different_tenants(client, auth, other_tenant_auth):
    """同名用户可跨租户共存：唯一键是 (tenant_id, username) 而不是 username。"""
    assert client.get("/conversations", headers=auth).status_code == 200
    assert client.get("/conversations", headers=other_tenant_auth).status_code == 200

    # 反向确认：同一租户内仍然不允许重名
    dup = client.post("/auth/register", json={"username": "tester", "password": PASSWORD})
    assert dup.status_code == 409, dup.text


def test_tenant_is_normalized_on_register(client):
    """租户标识会被归一化（去空白、非法字符替换），避免 `" A "` 与 `"A"` 变成两个租户。"""
    resp = client.post(
        "/auth/register",
        json={"username": "norm-user", "password": PASSWORD, "tenant": "  Acme Corp  "},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "Acme-Corp"


def test_registered_user_defaults_to_user_role(client):
    resp = client.post("/auth/register", json={"username": "plain-user", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "user"


def test_bootstrap_username_becomes_admin(client):
    """`BOOTSTRAP_ADMIN_USERNAME` 指定的账号注册即 admin（否则新库永远没有管理员）。"""
    resp = client.post("/auth/register", json={"username": "root", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


def test_self_register_can_be_disabled(settings: Settings, fake_db):
    """`ALLOW_SELF_REGISTER=false` 时注册被拒，但仍可用已有账号登录。"""
    cfg = dataclasses.replace(settings, allow_self_register=False)
    c = TestClient(create_app(cfg))

    resp = c.post("/auth/register", json={"username": "whoever", "password": PASSWORD})
    assert resp.status_code == 403, resp.text

    # admin 代建后可以登录（自助注册关闭不应影响既有账号）
    c.app.state.container.auth.create_user("invited", PASSWORD, "default", "user")
    assert c.post("/auth/login", json={"username": "invited", "password": PASSWORD}).status_code == 200


# ---------- 2. DAO 层：跨租户查不到 ----------

def test_documents_are_isolated_between_tenants(client, auth, other_tenant_auth):
    resp = _upload(client, auth, name="a-only.txt", text="租户 A 的文档：代号 ALPHA-ONE。")
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["document_id"]

    # B 租户列表里没有
    assert client.get("/documents", headers=other_tenant_auth).json() == []
    # B 租户直接拿 id 也删不掉（且不是 200）
    assert client.delete(f"/documents/{doc_id}", headers=other_tenant_auth).status_code != 200
    # A 租户自己看得到，也还在
    assert [d["document_id"] for d in client.get("/documents", headers=auth).json()] == [doc_id]


def test_conversations_are_isolated_between_tenants(client, auth, other_tenant_auth):
    resp = client.post("/ask", json={"question": "你好"}, headers=auth)
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["conversation_id"]

    # 跨租户按"不存在"处理（404），不暴露他人会话是否存在
    assert client.get(f"/conversations/{conv_id}", headers=other_tenant_auth).status_code == 404
    assert client.get("/conversations", headers=other_tenant_auth).json() == []

    mine = client.get("/conversations", headers=auth).json()
    assert [c["conversation_id"] for c in mine] == [conv_id]


def test_cross_tenant_cannot_delete_conversation(client, auth, other_tenant_auth):
    conv_id = client.post("/ask", json={"question": "你好"}, headers=auth).json()["conversation_id"]
    assert client.delete(f"/conversations/{conv_id}", headers=other_tenant_auth).status_code == 404
    # A 租户的会话必须完好无损
    assert client.get(f"/conversations/{conv_id}", headers=auth).status_code == 200


def test_evaluation_runs_are_isolated_between_tenants(client, admin_auth, admin_b_auth):
    assert client.post("/evaluation/run", json={}, headers=admin_auth).status_code == 200
    assert client.get("/evaluation/runs", headers=admin_auth).json() != []
    assert client.get("/evaluation/runs", headers=admin_b_auth).json() == []


# ---------- 3. 向量库层：跨租户检索不到 ----------

def test_retrieval_is_scoped_by_tenant(client, tmp_path, other_tenant):
    """**多租户 RAG 最关键的一条**：向量检索必须按租户过滤。

    两个租户各灌一篇内容迥异的文档，用同一个查询去检索：
    任何一次检索都不允许命中对方租户的片段。
    """
    container = client.app.state.container
    file_a = tmp_path / "a.txt"
    file_a.write_text("租户 A 专属知识：代号 ALPHA-ONE 对应报销上限 600 元。", encoding="utf-8")
    file_b = tmp_path / "b.txt"
    file_b.write_text("租户 B 专属知识：代号 BRAVO-TWO 对应报销上限 900 元。", encoding="utf-8")

    container.ingestion.ingest(str(file_a), "a.txt", "default")
    container.ingestion.ingest(str(file_b), "b.txt", other_tenant)

    def names(tenant_id) -> set[str]:
        hits = container.rag.search("专属知识 代号 报销上限", top_k=10, tenant_id=tenant_id)
        return {h.metadata.get("document_name") for h in hits}

    assert names("default") == {"a.txt"}
    assert names(other_tenant) == {"b.txt"}

    # 不传租户（None）= 不过滤，是 V1 的旧行为；确认这条"逃生舱"没有被误当成默认值
    assert names(None) == {"a.txt", "b.txt"}


def test_retrieval_does_not_leak_even_with_large_top_k(client, tmp_path, other_tenant):
    """把 top_k 拉大也不能把别的租户"捞"进来（防止实现里用先取全量再截断的写法漏过滤）。"""
    container = client.app.state.container
    file_a = tmp_path / "a.txt"
    file_a.write_text("A 租户文档：ZEBRA-ALPHA。", encoding="utf-8")
    file_b = tmp_path / "b.txt"
    file_b.write_text("B 租户文档：ZEBRA-BRAVO。", encoding="utf-8")
    container.ingestion.ingest(str(file_a), "a.txt", "default")
    container.ingestion.ingest(str(file_b), "b.txt", other_tenant)

    hits = container.rag.search("文档", top_k=1000, tenant_id="default")
    assert hits, "本租户应该能检索到自己的片段"
    assert all(h.metadata.get("tenant_id") == "default" for h in hits)


# ---------- 4. 角色矩阵 ----------

def test_viewer_can_ask_but_cannot_upload(client, viewer_auth):
    assert client.post("/ask", json={"question": "你好"}, headers=viewer_auth).status_code == 200
    assert _upload(client, viewer_auth).status_code == 403


def test_user_cannot_delete_document(client, auth):
    doc_id = _upload(client, auth).json()["document_id"]
    resp = client.delete(f"/documents/{doc_id}", headers=auth)
    assert resp.status_code == 403, resp.text
    # 文档必须还在（403 不能是"删了才报错"）
    assert [d["document_id"] for d in client.get("/documents", headers=auth).json()] == [doc_id]


def test_admin_can_delete_document(client, admin_auth):
    doc_id = _upload(client, admin_auth).json()["document_id"]
    assert client.delete(f"/documents/{doc_id}", headers=admin_auth).status_code == 200
    assert client.get("/documents", headers=admin_auth).json() == []


@pytest.mark.parametrize(
    "method_path",
    [("post", "/evaluation/run"), ("get", "/evaluation/runs")],
)
def test_evaluation_requires_admin(client, auth, viewer_auth, method_path):
    method, path = method_path
    for headers in (auth, viewer_auth):
        resp = getattr(client, method)(path, headers=headers, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 403, f"{method} {path} 应仅限 admin，实际 {resp.status_code}"


def test_admin_can_run_evaluation(client, admin_auth):
    assert client.post("/evaluation/run", json={}, headers=admin_auth).status_code == 200
    assert client.get("/evaluation/runs", headers=admin_auth).status_code == 200


@pytest.mark.parametrize("method", ["get", "post"])
def test_admin_users_requires_admin(client, auth, viewer_auth, method, admin_auth):
    for headers in (auth, viewer_auth):
        resp = getattr(client, method)(
            "/admin/users", headers=headers, **({"json": {"username": "x1", "password": PASSWORD}} if method == "post" else {})
        )
        assert resp.status_code == 403, f"{method} /admin/users 应仅限 admin，实际 {resp.status_code}"


def test_anonymous_is_rejected(client):
    assert client.get("/documents").status_code == 401
    assert client.get("/admin/users").status_code == 401
    assert client.post("/evaluation/run", json={}).status_code == 401


# ---------- 5. /admin/users 的租户边界 ----------

def test_admin_users_lists_only_own_tenant(client, admin_auth, admin_b_auth, auth, other_tenant):
    """默认租户的 admin 只该看到默认租户的人，看不到 tenant-b 的账号。"""
    rows = client.get("/admin/users", headers=admin_auth).json()
    usernames = {r["username"] for r in rows}
    assert {r["tenant_id"] for r in rows} == {"default"}
    assert "boss-b" not in usernames
    assert "tester" in usernames  # auth fixture 注册的普通用户

    rows_b = client.get("/admin/users", headers=admin_b_auth).json()
    assert {r["tenant_id"] for r in rows_b} == {other_tenant}
    assert {r["username"] for r in rows_b} == {"boss-b"}


def test_admin_can_create_user_in_own_tenant(client, admin_auth):
    resp = client.post(
        "/admin/users",
        json={"username": "newbie", "password": PASSWORD, "role": "viewer"},
        headers=admin_auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "newbie" and body["role"] == "viewer"
    assert body["tenant_id"] == "default"
    assert "password" not in body

    # 建出来的账号可以直接登录，且角色就是 viewer
    headers = _login(client, "newbie")
    assert _upload(client, headers).status_code == 403


def test_admin_cannot_create_user_in_another_tenant(client, admin_auth, other_tenant):
    resp = client.post(
        "/admin/users",
        json={"username": "sneaky", "password": PASSWORD, "role": "admin", "tenant": other_tenant},
        headers=admin_auth,
    )
    assert resp.status_code == 403, resp.text
    # 不能静默改写租户：确认 tenant-b 里没有建出这个账号
    assert client.post("/auth/login", json={"username": "sneaky", "password": PASSWORD, "tenant": other_tenant}).status_code == 401


def test_duplicate_username_in_same_tenant_conflicts(client, admin_auth):
    payload = {"username": "dup-user", "password": PASSWORD, "role": "user"}
    assert client.post("/admin/users", json=payload, headers=admin_auth).status_code == 200
    assert client.post("/admin/users", json=payload, headers=admin_auth).status_code == 409
