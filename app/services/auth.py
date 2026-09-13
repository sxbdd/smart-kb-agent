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
    ) -> None:
        self.db = db
        self.jwt_secret = jwt_secret
        self.jwt_expire_minutes = jwt_expire_minutes
        self.default_tenant = normalize_tenant(default_tenant)
        self.allow_self_register = allow_self_register
        self.bootstrap_admin_username = (bootstrap_admin_username or "").strip()

    # ---------------- 注册 / 登录 ----------------

    def register(self, username: str, password: str, tenant: str | None = None) -> dict:
        """自助注册。``ALLOW_SELF_REGISTER=false`` 时直接 403（改由 admin 代建）。"""
        if not self.allow_self_register:
            raise AppError("自助注册已关闭，请联系管理员开通账号", 403)
        tenant_id = normalize_tenant(tenant, self.default_tenant)
        if self.db.get_user_by_username(username, tenant_id) is not None:
            raise AppError("用户名已存在", 409)
        role = self._initial_role(username)
        user_id = self.db.create_user(username, hash_password(password), tenant_id, role)
        return self._auth_payload(user_id, username, tenant_id, role)

    def login(self, username: str, password: str, tenant: str | None = None) -> dict:
        tenant_id = normalize_tenant(tenant, self.default_tenant)
        user = self.db.get_user_by_username(username, tenant_id)
        if user is None or not verify_password(password, user["password_hash"]):
            # 不区分"用户不存在"与"密码错误"，避免账号枚举
            raise AppError("用户名或密码错误", 401)
        role = normalize_role(user.get("role"))
        return self._auth_payload(user["id"], username, tenant_id, role)

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
