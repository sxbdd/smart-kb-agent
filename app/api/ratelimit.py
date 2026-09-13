"""限流：默认进程内滑动窗口，可切到 Redis 共享存储并**自动回退**。

三层结构：

- ``SlidingWindowLimiter``：进程内滑动窗口，单副本部署够用（V1 行为，保持不变）。
- ``RedisSlidingWindowLimiter``：基于 Redis ``INCR`` + ``EXPIRE`` 的固定窗口计数，
  多个副本共享同一份配额（key 形如 ``rl:{name}:{key}:{窗口编号}``）。
- ``build_limiter()``：按配置挑选后端。Redis 建客户端失败 / ``PING`` 不通 / 依赖缺失
  时**只记 warning 并回退内存实现**，绝不因为 Redis 挂掉让服务起不来。

两种实现的 ``check()`` 语义保持一致：窗口内计数达到上限后返回 ``False``，
且**超限的这一次请求不占用配额**（内存实现不追加时间戳，Redis 实现回滚 ``DECR``）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol

from fastapi import Request

from app.utils.exceptions import AppError

try:  # pragma: no cover - 只有未安装 redis 依赖时才会走到 except
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]

logger = logging.getLogger("ratelimit")

_WINDOW_SECONDS = 60

# Redis key 前缀与默认地址（可用 REDIS_URL 覆盖）
_KEY_PREFIX = "rl"
_DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

# 建连 / 读写超时：避免 Redis 半死不活时把请求线程拖死
_SOCKET_TIMEOUT_S = 2.0


class RateLimiter(Protocol):
    """限流器协议：``SlidingWindowLimiter`` / ``RedisSlidingWindowLimiter`` 都满足。

    ``limit`` 与 ``window`` 是公开属性——``app/api/routes_auth.py`` 会用它们
    拼 429 的提示文案。
    """

    limit: int
    window: int

    def check(self, key: str) -> bool:  # pragma: no cover - 仅用于类型标注
        ...

    def reset(self) -> None:  # pragma: no cover - 仅用于类型标注
        ...


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


class RedisSlidingWindowLimiter:
    """把计数放到 Redis 的固定窗口限流器（多副本共享配额）。

    固定窗口用 ``INCR`` 累加、``EXPIRE`` 兜底过期；窗口编号取 ``int(now / window)``，
    所以 key 会随窗口自然滚动，不需要遍历清理。窗口切换的瞬间可能出现
    「两个窗口各放 limit 次」的边界效应，这是固定窗口相对滑动窗口的已知代价，
    换来的是 O(1) 存储与原子自增。

    运行期 Redis 出错（连接断开、超时等）时**不再让请求失败**：记一次 warning，
    然后退到进程内实现继续限流，保证可用性优先。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int = _WINDOW_SECONDS,
        redis_url: str = _DEFAULT_REDIS_URL,
        name: str = "default",
        client: Optional["redis.Redis"] = None,  # type: ignore[valid-type]
        key_prefix: str = _KEY_PREFIX,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.name = name
        self.key_prefix = key_prefix
        self._redis_url = redis_url
        self._client = client
        self._fallback = SlidingWindowLimiter(limit, window_seconds)
        self._warned = False
        self._lock = threading.Lock()

    @property
    def client(self):
        """惰性建客户端：构造器不碰网络，便于测试注入假客户端。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = _make_redis_client(self._redis_url)
        return self._client

    def bucket_key(self, key: str, now: Optional[float] = None) -> str:
        """当前窗口的 key：``rl:{name}:{key}:{窗口编号}``。"""
        ts = time.time() if now is None else now
        slot = int(ts // self.window) if self.window > 0 else 0
        return f"{self.key_prefix}:{self.name}:{key}:{slot}"

    def check(self, key: str) -> bool:
        """记一次命中；超限返回 False（回滚本次计数，与内存实现语义一致）。"""
        try:
            client = self.client
            bucket = self.bucket_key(key)
            pipe = client.pipeline()
            pipe.incr(bucket)
            # TTL 给 2 个窗口：覆盖窗口末尾的写入，过期后 key 自动消失，不会泄漏
            pipe.expire(bucket, max(self.window * 2, 1))
            count = int(pipe.execute()[0])
            if count > self.limit:
                client.decr(bucket)
                return False
            return True
        except Exception as exc:
            self._warn_once(exc)
            return self._fallback.check(key)

    def reset(self) -> None:
        """清空本 limiter 名下所有窗口的计数（运维 / 测试用）。"""
        self._fallback.reset()
        try:
            client = self.client
            keys = list(client.scan_iter(match=f"{self.key_prefix}:{self.name}:*", count=100))
            if keys:
                client.delete(*keys)
        except Exception as exc:
            logger.warning("重置 Redis 限流计数失败（name=%s）：%s", self.name, exc)

    def _warn_once(self, exc: Exception) -> None:
        """同一实例只告警一次，避免 Redis 长时间不可用时刷爆日志。"""
        if self._warned:
            logger.debug("Redis 限流仍不可用（name=%s）：%s", self.name, exc)
            return
        self._warned = True
        logger.warning("Redis 限流执行失败，本进程临时回退到内存实现（name=%s）：%s", self.name, exc)


def _make_redis_client(redis_url: str):
    """按 URL 建 Redis 客户端；依赖缺失 / URL 非法时直接抛异常，由调用方回退。"""
    if redis is None:
        raise RuntimeError("未安装 redis 依赖，无法启用 Redis 限流后端")
    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=_SOCKET_TIMEOUT_S,
        socket_timeout=_SOCKET_TIMEOUT_S,
        health_check_interval=30,
    )


def build_limiter(
    limit: int,
    backend: str = "memory",
    redis_url: str = "",
    name: str = "default",
) -> RateLimiter:
    """按 backend 构造限流器：``redis`` 优先，任何失败都回退到进程内实现。

    :param limit: 窗口内允许的次数。
    :param backend: ``memory``（默认）或 ``redis``；未知取值回退 memory 并 warning。
    :param redis_url: Redis 连接串，空则用默认本机地址。
    :param name: 限流器名字，用于隔离 Redis key（例如 ``auth``）。
    """
    normalized = (backend or "memory").strip().lower()

    if normalized == "redis":
        url = redis_url or _DEFAULT_REDIS_URL
        try:
            client = _make_redis_client(url)
            # 真正建连并 PING：只有确认可用才切到 Redis，否则请求期才发现就晚了
            client.ping()
        except Exception as exc:
            logger.warning("限流后端 redis 不可用（%s），已回退到进程内实现：%s", url, exc)
            return SlidingWindowLimiter(limit)
        logger.info("限流后端：redis（%s，name=%s）", url, name)
        return RedisSlidingWindowLimiter(limit, redis_url=url, name=name, client=client)

    if normalized != "memory":
        logger.warning("未知的限流后端 %r，已回退到进程内实现", backend)
    return SlidingWindowLimiter(limit)


def client_ip(request: Request) -> str:
    """取客户端 IP，优先信任反向代理写入的 X-Forwarded-For 首个地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(limiter: RateLimiter) -> Callable[[Request], None]:
    """构造一个 FastAPI 依赖：超限直接 429（走 AppError 统一错误格式）。

    参数是任意具备 ``check()`` / ``limit`` / ``window`` 的限流器
    （内存实现或 Redis 实现皆可）。
    """

    def dependency(request: Request) -> None:
        if not limiter.check(client_ip(request)):
            raise AppError(f"请求过于频繁，请稍后再试（每 {limiter.window} 秒最多 {limiter.limit} 次）", 429)

    return dependency
