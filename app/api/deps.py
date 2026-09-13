"""鉴权与授权依赖：Bearer JWT → `Principal`，并按角色强制。

V1 的 `get_current_user()` 只返回 `int`，路由层拿不到租户与角色，只能退化成全局共享。
V2 改为返回 `Principal(user_id, username, tenant_id, role)`：

- **每次请求回查数据库**取角色与租户（而不是信任 JWT 里的声明）。
  这样改角色立即生效、用户被禁用立即失效；代价是每请求一次主键查询。
- 角色不足一律 `AppError(403)`，由全局异常处理器转成 JSON。
"""
from __future__ import annotations

from fastapi import Depends, Header, Request

from app.services.tenancy import (
    ROLE_ADMIN,
    Principal,
    normalize_role,
    normalize_tenant,
    require_role as _require_role,
)
from app.utils.exceptions import AppError


def get_current_user(request: Request, authorization: str = Header(default=None)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("未登录", 401)
    token = authorization[7:].strip()
    container = request.app.state.container
    try:
        payload = container.auth.decode_token(token)
    except Exception:
        raise AppError("登录已失效，请重新登录", 401)
    sub = payload.get("sub")
    if sub is None or sub == "":
        raise AppError("无效令牌", 401)
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        raise AppError("无效令牌", 401)

    user = container.db.get_user_by_id(user_id)
    if user is None:
        raise AppError("用户不存在或已被删除", 401)
    return Principal(
        user_id=user_id,
        username=user.get("username") or "",
        tenant_id=normalize_tenant(user.get("tenant_id")),
        role=normalize_role(user.get("role")),
    )


def require_role(*roles: str):
    """依赖工厂：`Depends(require_role(ROLE_ADMIN))`，角色不足返回 403。

    允许的角色集为空时视为**仅 admin**（fail-safe：写漏了参数不会变成人人可过）。
    """
    allowed = tuple(roles) or (ROLE_ADMIN,)

    def _dependency(principal: Principal = Depends(get_current_user)) -> Principal:
        _require_role(principal, *allowed)
        return principal

    return _dependency


#: 常用快捷依赖
require_admin = require_role(ROLE_ADMIN)
