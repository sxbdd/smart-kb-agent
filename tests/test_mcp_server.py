"""MCP server 协议层用例：**进程内** JSON-RPC 往返，不起子进程、不连真实依赖。

覆盖：`initialize` / `notifications/initialized` / `ping` / `tools/list` /
`tools/call`（两个工具各一次）/ 未知方法 / 非法参数 / 内部异常 / 坏 JSON /
id 回显与 jsonrpc 版本字段 / `--list-tools` / 容器惰性构建。

离线保证：容器一律用 `HashEmbedding` + `InMemoryVectorStore` + `FakeLLMClient`
+ `NoopReranker` 组装，**不调用真实 LLM、不连 MySQL、不访问外网**。
"""
from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_server import server
from mcp_server.server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    handle_message,
    main,
    serve,
)

POLICY = (
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00，午休 12:00-13:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
)


# ---------------- 辅助 ----------------


def _request(method: str, params: dict | None = None, msg_id: object = 1) -> dict:
    """构造一条 JSON-RPC 请求（带 id）。"""
    msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _call(name: str, arguments: dict, msg_id: object = 1) -> dict:
    """调用 tools/call 并返回响应 dict。"""
    return handle_message(_request("tools/call", {"name": name, "arguments": arguments}, msg_id))


def _text_of(response: dict) -> str:
    """取 MCP 工具返回的第一段文本。"""
    content = response["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    return content[0]["text"]


class _NullDatabase:
    """只记录写入的内存 DB 桩：IngestionService 仅用到 save_document。"""

    def __init__(self, *args, **kwargs) -> None:
        self.saved: list[dict] = []

    def init(self) -> None:
        return None

    def save_document(
        self,
        doc_id: str,
        filename: str,
        file_type: str,
        file_size: int,
        chunk_count: int,
        tenant_id: str = "default",
    ) -> None:
        self.saved.append({"id": doc_id, "filename": filename, "chunk_count": chunk_count, "tenant_id": tenant_id})

    def delete_document(self, doc_id: str, tenant_id: str = "default") -> None:
        self.saved = [d for d in self.saved if d["id"] != doc_id]


# ---------------- fixtures ----------------


@pytest.fixture
def offline_container(settings, monkeypatch):
    """全离线容器（不经过 build_container，因此绝不碰 MySQL / 真实 LLM）。"""
    from app.core.embedding import HashEmbedding
    from app.core.llm_client import FakeLLMClient
    from app.core.reranker import NoopReranker
    from app.core.vector_store import InMemoryVectorStore
    from app.services.ingestion import IngestionService
    from app.services.rag import RAGService

    embedder = HashEmbedding(settings.hash_embedding_dim)
    store = InMemoryVectorStore()
    llm = FakeLLMClient()
    reranker = NoopReranker()
    ingestion = IngestionService(
        embedder=embedder,
        vector_store=store,
        db=_NullDatabase(),
        documents_dir=settings.documents_dir,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    rag = RAGService(
        embedder=embedder,
        vector_store=store,
        llm=llm,
        reranker=reranker,
        top_k=settings.top_k,
        rerank_top_k=settings.rerank_top_k,
        max_history_messages=settings.max_history_messages,
    )
    container = SimpleNamespace(
        settings=settings, embedder=embedder, vector_store=store, llm=llm,
        reranker=reranker, ingestion=ingestion, rag=rag,
    )
    # 用 monkeypatch 写入协议层的容器缓存：用例结束自动还原，避免串味
    monkeypatch.setattr(server, "_container", container)
    return container


@pytest.fixture
def kb(offline_container):
    """在离线容器里入库一份制度文档，供检索 / 问答用例使用。"""
    docs_dir = Path(offline_container.settings.documents_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "员工考勤制度.txt"
    path.write_text(POLICY, encoding="utf-8")

    resp = offline_container.ingestion.ingest(str(path), path.name)
    assert resp.status == "success"
    assert resp.chunk_count >= 1
    return offline_container


# ---------------- 离线保证 ----------------


def test_container_uses_offline_components(kb) -> None:
    """容器必须由离线组件构成，否则用例可能偷偷打到真实 LLM / 向量库。"""
    assert type(kb.embedder).__name__ == "HashEmbedding"
    assert type(kb.vector_store).__name__ == "InMemoryVectorStore"
    assert type(kb.llm).__name__ == "FakeLLMClient"
    assert type(kb.reranker).__name__ == "NoopReranker"


# ---------------- initialize / ping / 通知 ----------------


def test_initialize_returns_protocol_version_and_server_info(kb) -> None:
    from app.config import settings as app_settings

    resp = handle_message(
        _request("initialize", {"protocolVersion": "2024-11-05", "clientInfo": {"name": "cursor"}}, msg_id=7)
    )

    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 7
    assert "error" not in resp

    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["version"] == SERVER_VERSION == "2.0.0"
    assert result["serverInfo"]["name"] == app_settings.mcp_server_name
    assert result["serverInfo"]["name"]


def test_initialize_reads_server_name_from_settings(kb, monkeypatch) -> None:
    """serverInfo.name 来自 settings.mcp_server_name，不是写死的常量。"""
    from app.config import settings as app_settings

    monkeypatch.setattr("app.config.settings", dataclasses.replace(app_settings, mcp_server_name="role-m-kb"))
    resp = handle_message(_request("initialize"))
    assert resp["result"]["serverInfo"]["name"] == "role-m-kb"


def test_notifications_initialized_returns_no_response(kb) -> None:
    """通知（无 id）不产生响应 —— MCP 客户端靠这一点继续流程，回了反而报错。"""
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_notification_is_silently_ignored(kb) -> None:
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) is None


def test_ping_returns_empty_result(kb) -> None:
    resp = handle_message(_request("ping"))
    assert resp["result"] == {}
    assert resp["jsonrpc"] == "2.0"


@pytest.mark.parametrize("msg_id", [1, "abc-1", 3.5, None])
def test_response_echoes_request_id(kb, msg_id: object) -> None:
    resp = handle_message(_request("ping", msg_id=msg_id))
    assert resp == {"jsonrpc": "2.0", "id": msg_id, "result": {}}


# ---------------- tools/list ----------------


def test_tools_list_exposes_two_well_formed_tools(kb) -> None:
    resp = handle_message(_request("tools/list"))

    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == ["knowledge_search", "ask_knowledge_base"]
    by_name = {t["name"]: t for t in tools}

    search_schema = by_name["knowledge_search"]["inputSchema"]
    assert search_schema["type"] == "object"
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["query"]["type"] == "string"
    assert search_schema["properties"]["top_k"]["type"] == "integer"
    assert search_schema["properties"]["top_k"]["minimum"] >= 1
    assert search_schema["additionalProperties"] is False

    ask_schema = by_name["ask_knowledge_base"]["inputSchema"]
    assert ask_schema["type"] == "object"
    assert ask_schema["required"] == ["question"]
    assert ask_schema["properties"]["question"]["type"] == "string"
    assert ask_schema["additionalProperties"] is False

    for tool in tools:
        assert tool["description"].strip()


def test_tools_list_is_json_serializable(kb) -> None:
    resp = handle_message(_request("tools/list"))
    # stdio 传输要求整条响应能一行 JSON 发出去
    assert json.loads(json.dumps(resp, ensure_ascii=False))["result"]["tools"]


# ---------------- tools/call ----------------


def test_tools_call_knowledge_search_returns_snippets_with_source(kb) -> None:
    resp = _call("knowledge_search", {"query": "出差住宿标准", "top_k": 3}, msg_id=11)

    assert resp["id"] == 11
    result = resp["result"]
    assert result["isError"] is False

    text = _text_of(resp)
    assert "员工考勤制度.txt" in text  # 来源文档名
    assert "600" in text  # 命中的片段内容
    assert "相似度" in text


def test_tools_call_knowledge_search_top_k_is_optional(kb) -> None:
    """top_k 省略时用主服务默认值，不得报错。"""
    resp = _call("knowledge_search", {"query": "年假"})
    assert resp["result"]["isError"] is False
    assert _text_of(resp)


def test_tools_call_ask_knowledge_base_returns_answer_with_citations(kb) -> None:
    resp = _call("ask_knowledge_base", {"question": "出差住宿标准是多少？"}, msg_id="ask-1")

    assert resp["id"] == "ask-1"
    result = resp["result"]
    assert result["isError"] is False

    text = _text_of(resp)
    assert "（测试回答）" in text  # FakeLLMClient 的确定性回答
    assert "引用来源" in text
    assert "员工考勤制度.txt" in text


def test_knowledge_search_on_empty_kb_is_graceful(offline_container) -> None:
    """空知识库不应报错：返回可读提示，isError 仍为 false。"""
    resp = _call("knowledge_search", {"query": "完全不存在的主题"})
    assert resp["result"]["isError"] is False
    assert "未在知识库中检索到" in _text_of(resp)


# ---------------- 错误约定 ----------------


def test_unknown_method_returns_32601_and_echoes_id(kb) -> None:
    resp = handle_message(_request("resources/list", msg_id="req-9"))

    assert resp["id"] == "req-9"
    assert resp["error"]["code"] == METHOD_NOT_FOUND == -32601
    assert "result" not in resp
    assert "resources/list" in resp["error"]["message"]


@pytest.mark.parametrize(
    "params",
    [
        {},  # 缺 name
        {"name": ""},  # name 为空
        {"name": "knowledge_search", "arguments": {}},  # 缺 query
        {"name": "knowledge_search", "arguments": {"query": ""}},  # query 为空串
        {"name": "knowledge_search", "arguments": {"query": 42}},  # query 类型错
        {"name": "knowledge_search", "arguments": {"query": "x", "top_k": 0}},  # top_k 越界
        {"name": "knowledge_search", "arguments": {"query": "x", "top_k": 999}},  # top_k 越界
        {"name": "knowledge_search", "arguments": {"query": "x", "top_k": "3"}},  # top_k 类型错
        {"name": "ask_knowledge_base", "arguments": {}},  # 缺 question
        {"name": "ask_knowledge_base", "arguments": {"question": None}},  # question 类型错
        {"name": "no_such_tool", "arguments": {}},  # 未知工具
        {"name": "knowledge_search", "arguments": [1, 2]},  # arguments 不是对象
    ],
)
def test_invalid_params_return_32602(kb, params: dict) -> None:
    resp = handle_message(_request("tools/call", params, msg_id=3))

    assert resp["id"] == 3
    assert resp["error"]["code"] == INVALID_PARAMS == -32602
    assert "result" not in resp


def test_internal_error_returns_32603_and_sets_is_error(kb, monkeypatch) -> None:
    """工具内部异常 → -32603，同时顶层 isError=true，且进程不受影响。"""

    def boom(*args, **kwargs):
        raise RuntimeError("向量库炸了")

    monkeypatch.setattr(kb.rag, "search", boom)

    resp = _call("knowledge_search", {"query": "考勤"})
    assert resp["error"]["code"] == INTERNAL_ERROR == -32603
    assert resp["isError"] is True
    assert "向量库炸了" in resp["error"]["message"]

    # 异常后协议层仍然可用（进程没崩）
    assert handle_message(_request("ping"))["result"] == {}


@pytest.mark.parametrize("msg", [[1, 2, 3], "hello", 42, None])
def test_non_object_request_returns_invalid_request(kb, msg: object) -> None:
    resp = handle_message(msg)  # type: ignore[arg-type] - 故意传非法类型
    assert resp["error"]["code"] == INVALID_REQUEST == -32600
    assert resp["id"] is None


@pytest.mark.parametrize(
    "msg, expected_code",
    [
        ({"jsonrpc": "2.0", "id": 1}, INVALID_REQUEST),  # 缺 method
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),  # 版本错
        ({"id": 1, "method": "ping"}, INVALID_REQUEST),  # 缺 jsonrpc
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]}, INVALID_PARAMS),  # params 类型错
    ],
)
def test_malformed_request_is_rejected_not_crashing(kb, msg: dict, expected_code: int) -> None:
    resp = handle_message(msg)
    assert resp["error"]["code"] == expected_code
    assert resp["id"] == 1


# ---------------- stdio 主循环 / CLI ----------------


def test_stdio_loop_round_trip_in_process(kb) -> None:
    """一行一条消息：坏 JSON 只影响该行，通知不回响应，其余照常往返。"""
    payload = "\n".join(
        [
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            "{ 这不是 JSON",
            '{"jsonrpc":"2.0","method":"notifications/initialized"}',
            "",
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        ]
    ) + "\n"

    out = io.StringIO()
    serve(stdin=io.StringIO(payload), stdout=out)

    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    # initialize + 解析错误 + tools/list = 3 行（通知与空行不产生响应）
    assert len(lines) == 3

    messages = [json.loads(line) for line in lines]
    assert [m.get("id") for m in messages] == [1, None, 2]
    assert messages[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert messages[1]["error"]["code"] == PARSE_ERROR == -32700
    assert [t["name"] for t in messages[2]["result"]["tools"]] == ["knowledge_search", "ask_knowledge_base"]


def test_list_tools_cli_flag(capsys) -> None:
    assert main(["--list-tools"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in payload["tools"]] == ["knowledge_search", "ask_knowledge_base"]


def test_main_refuses_to_start_when_disabled_in_settings(monkeypatch) -> None:
    """ENABLE_MCP=false 时不应进入 stdio 循环（客户端会看到进程退出，而不是静默挂死）。"""
    from app.config import settings as app_settings

    monkeypatch.setattr("app.config.settings", dataclasses.replace(app_settings, enable_mcp=False))
    assert main([]) == 1


def test_is_enabled_follows_settings(monkeypatch) -> None:
    """开关语义：只跟随 settings.enable_mcp，不受环境差异影响。"""
    from app.config import settings as app_settings

    monkeypatch.setattr("app.config.settings", dataclasses.replace(app_settings, enable_mcp=True))
    assert server._is_enabled() is True

    monkeypatch.setattr("app.config.settings", dataclasses.replace(app_settings, enable_mcp=False))
    assert server._is_enabled() is False


def test_write_message_forces_utf8_and_lf() -> None:
    """Windows 管道默认 GBK 且会把 \\n 翻成 \\r\\n；协议层必须强制 UTF-8 + LF，否则中文乱码 / 报文被 CR 污染。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936", newline=None)

    server._force_utf8(stream)
    server._write_message(stream, {"jsonrpc": "2.0", "id": 1, "result": {"text": "中文片段"}})

    data = raw.getvalue()
    assert b"\r" not in data
    assert data.endswith(b"\n")
    assert json.loads(data.decode("utf-8"))["result"]["text"] == "中文片段"


def test_serve_stops_cleanly_when_client_disconnects(kb) -> None:
    """客户端断开（BrokenPipe）只应干净退出，不应抛异常带崩进程。"""

    class _BrokenPipe:
        def write(self, _text: str) -> None:
            raise BrokenPipeError("管道已关闭")

        def flush(self) -> None:
            raise BrokenPipeError("管道已关闭")

    serve(stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), stdout=_BrokenPipe())


def test_serve_skips_undecodable_line_and_keeps_serving(kb) -> None:
    """非法字节不掀掉主循环：坏行 → -32700，后续请求照常处理。"""
    raw_in = io.TextIOWrapper(
        io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\xff\xfe not utf-8\n'),
        encoding="utf-8",
        errors="replace",  # 与正式 stdin 的处理一致
        newline="\n",
    )
    out = io.StringIO()

    serve(stdin=raw_in, stdout=out)

    messages = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [m.get("id") for m in messages] == [1, None]
    assert messages[0]["result"] == {}
    assert messages[1]["error"]["code"] == PARSE_ERROR


# ---------------- 惰性构建 ----------------


def test_get_container_builds_container_lazily_and_caches_it(settings, monkeypatch) -> None:
    """真正走一遍惰性构建路径：Database 换内存桩，其余组件由离线环境变量决定。"""
    from app import container as container_module

    class _OfflineDatabase(_NullDatabase):
        pass

    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr(container_module, "Database", _OfflineDatabase)
    server.set_container(None)

    try:
        built = server.get_container()
        assert built.settings is settings
        assert type(built.embedder).__name__ == "HashEmbedding"
        assert type(built.vector_store).__name__ == "InMemoryVectorStore"
        assert type(built.llm).__name__ == "FakeLLMClient"
        # 第二次调用复用缓存，不重复构建
        assert server.get_container() is built
    finally:
        server.set_container(None)


def test_protocol_layer_is_lazy_about_app_container(kb, monkeypatch) -> None:
    """屏蔽 app.container 后：initialize / tools/list 照常可用，
    只有真正执行工具才会去 import 容器，且失败被兜成 -32603 而不是崩进程。"""
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "app.container":
            raise ImportError("测试用：屏蔽 app.container 以验证惰性构建")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(server, "_container", None)
    monkeypatch.setattr(builtins, "__import__", blocked_import)

    assert handle_message(_request("initialize"))["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert handle_message(_request("tools/list"))["result"]["tools"]

    resp = _call("knowledge_search", {"query": "考勤"})
    assert resp["error"]["code"] == INTERNAL_ERROR
    assert resp["isError"] is True
