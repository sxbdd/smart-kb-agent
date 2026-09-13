"""极简进程内限流：用于登录 / 注册等敏感接口。

V1 单实例部署够用。注意这是**进程内**实现，多副本部署时各副本独立计数，
需要换成 Redis 等共享存储（已在 docs/deployment.md 登记为已知限制）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from fastapi import Request

from app.utils.exceptions import AppError

_WINDOW_SECONDS = 60


class SlidingWindowLimiter:
    """按 key（通常是客户端 IP）做滑动窗口计数。"""

    def __init__(self, limit: int, window_seconds: int = _WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """记一次命中；超限返回 False（不记录本次）。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_ip(request: Request) -> str:
    """取客户端 IP，优先信任反向代理写入的 X-Forwarded-For 首个地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(limiter: SlidingWindowLimiter) -> Callable[[Request], None]:
    """构造一个 FastAPI 依赖：超限直接 429（走 AppError 统一错误格式）。"""

    def dependency(request: Request) -> None:
        if not limiter.check(client_ip(request)):
            raise AppError(f"请求过于频繁，请稍后再试（每 {limiter.window} 秒最多 {limiter.limit} 次）", 429)

    return dependency
