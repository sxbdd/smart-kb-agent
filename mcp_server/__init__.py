"""MCP server 包：把本项目的知识库能力暴露给支持 MCP 的客户端。

`import mcp_server` 只加载协议层（`server` 模块），**不会**加载 bge 模型、
不会连 MySQL —— 真正的依赖容器在第一次检索 / 问答时才惰性构建。

用法见 `mcp_server/README.md`；协议实现见 `mcp_server/server.py`。
"""
from __future__ import annotations

from mcp_server.server import (
    PROTOCOL_VERSION,
    SERVER_VERSION,
    TOOLS,
    get_container,
    handle_message,
    main,
    serve,
    set_container,
)

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_VERSION",
    "TOOLS",
    "get_container",
    "handle_message",
    "main",
    "serve",
    "set_container",
]
