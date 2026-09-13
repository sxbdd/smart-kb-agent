"""认证接口。

注册 / 登录都挂了按客户端 IP 的滑动窗口限流：PBKDF2 迭代 12 万次，
不限流不仅可暴力枚举密码，还能被少量请求放大成 CPU 消耗
（历史问题见 docs/review-v1-audit.md §2.10）。

V2（多租户 + RBAC）变化：

- 请求体新增可选 ``tenant``，透传给 `AuthService` 做**归一化**后落到用户记录上；
  归一只在服务层做一次，路由层不重复实现，避免两处规则漂移。
- 响应体（`AuthResponse`）直接来自 `AuthService` 的 dict，含 `role` / `tenant_id`
  —— 这两个字段决定了后续所有接口的可见范围，前端拿到即可展示当前身份。
- 请求体再新增可选 ``invite_code``，**原样透传**给 `AuthService`：
  校验（有效性 / 是否用完 / 是否过期）、以及"租户与角色由邀请码决定"的规则
  全部在服务层与 DAO 的原子 UPDATE 里，路由层不做任何判断，避免闸门出现第二个实现。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.ratelimit import client_ip
from app.models.schemas import AuthResponse, LoginRequest, RegisterRequest
from app.utils.exceptions import AppError

router = APIRouter(tags=["认证"])


def _rate_limit(request: Request) -> None:
    limiter = request.app.state.container.auth_limiter
    if not limiter.check(client_ip(request)):
        raise AppError(f"请求过于频繁，请稍后再试（每 {limiter.window} 秒最多 {limiter.limit} 次）", 429)


@router.post(
    "/auth/register",
    response_model=AuthResponse,
    summary="注册",
    description="注册新用户并返回 JWT（含角色与租户）。",
    dependencies=[Depends(_rate_limit)],
)
def register(request: Request, body: RegisterRequest) -> dict:
    # 两个可选参数用**关键字**传：`register(username, password, tenant=None, invite_code=None)`
    # 的第 3/4 个位置参数分别是 tenant 与 invite_code，一旦服务层调整签名，
    # 位置传参会把两者静默串位（表现为"带码注册进错租户"这种最难查的漏洞）。
    return request.app.state.container.auth.register(
        body.username,
        body.password,
        tenant=body.tenant,
        invite_code=body.invite_code,
    )


@router.post(
    "/auth/login",
    response_model=AuthResponse,
    summary="登录",
    description="登录并返回 JWT（含角色与租户）。",
    dependencies=[Depends(_rate_limit)],
)
def login(request: Request, body: LoginRequest) -> dict:
    return request.app.state.container.auth.login(body.username, body.password, body.tenant)
