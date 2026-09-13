"""前端 E2E：用真实浏览器（Playwright + 系统 Edge）跑一遍核心用户流程。

为什么需要它：现有前端测试只做静态断言（"页面里含某个字符串"），
无法发现"点注册没反应""上传后列表不刷新""提问不渲染来源"这类真实交互问题。

设计取舍：
- **不需要下载浏览器**：用 `channel="msedge"` 复用系统自带 Edge（Windows 自带，实测可启动）。
- 应用用**离线组件**（hash / memory / fake）+ 假数据库起在**同进程的线程里**，
  所以整条 E2E 不需要 MySQL、模型、外网，也不产生任何费用。
- 默认跳过（`RUN_E2E=1` 才跑），避免拖慢常规回归与 CI；缺 Playwright / Edge 时自动 skip。

运行：``$env:RUN_E2E=1; .venv\\Scripts\\python -m pytest tests\\test_frontend_e2e.py -v -s``
"""
from __future__ import annotations

import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("RUN_E2E") != "1", reason="需要 RUN_E2E=1"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_health(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"live server 未在 {timeout}s 内就绪：{url}")


@pytest.fixture
def live_server(settings, fake_db):
    """在同进程线程里起一个真实 uvicorn（离线组件 + 假数据库）。

    因为 uvicorn 与测试同一进程，conftest 里对 `app.container.Database` 的 monkeypatch
    依然生效 —— 浏览器拿到的是一个完全离线、不写真实 MySQL 的应用。
    """
    uvicorn = pytest.importorskip("uvicorn")
    from app.factory import create_app

    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    _wait_health(base)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def browser():
    """启动系统 Edge；不可用时跳过而不是失败（保证套件在别的机器上仍绿）。"""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # 没有 Edge / 未安装浏览器
            pytest.skip(f"系统 Edge 不可用，跳过 E2E：{exc}")
        try:
            yield instance
        finally:
            instance.close()


def test_login_page_renders(browser, live_server):
    """未登录时应看到登录页，且看不到主应用。"""
    page = browser.new_page()
    page.goto(live_server, wait_until="domcontentloaded")
    assert page.is_visible("#login-panel")
    assert not page.is_visible("#app")
    assert "智能知识库" in page.title()


def test_register_login_upload_ask_flow(browser, live_server, tmp_path: Path):
    """核心闭环：注册 → 进入应用 → 上传文档 → 提问 → 看到回答与引用来源。"""
    page = browser.new_page()
    page.goto(live_server, wait_until="domcontentloaded")

    # 1) 注册
    page.fill("#login-username", "e2euser")
    page.fill("#login-password", "e2e123456")
    page.click("button[onclick='doRegister()']")
    page.wait_for_selector("#app:not(.hidden)", timeout=15_000)

    # 顶栏应显示用户名，三栏布局就位
    assert page.inner_text("#auth-user") == "e2euser"
    assert page.is_visible(".sidebar-left")
    assert page.is_visible(".sidebar-right")

    # 2) 上传一篇制度文档（用文件输入框，走真实 /upload）
    doc = tmp_path / "员工考勤制度.txt"
    doc.write_text("员工考勤制度：出差住宿标准：一线城市每晚不超过 600 元。", encoding="utf-8")
    page.set_input_files("#file-input", str(doc))
    page.wait_for_selector("#upload-btn:not([disabled])")
    page.click("#upload-btn")

    # 文档列表应出现该文件（说明上传 + 列表刷新都通了）
    page.wait_for_selector(".doc-item", timeout=20_000)
    assert "员工考勤制度.txt" in page.inner_text("#doc-list")

    # 3) 提问（含知识库关键词 → Router 走 rag 分支）
    page.fill("#question", "出差住宿标准是多少？")
    page.click("#ask-btn")

    # 4) 回答气泡出现，且带 mode 标签与「引用来源」区块
    page.wait_for_selector(".msg.assistant .markdown", timeout=30_000)
    page.wait_for_function(
        "() => !document.querySelector('#messages').innerText.includes('思考中')",
        timeout=30_000,
    )
    messages_text = page.inner_text("#messages")
    assert "出差住宿标准是多少？" in messages_text

    # 离线 FakeLLM 会把检索到的片段回显进回答，因此可以断言引用来源被渲染出来
    page.wait_for_selector(".sources", timeout=30_000)
    assert "员工考勤制度.txt" in page.inner_text(".sources")

    # 5) 会话列表出现一条记录（说明落库 + 列表刷新通了）
    page.wait_for_selector(".conv-item", timeout=15_000)


def test_calculation_question_shows_agent_mode(browser, live_server):
    """纯计算表达式应被 Router 判为 agent 并在气泡上显示 mode 标签。"""
    page = browser.new_page()
    page.goto(live_server, wait_until="domcontentloaded")
    page.fill("#login-username", "e2eagent")
    page.fill("#login-password", "e2e123456")
    page.click("button[onclick='doRegister()']")
    page.wait_for_selector("#app:not(.hidden)", timeout=15_000)

    page.fill("#question", "100 * 1.08")
    page.click("#ask-btn")
    page.wait_for_selector(".mode-tag", timeout=30_000)
    assert page.inner_text(".mode-tag").strip() == "agent"
