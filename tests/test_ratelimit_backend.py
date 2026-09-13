"""限流后端：内存实现 / Redis 共享实现 / 自动回退 / FastAPI 依赖行为。

离线优先：内存与回退路径完全不碰外部服务；只有真正打 Redis 的用例会连本机
Redis，连不上时自动 skip（不标记 integration，这样本机跑得到、CI 里自动跳过）。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import ratelimit as rl
from app.api.ratelimit import (
    RedisSlidingWindowLimiter,
    SlidingWindowLimiter,
    build_limiter,
    client_ip,
    rate_limit_dependency,
)
from app.utils.exceptions import AppError

REDIS_URL = "redis://127.0.0.1:6379/0"


# ---------------- 测试替身 ----------------


class _Clock:
    """可手动推进的时钟，用来替换 ratelimit 模块内的 time（不污染全局 time 模块）。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _request(ip: str = "1.2.3.4", forwarded: str | None = None) -> Request:
    """造一个最小可用的 Starlette Request（不启服务、不走网络）。"""
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": headers,
            "client": (ip, 12345),
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(rl, "time", c)
    return c


def _redis_client_or_skip():
    """连本机 Redis；不可用就 skip（CI 无 Redis 时用例自动跳过）。"""
    try:
        import redis

        client = redis.Redis.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0
        )
        client.ping()
    except Exception as exc:  # pragma: no cover - 取决于本机环境
        pytest.skip(f"Redis 不可用，跳过：{exc}")
    return client


# ---------------- 内存实现（行为保持不变） ----------------


def test_memory_limiter_allows_up_to_limit() -> None:
    limiter = SlidingWindowLimiter(3)
    assert limiter.limit == 3
    assert limiter.window == 60
    assert [limiter.check("1.1.1.1") for _ in range(3)] == [True, True, True]
    # 超限：既不通过，也不占用配额（继续调用仍是 False）
    assert limiter.check("1.1.1.1") is False
    assert limiter.check("1.1.1.1") is False
    # key 之间互相隔离
    assert limiter.check("2.2.2.2") is True


def test_memory_limiter_slides_window(clock: _Clock) -> None:
    limiter = SlidingWindowLimiter(2, window_seconds=60)
    assert limiter.check("ip") is True
    clock.advance(30)
    assert limiter.check("ip") is True
    assert limiter.check("ip") is False

    clock.advance(31)  # 最早一次命中（t=0）已经滑出 60 秒窗口
    assert limiter.check("ip") is True
    assert limiter.check("ip") is False


def test_memory_limiter_reset_clears_counts() -> None:
    limiter = SlidingWindowLimiter(1)
    assert limiter.check("ip") is True
    assert limiter.check("ip") is False
    limiter.reset()
    assert limiter.check("ip") is True


# ---------------- build_limiter：选择与回退 ----------------


def test_build_limiter_defaults_to_memory() -> None:
    assert isinstance(build_limiter(5), SlidingWindowLimiter)
    assert isinstance(build_limiter(5, "memory", REDIS_URL), SlidingWindowLimiter)
    assert isinstance(build_limiter(5, "  MEMORY  ", REDIS_URL), SlidingWindowLimiter)
    assert build_limiter(5).limit == 5


def test_build_limiter_unknown_backend_falls_back_with_warning(caplog) -> None:
    with caplog.at_level("WARNING", logger="ratelimit"):
        limiter = build_limiter(7, "memcached", REDIS_URL)
    assert isinstance(limiter, SlidingWindowLimiter)
    assert limiter.limit == 7
    assert any("未知的限流后端" in r.getMessage() for r in caplog.records)


def test_build_limiter_falls_back_when_client_creation_raises(monkeypatch, caplog) -> None:
    """建 Redis 客户端直接抛异常 → 返回内存实现且**不向上抛错**。"""

    def _boom(url: str):
        raise RuntimeError("redis 连接被拒绝")

    monkeypatch.setattr(rl, "_make_redis_client", _boom)
    with caplog.at_level("WARNING", logger="ratelimit"):
        limiter = build_limiter(3, "redis", REDIS_URL, name="auth")
    assert isinstance(limiter, SlidingWindowLimiter)
    assert limiter.limit == 3
    assert limiter.window == 60
    assert any("已回退到进程内实现" in r.getMessage() for r in caplog.records)


def test_build_limiter_falls_back_when_from_url_raises(monkeypatch) -> None:
    """走真实的 _make_redis_client 路径，但让 redis.Redis.from_url 抛错。"""
    if rl.redis is None:  # pragma: no cover - 依赖总是装着的
        pytest.skip("未安装 redis 依赖")

    def _boom(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(rl.redis.Redis, "from_url", _boom)
    limiter = build_limiter(4, "redis", REDIS_URL)
    assert isinstance(limiter, SlidingWindowLimiter)


def test_build_limiter_falls_back_when_ping_fails(monkeypatch, caplog) -> None:
    """客户端建得出来但 PING 不通（Redis 半死不活）同样要回退。"""

    class _DeadClient:
        def ping(self) -> bool:
            raise ConnectionError("redis 无响应")

    monkeypatch.setattr(rl, "_make_redis_client", lambda url: _DeadClient())
    with caplog.at_level("WARNING", logger="ratelimit"):
        limiter = build_limiter(2, "redis", REDIS_URL)
    assert isinstance(limiter, SlidingWindowLimiter)
    assert any("不可用" in r.getMessage() for r in caplog.records)


# ---------------- Redis 实现 ----------------


def test_redis_limiter_bucket_key_format(clock: _Clock) -> None:
    """key 形如 rl:{name}:{key}:{窗口编号}；构造器本身不碰网络。"""
    limiter = RedisSlidingWindowLimiter(5, window_seconds=60, name="auth", client=object())
    slot = int(clock.now // 60)
    assert limiter.bucket_key("1.2.3.4") == f"rl:auth:1.2.3.4:{slot}"
    assert limiter.limit == 5
    assert limiter.window == 60

    clock.advance(60)
    assert limiter.bucket_key("1.2.3.4") == f"rl:auth:1.2.3.4:{slot + 1}"


def test_redis_limiter_counts_on_real_redis() -> None:
    """真机 Redis：窗口内计数、超限、key 前缀与 TTL、reset() 清理。"""
    client = _redis_client_or_skip()
    name = f"test-{uuid.uuid4().hex[:8]}"
    limiter = build_limiter(2, "redis", REDIS_URL, name=name)
    try:
        assert isinstance(limiter, RedisSlidingWindowLimiter)
        assert (limiter.limit, limiter.window) == (2, 60)

        assert limiter.check("10.0.0.1") is True
        assert limiter.check("10.0.0.1") is True
        assert limiter.check("10.0.0.1") is False
        # 另一个 key 独立计数
        assert limiter.check("10.0.0.2") is True

        keys = list(client.scan_iter(match=f"rl:{name}:*"))
        assert keys, "限流命中后 Redis 里应存在计数 key"
        assert all(k.startswith(f"rl:{name}:") for k in keys)
        # 窗口编号 + TTL：key 会过期，不会泄漏
        assert all(int(k.rsplit(":", 1)[1]) > 0 for k in keys)
        assert all(client.ttl(k) > 0 for k in keys)
        # 超限的那次回滚了计数，配额不会越滚越高
        assert all(int(client.get(k)) <= 2 for k in keys)
    finally:
        limiter.reset()
    assert list(client.scan_iter(match=f"rl:{name}:*")) == []


def test_redis_limiter_survives_redis_failure_at_runtime(caplog) -> None:
    """请求期 Redis 挂掉：不抛错、退回内存限流、只告警一次。"""

    class _BrokenClient:
        def pipeline(self):
            raise ConnectionError("connection reset by peer")

        def scan_iter(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    limiter = RedisSlidingWindowLimiter(2, name="broken", client=_BrokenClient())
    with caplog.at_level("WARNING", logger="ratelimit"):
        assert limiter.check("ip") is True
        assert limiter.check("ip") is True
        assert limiter.check("ip") is False  # 回退后仍然限流
        limiter.reset()  # 清理失败也不能抛错
    warnings = [r for r in caplog.records if "回退" in r.getMessage()]
    assert len(warnings) == 1, "同一实例只应告警一次，避免刷日志"


# ---------------- FastAPI 依赖 ----------------


def test_client_ip_prefers_x_forwarded_for() -> None:
    assert client_ip(_request(ip="9.9.9.9", forwarded="1.1.1.1, 2.2.2.2")) == "1.1.1.1"
    assert client_ip(_request(ip="9.9.9.9")) == "9.9.9.9"


def test_rate_limit_dependency_raises_app_error_429() -> None:
    dependency = rate_limit_dependency(SlidingWindowLimiter(2))
    dependency(_request())
    dependency(_request())
    with pytest.raises(AppError) as excinfo:
        dependency(_request())
    assert excinfo.value.status_code == 429
    assert "请求过于频繁" in excinfo.value.detail
    assert "每 60 秒最多 2 次" in excinfo.value.detail


def test_rate_limit_dependency_over_http_returns_429() -> None:
    app = FastAPI()
    limiter = SlidingWindowLimiter(2)

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/ping", dependencies=[Depends(rate_limit_dependency(limiter))])
    def ping() -> dict:
        return {"ok": True}

    http = TestClient(app)
    assert http.get("/ping").status_code == 200
    assert http.get("/ping").status_code == 200
    blocked = http.get("/ping")
    assert blocked.status_code == 429
    assert "请求过于频繁" in blocked.json()["detail"]


def test_redis_limiter_end_to_end_over_http() -> None:
    """真机 Redis + FastAPI：第 3 次请求起 429（也是 Lead 接线后的实际形态）。"""
    _redis_client_or_skip()
    name = f"e2e-{uuid.uuid4().hex[:8]}"
    limiter = build_limiter(2, "redis", REDIS_URL, name=name)
    assert isinstance(limiter, RedisSlidingWindowLimiter)

    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.post("/login", dependencies=[Depends(rate_limit_dependency(limiter))])
    def login() -> dict:
        return {"ok": True}

    try:
        http = TestClient(app)
        assert [http.post("/login").status_code for _ in range(2)] == [200, 200]
        blocked = http.post("/login")
        assert blocked.status_code == 429
        assert "请求过于频繁" in blocked.json()["detail"]
    finally:
        limiter.reset()


def test_rate_limit_dependency_accepts_redis_limiter_interface() -> None:
    """依赖只要求 .check()/.limit/.window，Redis 实现同样可用（无需真连）。"""

    class _StubRedisLimiter:
        limit = 1
        window = 60

        def __init__(self) -> None:
            self.calls = 0

        def check(self, key: str) -> bool:
            self.calls += 1
            return self.calls <= 1

    limiter = _StubRedisLimiter()
    dependency = rate_limit_dependency(limiter)  # type: ignore[arg-type]
    dependency(_request())
    with pytest.raises(AppError) as excinfo:
        dependency(_request())
    assert excinfo.value.status_code == 429
