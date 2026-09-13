"""鉴权依赖：解析 Bearer JWT，返回当前用户 ID。"""
from __future__ import annotations

from fastapi import Header, Request

from app.utils.exceptions import AppError


def get_current_user(request: Request, authorization: str = Header(default=None)) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("未登录", 401)
    token = authorization[7:].strip()
    auth = request.app.state.container.auth
    try:
        payload = auth.decode_token(token)
    except Exception:
        raise AppError("登录已失效，请重新登录", 401)
    user_id = payload.get("sub")
    if not user_id:
        raise AppError("无效令牌", 401)
    return int(user_id)