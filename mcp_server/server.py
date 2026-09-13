"""MCP（Model Context Protocol）server：把本项目的知识库能力暴露给 MCP 客户端。

设计要点：

- **传输**：stdio；**协议**：JSON-RPC 2.0，一条消息一行（`\\n` 分隔）——这就是 MCP 的
  stdio 传输形态。
- **协议处理是纯函数**：`handle_message(msg)`，stdio 主循环只做
  「读一行 → 调用它 → 写回结果（通知不写）」，因此测试可以在**进程内**直接断言
  协议行为，无需起子进程。
- **容器惰性构建**：只有真正要检索 / 问答时才 `import app.container` 并组装，
  `import mcp_server` 不会加载 bge 模型、也不会连 MySQL。
- **永不崩进程**：任何异常都被转成 JSON-RPC 错误响应，坏报文只影响当前一行。
- stdout 归协议专用，日志一律走 stderr（混入日志会破坏 JSON-RPC 报文）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp_server")

#: MCP 协议版本（与 Claude Desktop / Cursor 当前支持的版本对齐）
PROTOCOL_VERSION = "2024-11-05"
#: 本 server 的版本，随 V2 发布
SERVER_VERSION = "2.0.0"
#: settings.mcp_server_name 取不到时的回退名
DEFAULT_SERVER_NAME = "smart-kb-agent"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: 检索片段数边界（与主服务 MAX_TOP_K 的默认值保持一致）
MIN_TOP_K = 1
MAX_TOP_K = 20

#: 工具清单。inputSchema 用规范的 JSON Schema 描述，客户端据此渲染参数表单 / 做校验。
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "knowledge_search",
        "description": "在企业知识库中做向量检索，返回相关片段（含来源文档名与相似度）。只检索、不生成回答。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "检索关键词或自然语言问题",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": MIN_TOP_K,
                    "maximum": MAX_TOP_K,
                    "description": f"返回片段数，默认取主服务 TOP_K 配置，范围 {MIN_TOP_K}-{MAX_TOP_K}",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_knowledge_base",
        "description": "基于企业知识库检索并由 LLM 生成回答，返回回答正文与引用来源。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要提问的问题（自然语言）",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]


# ---------------- 错误 / 响应构造 ----------------


def _error_response(msg_id: Any, code: int, message: str, is_error: bool = False) -> Dict[str, Any]:
    """构造 JSON-RPC 错误响应。

    `is_error=True` 时额外在**顶层**带一个 `isError: true`：MCP 的工具失败语义是
    result 里的 `isError`，而 JSON-RPC 的错误语义是 `error`。这里两者同时给出，
    让只想看 `isError` 的客户端也不会把内部异常当成成功结果。
    """
    response: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    if is_error:
        response["isError"] = True
    return response


def _result_response(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# ---------------- 容器（惰性构建） ----------------

#: 依赖容器缓存。测试可用 set_container() 注入离线容器。
_container: Any = None


def set_container(container: Any) -> None:
    """注入 / 清空容器缓存（测试 seam，也便于宿主进程复用已建好的容器）。"""
    global _container
    _container = container


def get_container() -> Any:
    """惰性构建依赖容器：首次真正需要检索 / 问答时才 import app.container。

    这样 `import mcp_server` 只是加载协议层，不会加载 bge 模型、不会连 MySQL，
    也不会因为缺配置而在 import 阶段就炸掉。
    """
    global _container
    if _container is None:
        from app.config import settings  # 延迟导入：避免 import 期就读配置 / 建连接
        from app.container import build_container

        _container = build_container(settings)
    return _container


def _server_name() -> str:
    """读取 settings.mcp_server_name；配置不可用时退回环境变量 / 默认名。"""
    try:
        from app.config import settings

        return settings.mcp_server_name or DEFAULT_SERVER_NAME
    except Exception:  # pragma: no cover - 配置缺失时仍要让协议层可用
        return os.getenv("MCP_SERVER_NAME", DEFAULT_SERVER_NAME)


def _is_enabled() -> bool:
    """读取 settings.enable_mcp（`ENABLE_MCP=false` 可整体关掉 MCP server）。

    配置读不到时按「开启」处理：协议层不该因为配置文件问题变成哑巴。
    """
    try:
        from app.config import settings

        return bool(settings.enable_mcp)
    except Exception:  # pragma: no cover - 配置缺失时保持可用
        return True


# ---------------- 参数校验 ----------------


class _InvalidParams(Exception):
    """参数不合法 → JSON-RPC -32602。"""


class _MethodNotFound(Exception):
    """未知方法 → JSON-RPC -32601。"""


def _require_non_empty_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _InvalidParams(f"参数 '{key}' 必须是非空字符串")
    return value.strip()


def _optional_top_k(args: Dict[str, Any]) -> Optional[int]:
    """top_k 可选：缺省 / null 表示用主服务默认值。"""
    if args.get("top_k") is None:
        return None
    value = args["top_k"]
    # bool 是 int 的子类，必须显式排除，否则 top_k=true 会被当成 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidParams("参数 'top_k' 必须是整数")
    if not MIN_TOP_K <= value <= MAX_TOP_K:
        raise _InvalidParams(f"参数 'top_k' 必须在 {MIN_TOP_K}-{MAX_TOP_K} 之间")
    return value


# ---------------- 工具实现 ----------------


def _tool_knowledge_search(args: Dict[str, Any]) -> str:
    """检索知识库，把片段（含来源文档名）拼成可读文本。"""
    query = _require_non_empty_str(args, "query")
    top_k = _optional_top_k(args)

    results = get_container().rag.search(query, top_k)
    if not results:
        return f"未在知识库中检索到与“{query}”相关的片段（知识库可能为空或没有匹配内容）。"

    lines: List[str] = [f"共检索到 {len(results)} 个相关片段：", ""]
    for index, item in enumerate(results, 1):
        name = item.metadata.get("document_name") or "未知文档"
        lines.append(f"[{index}] 来源：{name}（相似度 {float(item.score):.4f}）")
        lines.append((item.document or "").strip())
        lines.append("")
    return "\n".join(lines).strip()


def _tool_ask_knowledge_base(args: Dict[str, Any]) -> str:
    """检索 + LLM 生成回答，并把引用来源附在回答后面。"""
    question = _require_non_empty_str(args, "question")

    answer, sources = get_container().rag.answer(question)
    if not sources:
        return answer

    lines: List[str] = [answer, "", "引用来源："]
    for index, source in enumerate(sources, 1):
        name = source.document_name or "未知文档"
        snippet = (source.content or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        lines.append(f"[{index}] {name}（相似度 {float(source.score):.4f}）：{snippet}")
    return "\n".join(lines)


#: 工具名 → 执行函数。新增工具时同时往 TOOLS 和这里加。
_TOOL_HANDLERS: Dict[str, Any] = {
    "knowledge_search": _tool_knowledge_search,
    "ask_knowledge_base": _tool_ask_knowledge_base,
}


def _call_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    """执行 tools/call：校验参数 → 调工具 → 返回 MCP 规定的 content 结构。"""
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _InvalidParams("tools/call 缺少参数 'name'（工具名）")

    arguments = params.get("arguments")
    if arguments is None:  # 客户端可以省略 arguments，等价于 {}
        arguments = {}
    if not isinstance(arguments, dict):
        raise _InvalidParams("参数 'arguments' 必须是 JSON 对象")

    handler: Any = _TOOL_HANDLERS.get(name)
    if handler is None:
        available = "、".join(_TOOL_HANDLERS)
        raise _InvalidParams(f"未知工具：{name}（可用：{available}）")

    text = handler(arguments)
    return {"content": [{"type": "text", "text": text}], "isError": False}


# ---------------- 协议分发 ----------------


def _dispatch(method: str, params: Dict[str, Any]) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _server_name(), "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return _call_tool(params)
    raise _MethodNotFound(method)


def handle_message(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息，返回响应 dict；**通知返回 None**（不产生响应）。

    纯函数：不读 stdin / 不写 stdout，只依赖 `msg` 与（惰性读取的）配置与容器，
    因此用例可以在进程内直接断言协议行为。
    """
    if not isinstance(msg, dict):
        return _error_response(None, INVALID_REQUEST, "请求必须是 JSON 对象")

    msg_id = msg.get("id")
    # 通知（notification）= 没有 id 成员的消息。MCP 的 notifications/initialized 走这条路。
    is_notification = "id" not in msg

    if msg.get("jsonrpc") != "2.0":
        if is_notification:
            return None
        return _error_response(msg_id, INVALID_REQUEST, '不支持的 jsonrpc 版本（要求 "2.0"）')

    method = msg.get("method")
    if not isinstance(method, str) or not method:
        if is_notification:
            return None
        return _error_response(msg_id, INVALID_REQUEST, "缺少 method 字段")

    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error_response(msg_id, INVALID_PARAMS, "params 必须是 JSON 对象")

    if is_notification:
        # 通知一律不回应（包含未知通知，遵循 MCP 约定）
        return None

    try:
        result = _dispatch(method, params)
    except _MethodNotFound:
        return _error_response(msg_id, METHOD_NOT_FOUND, f"未知方法：{method}")
    except _InvalidParams as exc:
        return _error_response(msg_id, INVALID_PARAMS, str(exc))
    except Exception as exc:  # 兜底：任何内部异常都不许把进程带走
        logger.exception("处理 %s 时发生内部异常", method)
        return _error_response(msg_id, INTERNAL_ERROR, f"内部错误：{exc}", is_error=True)

    return _result_response(msg_id, result)


# ---------------- stdio 主循环 ----------------


def _write_message(stream: Any, message: Dict[str, Any]) -> None:
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


def _force_utf8(stream: Any, errors: str = "strict") -> Any:
    """把标准流切成 UTF-8 + 不做换行翻译。

    MCP 规定 stdio 上的 JSON-RPC 报文是 UTF-8；而 Windows 上管道默认编码是
    locale（cp936/GBK），中文答案到了客户端会变成乱码。另外 TextIOWrapper 默认
    会把 `\\n` 翻成 `\\r\\n`，强制 newline="\\n" 保证「一行一条消息」干净。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors=errors, newline="\n", line_buffering=True)
        except Exception:  # pragma: no cover - 流不支持 reconfigure 时保持原样
            pass
    return stream


def serve(stdin: Any = None, stdout: Any = None) -> None:
    """stdio 主循环：一行一条消息，读一行 → handle_message → 写回一行（通知不写）。"""
    # 输入用 errors="replace"：客户端发来非法字节时，坏行退化成 JSON 解析错误（-32700），
    # 而不是让解码异常掀掉整个主循环。
    source = _force_utf8(sys.stdin, errors="replace") if stdin is None else stdin
    sink = _force_utf8(sys.stdout) if stdout is None else stdout

    for raw in source:
        line = raw.strip()
        if not line:  # 跳过空行（客户端偶尔会补换行）
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _write_message(sink, _error_response(None, PARSE_ERROR, f"JSON 解析失败：{exc.msg}"))
            continue

        try:
            response = handle_message(msg)
        except Exception as exc:  # pragma: no cover - handle_message 内部已兜底
            logger.exception("handle_message 意外抛出")
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            response = _error_response(msg_id, INTERNAL_ERROR, f"内部错误：{exc}", is_error=True)

        if response is not None:
            try:
                _write_message(sink, response)
            except OSError as exc:
                # 客户端断开（BrokenPipe / 管道已关闭）：干净退出，不打印堆栈
                logger.warning("写出响应失败，连接可能已断开：%s", exc)
                return


def _setup_logging() -> None:
    """日志只写 stderr：stdout 是协议通道，混入日志会破坏 JSON-RPC 报文。"""
    handler = logging.StreamHandler(_force_utf8(sys.stderr))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：`python -m mcp_server`（默认跑 stdio 循环）/ `--list-tools`（打印工具清单）。"""
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server",
        description="smart-kb-agent 的 MCP server：stdio + JSON-RPC 2.0，暴露知识库检索与问答工具。",
    )
    parser.add_argument("--list-tools", action="store_true", help="打印可用工具清单（JSON）后退出")
    args = parser.parse_args(argv)

    _setup_logging()

    if args.list_tools:
        json.dump({"tools": TOOLS}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0

    if not _is_enabled():
        logger.error("ENABLE_MCP=false，MCP server 已被配置禁用（改 .env 里的 ENABLE_MCP 以启用）")
        return 1

    logger.info("MCP server 启动（stdio / JSON-RPC 2.0），工具：%s", [t["name"] for t in TOOLS])
    serve()
    return 0
