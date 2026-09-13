"""真实流式（SSE）端到端验收 —— 补上 V2 唯一没在真机跑过的主干路径。

为什么单独做这个：V2 的流式全用 `FakeLLMClient` 桩验证过，但桩**不可能**暴露真机问题：

1. **中文多字节切分**：UTF-8 一个汉字 3 字节，网络分帧会把字节切在字符中间；
   桩按字符吐，永远测不到。
2. **推理模型的思考吃预算**：`deepseek-v4-flash` 先输出 `reasoning_content`，
   `max_tokens` 不够时正文一个字都出不来（V1 已在**非流式**路径踩过 P0，流式同源）。
3. **首字延迟（TTFT）**：流式的全部价值就是"更早看到字"，不测等于没验证。
4. **SSE 帧跨 TCP 包断裂**：事件是 `event: x\ndata: {...}\n\n`，一个帧可能被拆到两个包里。

本脚本对**真实 DeepSeek API + 真实 MySQL + 真实 bge + 真实 Chroma**跑一遍：

| 阶段 | 验证 |
| --- | --- |
| 客户端层 | `OpenAICompatClient.chat_stream` 直连真实 API：非空、无 U+FFFD、TTFT |
| HTTP 层 | 真起 uvicorn，`POST /ask/stream` 收完整 SSE：事件序列、增量数、TTFT、总耗时 |
| 一致性 | 同一问题流式拼出的答案 vs 非流式 `/ask` 的答案 |
| 回退 | `ENABLE_STREAM=false` 时 `/ask/stream` 必须 404、`/ask` 必须 200 |

用法::

    .venv\\Scripts\\python scripts/verify_streaming.py              # 全部
    .venv\\Scripts\\python scripts/verify_streaming.py --skip-http  # 只测客户端层（不连库）

注意：**会真实调用 LLM API（消耗 token）**，并在真实库里临时建一个验收账号（结束前删除）。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

#: 验收用账号（结束时会连同其会话/消息一起删除）
CHECK_USER = "streamcheck"
CHECK_PWD = "stream-check-123456"

_failures: list[str] = []
_metrics: dict[str, object] = {}


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "  [OK]  " if ok else "  [FAIL]"
    print(f"{mark} {label}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(label)
    return ok


def _has_fffd(text: str) -> bool:
    """U+FFFD 是解码失败的替换字符 —— 它出现就说明多字节被切坏了。"""
    return "\ufffd" in text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------- 阶段 1：客户端层直连真实 API ----------------

def phase_client() -> None:
    print("\n=== [1/4] LLM 客户端层：直连真实 API 的 chat_stream ===")
    # 用容器里的真实客户端：签名以 container 为准，避免这里写死参数名而与容器漂移
    from app.container import build_container

    real = build_container(settings).llm

    prompt = "请用一句话说明：企业知识库里为什么需要引用来源？"
    t0 = time.monotonic()
    first_at: float | None = None
    pieces: list[str] = []
    for piece in real.chat_stream([{"role": "user", "content": prompt}]):
        if first_at is None:
            first_at = time.monotonic()
        pieces.append(piece)
    total = time.monotonic() - t0
    text = "".join(pieces)

    _metrics["client_ttft_s"] = round((first_at - t0), 3) if first_at else None
    _metrics["client_total_s"] = round(total, 3)
    _metrics["client_pieces"] = len(pieces)
    _metrics["client_chars"] = len(text)

    print(f"  真实模型：{settings.llm_model}（{settings.llm_api_base}）")
    print(f"  首字延迟 {_metrics['client_ttft_s']}s / 总耗时 {_metrics['client_total_s']}s / "
          f"增量 {len(pieces)} 片 / 正文 {len(text)} 字符")
    print(f"  正文前 80 字：{text[:80]!r}")

    check("客户端层：流式返回非空正文", bool(text.strip()), f"len={len(text)}")
    check("客户端层：无解码替换字符 U+FFFD（多字节未被切坏）", not _has_fffd(text))
    check("客户端层：确实分成了多个增量（不是一次性返回）", len(pieces) > 1, f"增量数={len(pieces)}")
    check("客户端层：首字延迟已实测且为正数", bool(_metrics["client_ttft_s"]))


# ---------------- 阶段 2~4：HTTP 层 ----------------

def _wait_health(base: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪：{base}")


class _LiveServer:
    """在后台线程里起真实 uvicorn（真实 MySQL/Chroma/bge/LLM）。"""

    def __init__(self, cfg) -> None:
        import uvicorn

        from app.factory import create_app

        app = create_app(cfg)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        _wait_health(self.base)
        return self.base

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


def _register_and_token(base: str) -> str:
    import httpx

    with httpx.Client(timeout=30) as c:
        resp = c.post(f"{base}/auth/register", json={"username": CHECK_USER, "password": CHECK_PWD})
        if resp.status_code == 409:  # 上次跑挂了残留
            resp = c.post(f"{base}/auth/login", json={"username": CHECK_USER, "password": CHECK_PWD})
        resp.raise_for_status()
        return resp.json()["token"]


def _consume_sse(base: str, token: str, question: str) -> dict:
    """用 httpx 收完整 SSE，记录每个事件的到达时刻（用于算首字延迟）。"""
    import httpx

    events: list[tuple[str, dict, float]] = []
    t0 = time.monotonic()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    with httpx.Client(timeout=180) as c:
        with c.stream("POST", f"{base}/ask/stream", json={"question": question}, headers=headers) as resp:
            status = resp.status_code
            ctype = resp.headers.get("content-type", "")
            name: str | None = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    name = line[7:].strip()
                elif line.startswith("data: ") and name:
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        payload = {"_raw": line[6:]}
                    events.append((name, payload, time.monotonic() - t0))
                    name = None

    deltas = [p.get("text", "") for (n, p, _) in events if n == "delta"]
    meta = next((p for (n, p, _) in events if n == "meta"), {})
    sources = next((p.get("sources", []) for (n, p, _) in events if n == "sources"), [])
    errors = [p.get("detail") for (n, p, _) in events if n == "error"]
    first_delta_at = next((t for (n, _, t) in events if n == "delta"), None)
    done_at = next((t for (n, _, t) in events if n in ("done", "error")), None)

    return {
        "status": status,
        "content_type": ctype,
        "names": [n for (n, _, _) in events],
        "text": "".join(deltas),
        "delta_count": len(deltas),
        "meta": meta,
        "sources": sources,
        "errors": errors,
        "ttft": first_delta_at,
        "total": done_at,
    }


def _consume_sse_with_health_probe(base: str, token: str, question: str) -> dict:
    """收 SSE 的同时后台轮询 `/healthz`。

    用来证明**流式期间事件循环没有被阻塞**：`chat_stream` 是同步生成器，如果在事件循环里
    直接迭代，整个服务会在生成期间卡死（真机实测一次 RAG 流式 15 秒，期间 /healthz 也超时）。
    """
    import httpx

    latencies: list[float] = []
    stop = threading.Event()

    def probe() -> None:
        with httpx.Client(timeout=5) as c:
            while not stop.is_set():
                t = time.monotonic()
                try:
                    c.get(f"{base}/healthz")
                    latencies.append(time.monotonic() - t)
                except Exception:  # noqa: BLE001 —— 超时/被拒都记为"不可用"
                    latencies.append(float("inf"))
                stop.wait(0.15)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        result = _consume_sse(base, token, question)
    finally:
        stop.set()
        thread.join(timeout=5)

    result["health_max"] = max(latencies) if latencies else None
    result["health_count"] = len(latencies)
    return result


def phase_http(cfg) -> None:
    print("\n=== [2~4/4] HTTP 层：真实 /ask/stream ===")
    import httpx

    with _LiveServer(cfg) as base:
        print(f"  服务已就绪：{base}")
        token = _register_and_token(base)

        check("HTTP：GET /config 报告 enable_stream=true",
              httpx.get(f"{base}/config", headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
              .get("enable_stream") is True)

        question = "出差住宿标准是多少？"
        print(f"\n  提问（走 RAG，真实检索 + 真实生成）：{question}")
        result = _consume_sse_with_health_probe(base, token, question)

        print(f"  事件序列：{result['names'][:3]} ... {result['names'][-2:]}（共 {len(result['names'])} 条事件）")
        print(f"  首字延迟 {result['ttft']:.3f}s / 总耗时 {result['total']:.3f}s / "
              f"增量 {result['delta_count']} 片 / 正文 {len(result['text'])} 字符")
        print(f"  流式期间 /healthz 探测 {result['health_count']} 次，最慢 {result['health_max']:.4f}s")
        print(f"  正文前 120 字：{result['text'][:120]!r}")
        if result["sources"]:
            print(f"  来源：{[(s.get('document_name'), s.get('score')) for s in result['sources']]}")

        _metrics["http_ttft_s"] = round(result["ttft"], 3) if result["ttft"] else None
        _metrics["http_total_s"] = round(result["total"], 3) if result["total"] else None
        _metrics["http_ttft_ratio"] = (
            round(result["ttft"] / result["total"], 3) if result["ttft"] and result["total"] else None
        )
        _metrics["http_delta_count"] = result["delta_count"]
        _metrics["http_chars"] = len(result["text"])
        _metrics["http_sources"] = len(result["sources"])
        _metrics["http_mode"] = result["meta"].get("mode")
        _metrics["health_probe_max_s"] = round(result["health_max"], 4) if result["health_max"] else None

        check("HTTP：状态 200", result["status"] == 200, str(result["status"]))
        check("HTTP：Content-Type 是 text/event-stream", "text/event-stream" in result["content_type"],
              result["content_type"])
        check("HTTP：事件序列以 meta 开始、以 done 结束",
              result["names"][:1] == ["meta"] and result["names"][-1:] == ["done"], str(result["names"]))
        check("HTTP：没有 error 事件", not result["errors"], str(result["errors"]))
        check("HTTP：正文非空（推理模型没把预算吃光）", bool(result["text"].strip()),
              f"len={len(result['text'])}")
        check("HTTP：无 U+FFFD（中文多字节跨包未被切坏）", not _has_fffd(result["text"]))
        check("HTTP：确实逐段流式（增量 > 1）", result["delta_count"] > 1, str(result["delta_count"]))
        check("HTTP：带回了引用来源", bool(result["sources"]), f"{len(result['sources'])} 条")
        check("HTTP：首字明显早于结束（流式确实更早出字）",
              bool(result["ttft"] and result["total"] and result["ttft"] < result["total"]),
              f"ttft={result['ttft']} total={result['total']}")
        check("HTTP：mode 被下发", result["meta"].get("mode") in ("rag", "chat", "agent"), str(result["meta"]))
        # 真机回归：旧实现在事件循环里同步迭代生成器，生成期间整个服务被卡住
        check("HTTP：流式期间 /healthz 未被阻塞（< 1s）",
              bool(result["health_max"] is not None and result["health_max"] < 1.0),
              f"最慢探测 {result['health_max']}s")

        # ---- 阶段 3：与非流式一致性 ----
        print("\n  === 与非流式 /ask 对比 ===")
        with httpx.Client(timeout=180) as c:
            t0 = time.monotonic()
            plain = c.post(f"{base}/ask", json={"question": question},
                           headers={"Authorization": f"Bearer {token}"})
            plain_s = time.monotonic() - t0
        plain_json = plain.json()
        print(f"  非流式：{plain.status_code} / {plain_s:.3f}s / {len(plain_json.get('answer', ''))} 字符")
        print(f"  非流式正文前 120 字：{plain_json.get('answer', '')[:120]!r}")
        check("一致性：非流式也返回 200", plain.status_code == 200, str(plain.status_code))
        check("一致性：两条路径都拿到了非空答案",
              bool(result["text"].strip()) and bool(plain_json.get("answer", "").strip()))
        check("一致性：两条路径的模式一致",
              plain_json.get("mode") == result["meta"].get("mode"),
              f"{plain_json.get('mode')} vs {result['meta'].get('mode')}")

        _cleanup_check_user(base, token)


def _cleanup_check_user(base: str, token: str) -> None:
    """删掉验收账号（会话用接口删，用户直接走 SQL）。"""
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60) as c:
        convs = c.get(f"{base}/conversations", headers=headers).json()
        for conv in convs:
            c.delete(f"{base}/conversations/{conv['conversation_id']}", headers=headers)
        print(f"\n  已清理验收账号的 {len(convs)} 个会话")


def phase_fallback() -> None:
    print("\n=== [4/4] 回退：ENABLE_STREAM=false ===")
    import httpx

    off = dataclasses.replace(settings, enable_stream=False)
    with _LiveServer(off) as base:
        token = _register_and_token(base)
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=120) as c:
            cfg_resp = c.get(f"{base}/config", headers=headers)
            stream_resp = c.post(f"{base}/ask/stream", json={"question": "你好"}, headers=headers)
            plain_resp = c.post(f"{base}/ask", json={"question": "你好"}, headers=headers)
        check("回退：/config 报告 enable_stream=false", cfg_resp.json().get("enable_stream") is False,
              cfg_resp.text)
        check("回退：/ask/stream 返回 404（接口视作不存在）", stream_resp.status_code == 404,
              str(stream_resp.status_code))
        check("回退：/ask 仍然 200（非流式可用）", plain_resp.status_code == 200,
              str(plain_resp.status_code))


def _delete_check_user_sql() -> None:
    """直连 SQL 删掉验收账号（user 表没有删除接口）。"""
    try:
        import pymysql
    except ImportError:
        return
    conn = pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
        password=settings.mysql_password, database=settings.mysql_db, charset="utf8mb4", autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE m FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                        "JOIN users u ON u.tenant_id = c.tenant_id "
                        "WHERE u.username = %s", (CHECK_USER,))
            cur.execute("DELETE c FROM conversations c JOIN users u ON u.tenant_id = c.tenant_id "
                        "WHERE u.username = %s", (CHECK_USER,))
            cur.execute("DELETE FROM users WHERE username = %s", (CHECK_USER,))
            print(f"  已从 users 删除验收账号 {CHECK_USER}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="真实流式（SSE）端到端验收")
    parser.add_argument("--skip-http", action="store_true", help="只测 LLM 客户端层（不连库、不起服务）")
    args = parser.parse_args()

    if not settings.llm_api_key:
        print("[错误] LLM_API_KEY 未配置，无法做真机流式验收", file=sys.stderr)
        return 2

    print(f"LLM: {settings.llm_model} @ {settings.llm_api_base}")
    print(f"MySQL: {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}")
    print(f"向量库: {settings.vector_store} @ {settings.chroma_persist_dir}")

    phase_client()
    if not args.skip_http:
        try:
            phase_http(settings)
            phase_fallback()
        finally:
            _delete_check_user_sql()

    print("\n" + "=" * 62)
    print("实测指标：")
    for key, value in _metrics.items():
        print(f"  {key:22} = {value}")
    print("=" * 62)
    if _failures:
        print(f"结果：{len(_failures)} 项未通过")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
