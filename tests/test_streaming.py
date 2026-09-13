"""流式输出（SSE）：LLM 层解析与健壮性、服务层 `stream_answer`、接口层事件序列与落库。

全部离线：LLM 用桩 httpx 或 `FakeLLMClient`，向量库 memory，数据库 FakeDatabase。

注意：`app/api/routes_stream.py` 的挂载属于**共享文件** `app/factory.py` 的接线工作，
本文件用 `stream_app` fixture 自行 `include_router` —— 与 Lead 在 factory 里的挂法一致，
因此既能量到真实行为，又不用改别人的文件。
"""
from __future__ import annotations

import dataclasses
import io
from types import SimpleNamespace

import pytest

from app.core.llm_client import FakeLLMClient, OpenAICompatClient
from app.utils.exceptions import LLMError

POLICY = (
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
)


# ==================== 桩 httpx（SSE） ====================

class _StreamResp:
    """模拟 `httpx` 的流式响应：`iter_lines()` 逐行产出 SSE 文本。"""

    def __init__(self, lines, status_code: int = 200, text: str = "", fail_at: int | None = None) -> None:
        self._lines = list(lines)
        self.status_code = status_code
        self.text = text
        self.fail_at = fail_at
        self.closed = False

    def iter_lines(self):
        for i, line in enumerate(self._lines):
            if self.fail_at is not None and i == self.fail_at:
                raise OSError("连接被重置")
            yield line

    def close(self) -> None:
        self.closed = True


def _sse(*chunks, finish_reason=None, reasoning: int = 0):
    """把若干文本增量打包成 SSE 行。"""
    lines = []
    if reasoning:
        lines.append("data: " + _json({"choices": [{"delta": {"reasoning_content": "思" * reasoning}}]}))
    for c in chunks:
        lines.append("data: " + _json({"choices": [{"delta": {"content": c}}]}))
    if finish_reason:
        lines.append("data: " + _json({"choices": [{"delta": {}, "finish_reason": finish_reason}]}))
    lines.append("data: [DONE]")
    return lines


def _json(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _truncated_stream(reasoning: int = 500):
    """推理模型把预算吃光：整条流没有任何 content，finish_reason=length。"""
    return _StreamResp(_sse(finish_reason="length", reasoning=reasoning))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """去掉重试退避，避免用例真的等待 1~3 秒（只影响 chat_stream 内部取的别名）。"""
    monkeypatch.setattr("app.core.llm_client.time.sleep", lambda *_: None)


def _stream_client(responses, max_tokens: int = 256, max_retries: int = 3):
    client = OpenAICompatClient("http://x/v1", "k", "m", max_tokens=max_tokens, max_retries=max_retries)
    budgets: list[int] = []
    payloads: list[dict] = []

    def post(url, headers=None, json=None, timeout=None, stream=False):
        budgets.append(json["max_tokens"])
        payloads.append(json)
        assert stream is True, "chat_stream 必须以 stream=True 发起请求"
        return responses.pop(0)

    client.httpx = SimpleNamespace(post=post)
    return client, budgets, payloads


# ==================== 1. SSE 解析 ====================

def test_chat_stream_parses_sse_frames_and_stops_at_done():
    """多行 `data:` 帧应逐段产出；`[DONE]` 之后的内容必须被丢弃。"""
    lines = _sse("你好", "，", "世界", finish_reason="stop")
    lines.append("data: " + _json({"choices": [{"delta": {"content": "不应出现"}}]}))
    client, budgets, payloads = _stream_client([_StreamResp(lines)])

    pieces = list(client.chat_stream([{"role": "user", "content": "hi"}]))

    assert pieces == ["你好", "，", "世界"]
    assert "".join(pieces) == "你好，世界"
    assert payloads[0]["stream"] is True
    assert budgets == [256]


def test_chat_stream_ignores_heartbeat_and_malformed_frames():
    """注释行 / 空帧 / 非 JSON 帧是正常的 SSE 噪声，不能打断整条流。"""
    lines = [
        ": keep-alive",
        "",
        "data: not-json",
        "event: ping",
        *(_sse("答", "案", finish_reason="stop")),
    ]
    client, _, _ = _stream_client([_StreamResp(lines)])
    assert "".join(client.chat_stream([{"role": "user", "content": "hi"}])) == "答案"


def test_chat_stream_api_error_is_retried():
    """非 200：应重试；重试成功后正常产出。"""
    bad = _StreamResp([], text="boom", status_code=500)
    good = _StreamResp(_sse("恢复", finish_reason="stop"))
    client, budgets, _ = _stream_client([bad, good], max_retries=3)

    assert "".join(client.chat_stream([{"role": "user", "content": "hi"}])) == "恢复"
    assert len(budgets) == 2
    assert bad.closed, "失败响应必须显式关闭，避免连接泄漏"


# ==================== 2. 推理模型截断：流式路径也要加倍预算 ====================

def test_chat_stream_doubles_budget_when_truncated():
    client, budgets, _ = _stream_client(
        [_truncated_stream(), _StreamResp(_sse("真答案", finish_reason="stop"))],
        max_tokens=256,
    )

    assert "".join(client.chat_stream([{"role": "user", "content": "hi"}])) == "真答案"
    assert budgets == [256, 512], "被 length 截断且零产出时，流式路径也必须翻倍预算重试"


def test_chat_stream_doubling_is_capped_at_ceiling():
    client, budgets, _ = _stream_client([_truncated_stream()] * 3, max_tokens=4096, max_retries=3)

    with pytest.raises(LLMError):
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert budgets == [4096, 8192, 8192], "预算不得超过 MAX_TOKEN_CEILING"


def test_chat_stream_empty_without_length_does_not_inflate_budget():
    empty = _StreamResp(_sse(finish_reason="stop"))
    client, budgets, _ = _stream_client([empty, empty, empty], max_tokens=256, max_retries=3)

    with pytest.raises(LLMError) as exc:
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert "空内容" in str(exc.value)
    assert budgets == [256, 256, 256], "真正的空返回不应无脑翻倍"


def test_chat_stream_midway_failure_raises_instead_of_silently_truncating():
    """已产出内容后流断掉：必须抛 LLMError，绝不能把半截答案当成功返回。"""
    broken = _StreamResp(_sse("前半", finish_reason=None) + ["data: " + _json({"choices": [{"delta": {"content": "后半"}}]})],
                         fail_at=1)
    client, budgets, _ = _stream_client([broken, _StreamResp(_sse("不该被用到"))], max_retries=3)

    with pytest.raises(LLMError) as exc:
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert "流式中断" in str(exc.value)
    assert len(budgets) == 1, "已产出内容后不允许重试（会造成重复输出）"
    assert broken.closed, "中断的响应也要关闭"


def test_chat_stream_failure_before_any_content_is_retried():
    """一个字都没产出就断掉：属于可恢复故障，退避重试。"""
    broken = _StreamResp(_sse("前半"), fail_at=0)
    client, budgets, _ = _stream_client([broken, _StreamResp(_sse("恢复", finish_reason="stop"))], max_retries=3)

    assert "".join(client.chat_stream([{"role": "user", "content": "hi"}])) == "恢复"
    assert len(budgets) == 2


def test_chat_stream_without_done_frame_is_treated_as_truncated():
    """上游没发 `[DONE]` 也没有 finish_reason 就结束 → 视为截断，重试。"""
    no_done = _StreamResp(['data: ' + _json({"choices": [{"delta": {"reasoning_content": "思"}}]}), ""])
    client, budgets, _ = _stream_client([no_done, _StreamResp(_sse("补上", finish_reason="stop"))], max_retries=3)

    assert "".join(client.chat_stream([{"role": "user", "content": "hi"}])) == "补上"
    assert len(budgets) == 2


def test_chat_stream_exhausted_retries_raise_llm_error():
    broken = [_StreamResp([], fail_at=0) for _ in range(3)]
    client, budgets, _ = _stream_client(broken, max_retries=3)

    with pytest.raises(LLMError):
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert len(budgets) == 3


# ==================== 3. Fake 流式 == 非流式 ====================

@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "你好"}],
    [{"role": "user", "content": "文档内容开始\n出差住宿标准 600 元。\n文档内容结束\n用户问题：出差住宿标准？\n请回答："}],
])
def test_fake_chat_stream_concatenation_equals_chat(messages):
    fake = FakeLLMClient()
    pieces = list(fake.chat_stream(messages))

    assert len(pieces) > 1, "FakeLLMClient 应真的切片，而不是一次性吐出"
    assert "".join(pieces) == fake.chat(messages)


def test_fake_chat_stream_chunk_size_is_fixed():
    fake = FakeLLMClient()
    pieces = list(fake.chat_stream([{"role": "user", "content": "你好"}]))
    assert all(len(p) <= fake.STREAM_CHUNK_SIZE for p in pieces)
    assert all(len(p) == fake.STREAM_CHUNK_SIZE for p in pieces[:-1])


# ==================== 4. 服务层 stream_answer ====================

def _ingest_policy(services, name: str = "员工考勤制度.txt"):
    from pathlib import Path

    docs_dir = Path(services["settings"].documents_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / name
    path.write_text(POLICY, encoding="utf-8")
    return services["ingestion"].ingest(str(path), name)


def test_stream_answer_matches_answer_and_keeps_min_score_wiring(services, monkeypatch):
    """`stream_answer` 与非流式同源：相同来源、相同文本，且 min_score 仍然透传。"""
    _ingest_policy(services)
    seen: list[float] = []
    real_query = services["store"].query

    def spy_query(embedding, top_k, min_score=0.0, tenant_id=None):
        seen.append(min_score)
        return real_query(embedding, top_k, min_score=min_score, tenant_id=tenant_id)

    monkeypatch.setattr(services["store"], "query", spy_query)

    sources, deltas = services["rag"].stream_answer("出差住宿标准是多少？", top_k=3)
    streamed = "".join(deltas)

    expected, expected_sources = services["rag"].answer("出差住宿标准是多少？", top_k=3)

    assert streamed == expected
    assert [s.chunk_id for s in sources] == [s.chunk_id for s in expected_sources]
    assert sources[0].document_name == "员工考勤制度.txt"
    assert seen == [services["rag"].min_score, services["rag"].min_score], "min_score 必须继续透传"


# ==================== 5. 接口层：/ask/stream ====================

@pytest.fixture
def stream_client_factory(settings, fake_db):
    """构造带 `/ask/stream` 的 TestClient。

    Lead 已在 `app/factory.py` 里 include 了 routes_stream；这里做**幂等**处理：
    只有当前应用还没挂载该路由（例如接线被回滚）时才补一次，避免路径重复注册。
    """
    from fastapi.testclient import TestClient

    from app.api import routes_stream
    from app.factory import create_app

    def _make(cfg=None):
        app = create_app(cfg or settings)
        mounted = any(getattr(r, "path", None) == "/ask/stream" for r in app.routes)
        if not mounted:
            app.include_router(routes_stream.router)
        return TestClient(app)

    return _make


@pytest.fixture
def stream_client(stream_client_factory):
    return stream_client_factory()


@pytest.fixture
def stream_auth(stream_client) -> dict[str, str]:
    resp = stream_client.post("/auth/register", json={"username": "streamer", "password": "test123456"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 `[(event, data), ...]`，顺带断言帧格式。"""
    import json

    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        assert name is not None and data is not None, f"事件帧格式不对：{block!r}"
        events.append((name, data))
    return events


def _upload_policy(client, auth, name: str = "员工考勤制度.txt"):
    resp = client.post(
        "/upload",
        files={"file": (name, io.BytesIO(POLICY.encode("utf-8")), "text/plain")},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text


def test_ask_stream_event_sequence_and_content_type(stream_client, stream_auth):
    _upload_policy(stream_client, stream_auth)
    resp = stream_client.post("/ask/stream", json={"question": "出差住宿标准是多少？", "top_k": 3}, headers=stream_auth)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    names = [n for n, _ in events]
    # 事件序列固定：meta → delta* → sources → done
    assert names[0] == "meta"
    assert names[-2:] == ["sources", "done"]
    assert names.count("delta") >= 1
    assert set(names[1:-2]) == {"delta"}, f"meta 与 sources 之间只允许 delta：{names}"

    meta = events[0][1]
    assert meta["mode"] == "rag"
    assert meta["conversation_id"]

    text = "".join(d["text"] for n, d in events if n == "delta")
    assert text.strip(), "应产出增量文本"

    sources = dict(events)["sources"]["sources"]
    assert sources and sources[0]["document_name"] == "员工考勤制度.txt"
    assert dict(events)["done"] == {}


def test_ask_stream_chat_mode_has_empty_sources(stream_client, stream_auth):
    resp = stream_client.post("/ask/stream", json={"question": "你好"}, headers=stream_auth)
    events = _parse_sse(resp.text)

    assert events[0] == ("meta", {"mode": "chat", "conversation_id": events[0][1]["conversation_id"]})
    assert dict(events)["sources"]["sources"] == []
    assert events[-1] == ("done", {})


def test_ask_stream_persists_two_messages(stream_client, stream_auth, fake_db):
    _upload_policy(stream_client, stream_auth)
    resp = stream_client.post("/ask/stream", json={"question": "出差住宿标准是多少？"}, headers=stream_auth)
    events = _parse_sse(resp.text)
    cid = events[0][1]["conversation_id"]
    streamed = "".join(d["text"] for n, d in events if n == "delta")

    msgs = [m for m in fake_db.messages if m["conversation_id"] == cid]
    assert [m["role"] for m in msgs] == ["user", "assistant"], "流式结束后也要落库 2 条消息"
    assert msgs[0]["content"] == "出差住宿标准是多少？"
    assert msgs[1]["content"] == streamed
    assert msgs[1]["sources"], "assistant 消息必须带引用来源"

    # 会话历史接口也应能读到
    history = stream_client.get(f"/conversations/{cid}", headers=stream_auth).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]


def test_ask_stream_emits_error_event_instead_of_dropping_connection(stream_client, stream_auth, monkeypatch):
    """生成阶段抛异常：先发 meta，再发 error，响应仍然是完整的 SSE（200）。"""
    from app.services.rag import RAGService

    def boom(self, *a, **k):
        raise LLMError("上游模型 502")

    monkeypatch.setattr(RAGService, "stream_answer", boom)

    resp = stream_client.post("/ask/stream", json={"question": "出差住宿标准是多少？"}, headers=stream_auth)
    assert resp.status_code == 200, "异常不能变成断流/非 200"
    events = _parse_sse(resp.text)

    names = [n for n, _ in events]
    assert names[0] == "meta"
    assert "error" in names
    assert list(dict(events)["error"].keys()) == ["detail"]
    assert "502" in dict(events)["error"]["detail"]
    # error 之后不允许再有 delta/done
    assert "done" not in names


def test_ask_stream_retrieval_failure_still_reports_error(stream_client, stream_auth, monkeypatch):
    """检索阶段（meta 之前）失败：同样以 error 事件收尾，而不是断流。"""
    from app.services.rag import RAGService

    def boom(self, *a, **k):
        raise LLMError("检索后端不可用")

    monkeypatch.setattr(RAGService, "_prepare", boom)

    resp = stream_client.post("/ask/stream", json={"question": "出差住宿标准是多少？"}, headers=stream_auth)
    events = _parse_sse(resp.text)
    assert [n for n, _ in events] == ["meta", "error"]
    assert "检索后端不可用" in dict(events)["error"]["detail"]


def test_ask_stream_requires_token(stream_client):
    assert stream_client.post("/ask/stream", json={"question": "你好"}).status_code == 401


def test_ask_stream_rejects_oversized_question(stream_client, stream_auth, settings):
    payload = {"question": "x" * (settings.max_question_chars + 1)}
    assert stream_client.post("/ask/stream", json=payload, headers=stream_auth).status_code == 422


def test_ask_stream_disabled_returns_404(settings, stream_client_factory):
    """`ENABLE_STREAM=false` 时接口视为不存在（404），前端据此回退 /ask。"""
    client = stream_client_factory(dataclasses.replace(settings, enable_stream=False))
    resp = client.post("/auth/register", json={"username": "nostream", "password": "test123456"})
    auth = {"Authorization": f"Bearer {resp.json()['token']}"}

    assert client.post("/ask/stream", json={"question": "你好"}, headers=auth).status_code == 404


def test_runtime_config_reports_stream_flag(stream_client, stream_auth):
    body = stream_client.get("/config", headers=stream_auth).json()
    assert body == {"enable_stream": True}


def test_client_disconnect_still_persists_partial_answer(settings, fake_db):
    """客户端中途断开：已产出的内容必须落库，且**只落一次**（不能重复计费/重复写库）。"""
    import asyncio

    from app.api.routes_stream import _stream_body
    from app.factory import create_app
    from app.models.schemas import AskRequest

    app = create_app(settings)
    container = app.state.container

    class _SlowStream:
        """模拟"一直有增量"的上游，便于在流中间断开。"""

        def __init__(self) -> None:
            self.n = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.n += 1
            if self.n > 50:
                raise StopIteration
            return f"第{self.n}段"

    async def _drive():
        container.rag.stream_answer = lambda *a, **k: ([], iter(_SlowStream()))
        gen = _stream_body(container, AskRequest(question="出差住宿标准是多少？", top_k=3))
        first = await gen.__anext__()          # meta
        second = await gen.__anext__()         # 第一个 delta
        await gen.aclose()                     # 模拟连接断开

        messages = [m for m in fake_db.messages if m["role"] == "assistant"]
        assert len(messages) == 1, "断开时已产出的内容要落库，且只能落一次"
        assert messages[0]["content"] == "第1段"
        return first, second

    first, second = asyncio.run(_drive())
    assert first.startswith("event: meta")
    assert second.startswith("event: delta")
