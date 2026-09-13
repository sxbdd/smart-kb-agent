"""邀请码准入与签发验收（V2，Role-I2）。

三条主线：

1. **准入是真的**：`REQUIRE_INVITE=true` 时普通账号没有邀请码进不来；
   持码则**租户与角色由码决定**，请求体里的 `tenant` 一律被忽略 ——
   否则注册者能自选租户，DAO / 向量库 / RBAC 三层隔离就全建在沙子上。
   唯一例外是 `BOOTSTRAP_ADMIN_USERNAME`（鸡生蛋：开了开关的新库总得有人能进）。
2. **额度语义**：一次性码只能用一次、不限次码可反复用、过期码作废，
   且"用户名已存在"(409) **不白吃**额度 —— 这是最容易写错的一处：
   先扣减再查重的话，攻击者/手滑者可以用一个重名请求把别人的码耗掉。
3. **管理面**：admin 只能签发 / 查看 / 删除**自己租户**的码（403 / 404），
   跨租户删除按"不存在"处理，不泄漏"这个码存在但你无权"。

为什么单独建 `invite_client` 而不复用 `client`：`tests/conftest.py` 的 `settings`
是 `require_invite=False`（既有用例依赖这个默认值），本文件需要一个开了开关的独立应用。
两者共用同一个 `FakeDatabase`（`fake_db` fixture 把 `app.container.Database` 换成了替身），
所以"开关关着时注册出来的账号 / 钉进去的码"在开关打开后依然可见 —— 这也正是
`require_invite_false_*` 那两条回归护栏能和邀请码用例放在同一个文件里的原因。
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app

PASSWORD = "test123456"
DEFAULT_TENANT = "default"
#: 与 `tests/conftest.py` 的 `BOOTSTRAP_ADMIN` / `OTHER_TENANT` 保持一致（硬编码是既有惯例）
BOOTSTRAP_ADMIN = "root"
OTHER_TENANT = "tenant-b"


# ---------- fixtures ----------

@pytest.fixture
def invite_client(settings, fake_db) -> TestClient:
    """`require_invite=True` 的应用（复用同一个 FakeDatabase）。"""
    return TestClient(create_app(dataclasses.replace(settings, require_invite=True)))


@pytest.fixture
def invite_admin_auth(invite_client) -> dict[str, str]:
    """默认租户的 admin：bootstrap 账号**免码**注册。

    刻意不叫 `admin_auth` —— conftest 里已有一个同名 fixture（挂在
    `require_invite=False` 的应用上），同名覆盖会让下一位读者误以为拿到的是同一个应用的身份。
    """
    resp = _register(invite_client, BOOTSTRAP_ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin", resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def invite_admin_b_auth(invite_client) -> dict[str, str]:
    """tenant-b 自己的 admin。

    第二个租户没有 bootstrap 例外（例外只认那一个用户名），所以只能由服务层代建
    —— 与 `tests/test_tenancy.py` 的做法一致，避免在测试里绕过真实注册路径开洞。
    """
    invite_client.app.state.container.auth.create_user("boss-b", PASSWORD, OTHER_TENANT, "admin")
    return _login(invite_client, "boss-b", OTHER_TENANT)


# ---------- 工具 ----------

def _register(
    client: TestClient,
    username: str,
    *,
    tenant: str | None = None,
    invite_code: str | None = None,
):
    """注册请求。`invite_code=""` 与"不带该字段"是两种输入，这里都保留（第 3 条要分别验）。"""
    payload: dict = {"username": username, "password": PASSWORD}
    if tenant is not None:
        payload["tenant"] = tenant
    if invite_code is not None:
        payload["invite_code"] = invite_code
    return client.post("/auth/register", json=payload)


def _login(client: TestClient, username: str, tenant: str | None = None) -> dict[str, str]:
    payload = {"username": username, "password": PASSWORD}
    if tenant is not None:
        payload["tenant"] = tenant
    resp = client.post("/auth/login", json=payload)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _issue(client: TestClient, tenant_id: str, role: str = "user",
           max_uses: int | None = None, expires_in_hours: int | None = None) -> str:
    """直接走服务层签发邀请码。

    多数用例只关心**注册侧**行为，经 HTTP 签发会引入与管理面无关的失败面；
    管理面自身的签发 / 列表 / 删除由 `invite_admin_auth` 那一组用例经 HTTP 覆盖。
    """
    container = client.app.state.container
    return container.auth.create_invite(
        tenant_id, role=role, max_uses=max_uses, expires_in_hours=expires_in_hours
    )["code"]


# --------------------------------------------------------------------------- #
# 1. 准入闸门
# --------------------------------------------------------------------------- #

def test_bootstrap_admin_registers_without_invite(invite_client):
    """鸡生蛋例外：开了邀请码之后，没有这条路径的新库一个人都进不来，
    也就没人能签发第一个码。"""
    resp = _register(invite_client, BOOTSTRAP_ADMIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "admin"
    assert body["tenant_id"] == DEFAULT_TENANT
    assert body["token"]


def test_plain_user_without_invite_is_403(invite_client):
    resp = _register(invite_client, "noinvite")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "注册需要邀请码，请向管理员索取"


@pytest.mark.parametrize(
    "invite_code",
    [None, "", "not-a-real-code-0000"],
    ids=["missing", "empty", "unknown"],
)
def test_missing_empty_or_unknown_invite_is_403(invite_client, invite_code):
    """三种"没有有效凭据"的输入都必须被拒；空串不能因为 Optional 就漏过闸门。"""
    resp = _register(invite_client, "candidate", invite_code=invite_code)
    assert resp.status_code == 403, resp.text
    assert "邀请码" in resp.json()["detail"]


def test_require_invite_false_keeps_legacy_behavior(client):
    """回归护栏：开关关闭时必须回到旧行为（不带码可注册、body 的 tenant 生效）。

    如果哪天有人把闸门写成"永远要求邀请码"，这条会先红 —— 它保护的不是邀请码功能，
    而是**开关本身还能被关掉**（存量部署与既有用例都依赖这个默认值）。"""
    resp = client.post(
        "/auth/register",
        json={"username": "legacy", "password": PASSWORD, "tenant": OTHER_TENANT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == OTHER_TENANT
    assert body["role"] == "user"


def test_require_invite_false_ignores_invite_code(client):
    """开关关闭时连非法邀请码也应当被忽略（旧客户端可能带着一个早已废弃的码）。"""
    resp = client.post(
        "/auth/register",
        json={"username": "legacy2", "password": PASSWORD, "invite_code": "stale-code"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == DEFAULT_TENANT


# --------------------------------------------------------------------------- #
# 2. 有效邀请码：租户与角色由码决定
# --------------------------------------------------------------------------- #

def test_valid_invite_determines_tenant_and_role(invite_client, fake_db):
    """码是 tenant-b + viewer：注册出来的账号就必须落在 tenant-b 且是 viewer。"""
    code = _issue(invite_client, OTHER_TENANT, role="viewer", max_uses=1)

    resp = _register(invite_client, "vip", invite_code=code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == OTHER_TENANT
    assert body["role"] == "viewer"
    assert body["token"]

    # 账号真的落在码指定的租户里，而不是"响应里写着 tenant-b"
    assert fake_db.get_user_by_username("vip", OTHER_TENANT) is not None
    assert fake_db.get_user_by_username("vip", DEFAULT_TENANT) is None
    assert fake_db.invites[code]["used_count"] == 1


def test_body_tenant_is_ignored_when_invite_present(invite_client, fake_db):
    """**准入缺口的核心断言**：拿 tenant-b 的码、body 里写 tenant=default，
    结果必须仍然是 tenant-b —— 否则注册者等于自选了租户。"""
    code = _issue(invite_client, OTHER_TENANT, role="user", max_uses=1)

    resp = _register(invite_client, "sneaky", tenant=DEFAULT_TENANT, invite_code=code)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == OTHER_TENANT
    assert fake_db.get_user_by_username("sneaky", DEFAULT_TENANT) is None
    assert fake_db.get_user_by_username("sneaky", OTHER_TENANT) is not None


def test_role_from_invite_cannot_be_escalated_by_body(invite_client, fake_db):
    """提权尝试必须无效：往 body 里塞 `role: admin` 只会被忽略
    （`RegisterRequest` 没有这个字段，pydantic 默认丢弃多余字段），
    角色只由邀请码决定 —— 这是"注册者无法给自己发 admin"的契约。"""
    code = _issue(invite_client, DEFAULT_TENANT, role="viewer", max_uses=1)
    resp = invite_client.post(
        "/auth/register",
        json={"username": "wannabe", "password": PASSWORD, "invite_code": code, "role": "admin"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"
    assert fake_db.get_user_by_username("wannabe", DEFAULT_TENANT)["role"] == "viewer"


# --------------------------------------------------------------------------- #
# 3. 额度语义
# --------------------------------------------------------------------------- #

def test_single_use_code_rejected_second_time(invite_client, fake_db):
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=1)

    assert _register(invite_client, "first", invite_code=code).status_code == 200

    resp = _register(invite_client, "second", invite_code=code)
    assert resp.status_code == 403, resp.text
    assert "邀请码" in resp.json()["detail"]
    assert fake_db.invites[code]["used_count"] == 1, "用尽后不能再涨计数"
    assert fake_db.get_user_by_username("second", DEFAULT_TENANT) is None


def test_unlimited_code_allows_many_registrations(invite_client, fake_db):
    """`max_uses=0` 表示不限次数：连续注册 3 个不同用户名都应成功。"""
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=0)

    for index in range(3):
        resp = _register(invite_client, f"guest{index}", invite_code=code)
        assert resp.status_code == 200, resp.text

    assert fake_db.invites[code]["max_uses"] == 0
    assert fake_db.invites[code]["used_count"] == 3


def test_expired_code_is_403(invite_client, fake_db):
    """过期码作废。

    `expires_in_hours=0` 的语义是"不过期"，所以这里签发后手工把过期时间改成过去，
    模拟一个已经过期的码 —— 只测注册侧行为，不去碰 DAO 的建码逻辑。
    """
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=5)
    fake_db.invites[code]["expires_at"] = "2000-01-01 00:00:00"

    resp = _register(invite_client, "late", invite_code=code)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "邀请码无效、已用尽或已过期"
    assert fake_db.invites[code]["used_count"] == 0, "过期码不能被扣减"
    assert fake_db.get_user_by_username("late", DEFAULT_TENANT) is None


def test_not_yet_expired_code_works(invite_client, fake_db):
    """过期判定的**正向对照**：如果比较方向写反，上面的过期用例仍会通过，
    只有这条会红。邀请码有效期是安全相关行为，两个方向都要有断言。"""
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=1, expires_in_hours=1)
    assert fake_db.invites[code]["expires_at"] is not None

    resp = _register(invite_client, "ontime", invite_code=code)
    assert resp.status_code == 200, resp.text
    assert fake_db.invites[code]["used_count"] == 1


def test_duplicate_username_409_does_not_burn_quota(invite_client, fake_db):
    """重名(409)**不消耗**额度。

    用 `max_uses=2` 而不是 1，才能在一次 409 之后仍有余量验证"换名还能进"；
    最后用 `used_count == 2` 反证那次 409 没有白吃一次额度。
    """
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=2)

    assert _register(invite_client, "dup", invite_code=code).status_code == 200

    dup = _register(invite_client, "dup", invite_code=code)
    assert dup.status_code == 409, dup.text
    assert fake_db.invites[code]["used_count"] == 1, "409 不能消耗额度"

    again = _register(invite_client, "dup2", invite_code=code)
    assert again.status_code == 200, again.text
    assert fake_db.invites[code]["used_count"] == 2


# --------------------------------------------------------------------------- #
# 4. 管理面：签发 / 列表 / 删除
# --------------------------------------------------------------------------- #

def test_admin_issues_invite_for_own_tenant(invite_client, settings, invite_admin_auth):
    """缺省的 `max_uses` / `expires_in_hours` 交给**服务端配置**决定
    （`INVITE_DEFAULT_MAX_USES` / `INVITE_TTL_HOURS`）：断言对着 `settings` 而不是写死 1，
    这样测的是"路由层没有自己填默认值"这条契约，而不是某个具体数值。"""
    resp = invite_client.post("/admin/invites", json={}, headers=invite_admin_auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == DEFAULT_TENANT
    assert body["role"] == "user"
    assert body["used_count"] == 0
    assert body["max_uses"] == settings.invite_default_max_uses
    # TTL=0 表示不过期；配了 TTL 就必须算出过期时间
    assert (body["expires_at"] is None) == (settings.invite_ttl_hours == 0)
    assert body["created_at"]
    assert body["code"]


def test_admin_can_issue_explicitly_for_own_tenant(invite_client, invite_admin_auth):
    """显式写出自己的租户不算越界（与 `/admin/users` 的既有行为保持一致）。"""
    resp = invite_client.post(
        "/admin/invites",
        json={"role": "viewer", "max_uses": 3, "expires_in_hours": 24, "tenant": DEFAULT_TENANT},
        headers=invite_admin_auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == DEFAULT_TENANT
    assert body["role"] == "viewer"
    assert body["max_uses"] == 3
    assert body["expires_at"] is not None, "给了 expires_in_hours 就必须算出过期时间"


def test_non_platform_admin_cannot_issue_invite_for_other_tenant(
    invite_client, fake_db, invite_admin_b_auth
):
    """不静默改写：**非平台租户**的管理员不能把码签到别的租户（包括 default）。"""
    before = len(fake_db.invites)

    for target in (DEFAULT_TENANT, "tenant-c"):
        resp = invite_client.post(
            "/admin/invites",
            json={"role": "admin", "max_uses": 1, "tenant": target},
            headers=invite_admin_b_auth,
        )
        assert resp.status_code == 403, resp.text
    assert len(fake_db.invites) == before, "403 不能留下任何码"


def test_platform_admin_can_issue_invite_for_another_tenant(
    invite_client, fake_db, invite_admin_auth
):
    """平台租户（DEFAULT_TENANT）的管理员**可以**为其它租户签码。

    这是新租户唯一的开通途径：要签码得先有那个租户的 admin，不放开就成了鸡生蛋。
    跨租户签发只放给平台租户，是有意的最小授权。
    """
    resp = invite_client.post(
        "/admin/invites",
        json={"role": "admin", "max_uses": 1, "tenant": OTHER_TENANT},
        headers=invite_admin_auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == OTHER_TENANT
    assert fake_db.list_invites(OTHER_TENANT) != []


def test_bootstrap_cannot_be_used_to_take_over_another_tenant(invite_client, fake_db):
    """**越权回归**（真机发现并修掉）：

    bootstrap 例外必须只在**默认租户**、且该租户还没有管理员时生效。
    收窄前，`{"username":"root","tenant":"victim-corp"}` 会免码注册成 victim-corp 的 admin ——
    如果那个租户已经存在且没有同名账号，就等于跨租户越权读别人的数据。
    """
    # 先在默认租户用掉 bootstrap（此刻默认租户没有管理员，属于合法引导）
    assert _register(invite_client, BOOTSTRAP_ADMIN).status_code == 200

    takeover = _register(invite_client, BOOTSTRAP_ADMIN, tenant="victim-corp")
    assert takeover.status_code == 403, takeover.text
    assert fake_db.get_user_by_username(BOOTSTRAP_ADMIN, "victim-corp") is None


def test_bootstrap_exception_expires_once_default_tenant_has_an_admin(invite_client):
    """默认租户已经有管理员后，bootstrap 用户名的免码例外自动失效（不能被"抢注"）。"""
    container = invite_client.app.state.container
    container.auth.create_user("existing-admin", PASSWORD, DEFAULT_TENANT, "admin")

    resp = _register(invite_client, BOOTSTRAP_ADMIN)
    assert resp.status_code == 403, resp.text


def test_list_invites_only_returns_own_tenant(invite_client, invite_admin_auth):
    mine = invite_client.post("/admin/invites", json={"max_uses": 2}, headers=invite_admin_auth).json()["code"]
    theirs = _issue(invite_client, OTHER_TENANT, role="viewer", max_uses=3)

    resp = invite_client.get("/admin/invites", headers=invite_admin_auth)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert {row["code"] for row in rows} == {mine}
    assert {row["tenant_id"] for row in rows} == {DEFAULT_TENANT}
    assert theirs not in {row["code"] for row in rows}

    row = rows[0]
    assert row["role"] == "user"
    assert row["max_uses"] == 2
    assert row["used_count"] == 0


def test_list_invites_reflects_consumption(invite_client, invite_admin_auth):
    """列表里的 `used_count` 要能反映真实用量，否则管理员无法判断码还能不能用。"""
    code = invite_client.post("/admin/invites", json={"max_uses": 5}, headers=invite_admin_auth).json()["code"]
    assert _register(invite_client, "used-once", invite_code=code).status_code == 200

    rows = invite_client.get("/admin/invites", headers=invite_admin_auth).json()
    assert [(r["code"], r["used_count"]) for r in rows] == [(code, 1)]


def test_admin_deletes_own_invite(invite_client, fake_db, invite_admin_auth):
    code = invite_client.post("/admin/invites", json={}, headers=invite_admin_auth).json()["code"]

    resp = invite_client.delete(f"/admin/invites/{code}", headers=invite_admin_auth)
    assert resp.status_code == 200, resp.text
    assert code not in fake_db.invites
    assert invite_client.get("/admin/invites", headers=invite_admin_auth).json() == []

    # 删掉之后再删一次：已经不存在了，必须是 404 而不是再次 200
    assert invite_client.delete(f"/admin/invites/{code}", headers=invite_admin_auth).status_code == 404


def test_delete_unknown_invite_is_404(invite_client, invite_admin_auth):
    assert invite_client.delete("/admin/invites/no-such-code", headers=invite_admin_auth).status_code == 404


def test_cross_tenant_delete_is_404_and_does_not_touch_the_code(
    invite_client, fake_db, invite_admin_auth, invite_admin_b_auth
):
    """跨租户删除按"不存在"处理：默认租户 admin 删 tenant-b 的码应 404，
    而且**码必须还在**（404 不等于"删成功了但报错"），tenant-b 的 admin 才删得掉。"""
    code = invite_client.post("/admin/invites", json={}, headers=invite_admin_auth).json()["code"]
    assert fake_db.invites[code]["tenant_id"] == DEFAULT_TENANT

    # 把码挪到 tenant-b，模拟"另一个租户名下的码"
    fake_db.invites[code]["tenant_id"] = OTHER_TENANT

    denied = invite_client.delete(f"/admin/invites/{code}", headers=invite_admin_auth)
    assert denied.status_code == 404, denied.text
    assert code in fake_db.invites, "404 之后码必须完好无损"

    ok = invite_client.delete(f"/admin/invites/{code}", headers=invite_admin_b_auth)
    assert ok.status_code == 200, ok.text
    assert code not in fake_db.invites


def test_cross_tenant_code_cannot_register_into_the_other_tenant(invite_client):
    """把码的租户改成 tenant-b 之后，注册出来的账号落在 tenant-b；
    这也顺带证明"注册侧用的是码里的租户"，而不是请求体或调用者身份。"""
    code = _issue(invite_client, DEFAULT_TENANT, max_uses=1)
    container = invite_client.app.state.container
    container.db.invites[code]["tenant_id"] = OTHER_TENANT

    resp = _register(invite_client, "crosser", invite_code=code)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == OTHER_TENANT


# --------------------------------------------------------------------------- #
# 5. 权限
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ["user", "viewer"])
def test_invite_endpoints_reject_non_admin(invite_client, role):
    """非 admin 三个端点全拒。DELETE 也要验：普通用户能删码的话，
    等于能作废别人的准入凭据（拒绝服务），不能只挂读接口的权限。"""
    invite_client.app.state.container.auth.create_user(f"plain-{role}", PASSWORD, DEFAULT_TENANT, role)
    headers = _login(invite_client, f"plain-{role}")

    assert invite_client.get("/admin/invites", headers=headers).status_code == 403
    assert invite_client.post("/admin/invites", json={}, headers=headers).status_code == 403
    assert invite_client.delete("/admin/invites/any-code", headers=headers).status_code == 403


def test_invite_endpoints_reject_anonymous(invite_client):
    assert invite_client.get("/admin/invites").status_code == 401
    assert invite_client.post("/admin/invites", json={}).status_code == 401
    assert invite_client.delete("/admin/invites/any-code").status_code == 401
