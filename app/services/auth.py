"""认证服务：注册 / 登录 / 代建账号（JWT + PBKDF2 密码哈希）。

V2 变化（多租户 + 角色）：

- 用户名唯一性作用域从「全局」变成「**租户内**」：不同租户可以重名
  —— 落库唯一键是 ``uk_tenant_username(tenant_id, username)``，所以这里的查重也必须带 ``tenant_id``。
- 注册时确定角色：命中 ``BOOTSTRAP_ADMIN_USERNAME`` 的账号成为该租户 admin，
  其余走 `DEFAULT_ROLE`（= ``user``，保持"注册后即可上传与提问"的既有可用性）。
- JWT 里**附带** ``tenant_id`` / ``role`` 声明，仅作可观测性；
  真正的授权判断由 ``app/api/deps.py`` 每请求回查数据库，避免"改了角色但旧令牌仍生效"。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.services.tenancy import (
    DEFAULT_ROLE,
    ROLE_ADMIN,
    normalize_role,
    normalize_tenant,
)
from app.utils.exceptions import AppError

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


class AuthService:
    def __init__(
        self,
        db,
        jwt_secret: str,
        jwt_expire_minutes: int,
        default_tenant: str = "default",
        allow_self_register: bool = True,
        bootstrap_admin_username: str = "",
        require_invite: bool = False,
        invite_ttl_hours: int = 0,
        invite_default_max_uses: int = 1,
    ) -> None:
        self.db = db
        self.jwt_secret = jwt_secret
        self.jwt_expire_minutes = jwt_expire_minutes
        self.default_tenant = normalize_tenant(default_tenant)
        self.allow_self_register = allow_self_register
        self.bootstrap_admin_username = (bootstrap_admin_username or "").strip()
        self.require_invite = require_invite
        self.invite_ttl_hours = max(0, int(invite_ttl_hours))
        self.invite_default_max_uses = max(0, int(invite_default_max_uses))

    # ---------------- 注册 / 登录 ----------------

    def register(
        self,
        username: str,
        password: str,
        tenant: str | None = None,
        invite_code: str | None = None,
    ) -> dict:
        """自助注册。

        - `ALLOW_SELF_REGISTER=false` → 403（改由 admin 代建）；
        - `REQUIRE_INVITE=true` → 必须持有效邀请码，且**租户与角色由邀请码决定**，
          请求里的 `tenant` 一律忽略 —— 否则注册者能自选租户，隔离强制点就失去了前提；
        - **例外**：`BOOTSTRAP_ADMIN_USERNAME` 指定的账号不需要邀请码，但**只在默认租户里、
          且该租户还没有管理员时**才生效。没有这个例外就有鸡生蛋问题（开启邀请码后新库
          一个人都进不来）；但例外必须收窄 —— 否则任何知道这个用户名的人都能用
          `{"username":"root","tenant":"某个已存在的租户"}` 免码把自己变成那个租户的 admin，
          直接绕过邀请码闸门读别人的数据（这是真机踩到的越权洞）。
        """
        if not self.allow_self_register:
            raise AppError("自助注册已关闭，请联系管理员开通账号", 403)

        is_bootstrap_admin = self._is_bootstrap_registration(username, tenant)
        if self.require_invite and not is_bootstrap_admin:
            invite = self._peek_invite(invite_code)
            tenant_id = normalize_tenant(invite.get("tenant_id"), self.default_tenant)
            role = normalize_role(invite.get("role"))
        else:
            tenant_id = normalize_tenant(tenant, self.default_tenant)
            role = self._initial_role(username)

        # 先查重名（拿到租户才知道该查哪个租户），再去原子扣减邀请码 ——
        # 这样"用户名已存在"不会白白消耗掉一次邀请码额度。
        if self.db.get_user_by_username(username, tenant_id) is not None:
            raise AppError("用户名已存在", 409)

        if self.require_invite and not is_bootstrap_admin:
            # 真正的闸门：带条件的 UPDATE，过期/用尽/不存在都会让 rowcount != 1。
            # 上面那次 peek 只用来拿租户与角色、把报错说清楚，**不作为放行依据**。
            consumed = self.db.consume_invite(invite_code.strip(), tenant_id)
            if consumed is None:
                raise AppError("邀请码无效、已用尽或已过期", 403)

        user_id = self.db.create_user(username, hash_password(password), tenant_id, role)
        return self._auth_payload(user_id, username, tenant_id, role)

    def _is_bootstrap_registration(self, username: str, tenant: str | None) -> bool:
        """bootstrap 例外的**收窄**判定：`默认租户` + `该租户还没有管理员`。

        为什么必须收窄（两个都是真机验证出来的洞）：

        1. **不限租户** → 任何知道 `BOOTSTRAP_ADMIN_USERNAME` 的人都能发
           `{"username":"root","tenant":"别的租户"}`，免码成为那个租户的 admin，
           等于绕过邀请码闸门去读别人租户的数据；
        2. **不检查"是否已有管理员"** → 一个**已存在**的租户只要当前没有 admin
           （存量库升级后就是这样），就能被后来者抢注。
        """
        if not self.bootstrap_admin_username or username != self.bootstrap_admin_username:
            return False
        if normalize_tenant(tenant, self.default_tenant) != self.default_tenant:
            return False
        # 用 list_users 而不是新加 DAO 方法：租户内用户数是个位数量级，
        # 而多一个 DAO 方法就多一份必须在 FakeDatabase 里同步维护的契约。
        return not any(
            normalize_role(u.get("role")) == ROLE_ADMIN
            for u in self.db.list_users(self.default_tenant)
        )

    def _peek_invite(self, invite_code: str | None) -> dict:
        """只读地取出邀请码对应的租户与角色（不扣减额度）。"""
        code = (invite_code or "").strip()
        if not code:
            raise AppError("注册需要邀请码，请向管理员索取", 403)
        invite = self.db.get_invite_by_code(code)
        if invite is None:
            raise AppError("邀请码无效", 403)
        return invite

    def login(self, username: str, password: str, tenant: str | None = None) -> dict:
        tenant_id = normalize_tenant(tenant, self.default_tenant)
        user = self.db.get_user_by_username(username, tenant_id)
        if user is None or not verify_password(password, user["password_hash"]):
            # 不区分"用户不存在"与"密码错误"，避免账号枚举
            raise AppError("用户名或密码错误", 401)
        role = normalize_role(user.get("role"))
        return self._auth_payload(user["id"], username, tenant_id, role)

    # ---------------- 邀请码（admin 签发）----------------

    def create_invite(
        self,
        tenant_id: str,
        role: str = DEFAULT_ROLE,
        max_uses: int | None = None,
        expires_in_hours: int | None = None,
        created_by: int | None = None,
    ) -> dict:
        """签发一个邀请码并返回它的全部信息（`code` 只在此时可见，之后列表里也仍是它）。"""
        tenant_id = normalize_tenant(tenant_id, self.default_tenant)
        uses = self.invite_default_max_uses if max_uses is None else max(0, int(max_uses))
        ttl = self.invite_ttl_hours if expires_in_hours is None else max(0, int(expires_in_hours))
        code = secrets.token_hex(8)  # 16 位十六进制：够用（64bit）且便于人工输入
        self.db.create_invite(
            code=code,
            tenant_id=tenant_id,
            role=normalize_role(role),
            created_by=created_by,
            max_uses=uses,
            expires_in_hours=ttl,
        )
        row = self.db.get_invite(code, tenant_id) or {}
        return self._invite_payload(row, code=code, tenant_id=tenant_id, role=normalize_role(role),
                                    max_uses=uses)

    @staticmethod
    def _invite_payload(row: dict, code: str, tenant_id: str, role: str, max_uses: int) -> dict:
        expires = row.get("expires_at")
        return {
            "code": code,
            "tenant_id": tenant_id,
            "role": role,
            "max_uses": int(row.get("max_uses", max_uses) or 0),
            "used_count": int(row.get("used_count", 0) or 0),
            "expires_at": None if expires is None else str(expires),
            "created_at": None if row.get("created_at") is None else str(row.get("created_at")),
        }

    def create_user(
        self,
        username: str,
        password: str,
        tenant_id: str,
        role: str = DEFAULT_ROLE,
    ) -> dict:
        """admin 代建账号（自助注册关闭时的开通途径）。"""
        tenant_id = normalize_tenant(tenant_id, self.default_tenant)
        if self.db.get_user_by_username(username, tenant_id) is not None:
            raise AppError("用户名已存在", 409)
        safe_role = normalize_role(role)
        user_id = self.db.create_user(username, hash_password(password), tenant_id, safe_role)
        return self._auth_payload(user_id, username, tenant_id, safe_role)

    def _initial_role(self, username: str) -> str:
        """命中 BOOTSTRAP_ADMIN_USERNAME 的账号首次注册即为 admin，其余为默认角色。"""
        if self.bootstrap_admin_username and username == self.bootstrap_admin_username:
            return ROLE_ADMIN
        return DEFAULT_ROLE

    # ---------------- 令牌 ----------------

    def _auth_payload(self, user_id: int, username: str, tenant_id: str, role: str) -> dict:
        return {
            "token": self._issue_token(user_id, username, tenant_id, role),
            "username": username,
            "tenant_id": tenant_id,
            "role": role,
        }

    def _issue_token(self, user_id: int, username: str, tenant_id: str = "default", role: str = DEFAULT_ROLE) -> str:
        payload = {
            "sub": str(user_id),
            "username": username,
            "tenant_id": tenant_id,
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=self.jwt_expire_minutes),
        }
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")

    def decode_token(self, token: str) -> dict:
        return jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
