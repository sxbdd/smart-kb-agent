"""认证接口。

注册 / 登录都挂了按客户端 IP 的滑动窗口限流：PBKDF2 迭代 12 万次，
不限流不仅可暴力枚举密码，还能被少量请求放大成 CPU 消耗
（历史问题见 docs/review-v1-audit.md §2.10）。

V2（多租户 + RBAC）变化：

- 请求体新增可选 ``tenant``，透传给 `AuthService` 做**归一化**后落到用户记录上；
  归一只在服务层做一次，路由层不重复实现，避免两处规则漂移。
- 响应体（`AuthResponse`）直接来自 `AuthService` 的 dict，含 `role` / `tenant_id`
  —— 这两个字段决定了后续所有接口的可见范围，前端拿到即可展示当前身份。
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
    return request.app.state.container.auth.register(body.username, body.password, body.tenant)


@router.post(
    "/auth/login",
    response_model=AuthResponse,
    summary="登录",
    description="登录并返回 JWT（含角色与租户）。",
    dependencies=[Depends(_rate_limit)],
)
def login(request: Request, body: LoginRequest) -> dict:
    return request.app.state.container.auth.login(body.username, body.password, body.tenant)
