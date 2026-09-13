"""认证接口。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.models.schemas import AuthResponse, LoginRequest, RegisterRequest

router = APIRouter(tags=["认证"])


@router.post("/auth/register", response_model=AuthResponse, summary="注册", description="注册新用户并返回 JWT。")
def register(request: Request, body: RegisterRequest) -> dict:
    return request.app.state.container.auth.register(body.username, body.password)


@router.post("/auth/login", response_model=AuthResponse, summary="登录", description="登录并返回 JWT。")
def login(request: Request, body: LoginRequest) -> dict:
    return request.app.state.container.auth.login(body.username, body.password)