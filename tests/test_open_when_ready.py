"""启动等待器回归：必须「等服务真的就绪再开浏览器」。

守护的 bug：start.bat 曾在 uvicorn 起来之前就打开浏览器，
应用还没监听端口 → 用户看到 ERR_CONNECTION_REFUSED（见 docs/deployment.md 排障）。
"""
from __future__ import annotations

import http.server
import threading
import time

from scripts.open_when_ready import wait_until_ready


class _Handler(http.server.BaseHTTPRequestHandler):
    ready = False

    def do_GET(self):  # noqa: N802  (http.server 接口)
        if self.path == "/healthz" and type(self).ready:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # 静音
        return


def _serve():
    """起一个临时 HTTP 服务，返回 (server, url)。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_returns_true_when_healthy():
    _Handler.ready = True
    server, url = _serve()
    try:
        assert wait_until_ready(url, timeout=5, interval=0.05) is True
    finally:
        server.shutdown()
        server.server_close()


def test_returns_false_on_timeout_when_not_ready():
    _Handler.ready = False
    server, url = _serve()
    try:
        started = time.monotonic()
        assert wait_until_ready(url, timeout=1, interval=0.05) is False
        assert time.monotonic() - started >= 1.0, "应真的等到超时而不是立刻返回"
    finally:
        server.shutdown()
        server.server_close()


def test_returns_false_when_nothing_listens():
    """端口没人监听（典型抢跑场景）→ 必须返回 False，不能抛异常。"""
    server, url = _serve()
    port = server.server_address[1]
    server.shutdown()
    server.server_close()          # 端口关闭，无人监听
    assert wait_until_ready(url, timeout=0.6, interval=0.05) is False


def test_waits_for_late_readiness():
    """关键场景：一开始未就绪，稍后就绪 → 必须等到 True（证明是轮询而非一次性判断）。"""
    _Handler.ready = False
    server, url = _serve()

    def become_ready():
        time.sleep(0.6)
        _Handler.ready = True

    threading.Thread(target=become_ready, daemon=True).start()
    try:
        assert wait_until_ready(url, timeout=5, interval=0.05) is True
    finally:
        server.shutdown()
        server.server_close()
