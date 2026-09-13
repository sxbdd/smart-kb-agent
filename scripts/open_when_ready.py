"""等待服务就绪后再打开浏览器。

**为什么需要它**：`start.bat` 原先在启动 uvicorn **之前**就执行
`start "" http://127.0.0.1:8000/`，而应用要等 reloader 子进程 import 完
`app.main` 并加载 bge 模型（数秒到十几秒）才开始监听端口 —— 浏览器抢跑，
用户看到的就是 `ERR_CONNECTION_REFUSED`（见 docs/deployment.md 排障）。

本脚本由 start.bat 用 `pythonw` 后台调用：轮询 `/healthz`，**200 才开浏览器**；
超时则弹一个提示框并写日志（首次运行要下载模型，可能等很久）。

用法：
    python scripts/open_when_ready.py [--url http://127.0.0.1:8000] [--timeout 600]
    python scripts/open_when_ready.py --dry-run        # 只探测，不开浏览器（用于验证/CI）
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

_log_file = None


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    if _log_file is not None:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except OSError:
            pass


def _warn(title: str, text: str) -> None:
    """超时提示。用 MessageBox 是因为调用方是 pythonw（没有控制台可打印）。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00000030)  # MB_ICONWARNING
    except Exception:
        pass


def wait_until_ready(url: str, timeout: float, interval: float = 0.5) -> bool:
    """轮询 `<url>/healthz`，200 即返回 True；超时返回 False。"""
    health = url.rstrip("/") + "/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)
    return False


def main() -> int:
    global _log_file
    parser = argparse.ArgumentParser(description="等服务 /healthz 就绪后打开浏览器")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=600.0, help="最长等待秒数（首次下载模型可能很久）")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--log", default=None, help="日志文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只探测，不打开浏览器")
    args = parser.parse_args()

    if args.log:
        try:
            path = Path(args.log)
            path.parent.mkdir(parents=True, exist_ok=True)
            _log_file = path.open("a", encoding="utf-8")
        except OSError:
            _log_file = None

    _log(f"waiting for {args.url}/healthz (timeout {args.timeout:.0f}s, dry_run={args.dry_run})")

    if wait_until_ready(args.url, args.timeout, args.interval):
        _log("ready")
        if not args.dry_run:
            webbrowser.open(args.url)
        return 0

    _log("timeout")
    if not args.dry_run:
        _warn(
            "服务启动超时",
            f"等待 {args.url} 就绪超过 {args.timeout:.0f} 秒。\n\n"
            "首次运行需要下载 bge 模型（约 100MB），请查看启动窗口的日志。\n"
            f"服务就绪后手动打开：{args.url}",
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
