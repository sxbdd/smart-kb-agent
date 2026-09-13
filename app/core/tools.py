"""Agent 工具：知识库检索 + 安全计算器。"""
from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

# 租户归一规则集中定义在 tenancy（横切值对象，不依赖任何业务模块），
# 这里复用而不是自己写字符串处理：规则只应有一份，否则迟早不一致。
from app.services.tenancy import normalize_tenant


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable

    def execute(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_calculate(expression: str) -> float:
    """只允许数字与四则/幂运算，禁止任意代码执行。"""

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](_eval(node.operand))
        raise ValueError("不支持的表达式")

    tree = ast.parse(expression, mode="eval")
    return _eval(tree)


def build_tools(rag_service, tenant_id: Optional[str] = None) -> List[Tool]:
    """构造工具集合；``tenant_id`` 非 None 时把租户**绑定进闭包**。

    为什么按调用方传参绑定，而不是在容器里绑一次：容器只在进程启动时
    ``build_tools(rag)`` 一次，而租户是**每个请求**才知道的。若把租户固化在容器级
    工具里，所有租户就会共享同一份检索结果 —— 多租户隔离在 Agent 路径上直接破防。
    所以这里返回的是"本次请求专用"的工具集合，租户通过闭包捕获。

    为什么必须归一：``tenant_id=None``（系统路径，不过滤）与 ``"default"``（真实租户）
    语义完全不同，不能混同。空串/纯空白若不归一，可能被下层当成"无过滤"，因此统一走
    ``normalize_tenant``，保证真正进入检索的租户标识永远非空。
    """
    # 归一化只做一次；后面闭包捕获的是规范值，不会出现"工具链这头归一、那头没归一"的分歧
    bound_tenant = None if tenant_id is None else normalize_tenant(tenant_id)

    def search(query: str) -> str:
        # None 分支刻意保持与 V1 完全一致的调用形状（不传 tenant_id 这个关键字），
        # 既保住向后兼容（旧 fake / 旧调用方不接受该参数），也避免把"无租户"误当成某个租户。
        if bound_tenant is None:
            results = rag_service.search(query, top_k=3)
        else:
            results = rag_service.search(query, top_k=3, tenant_id=bound_tenant)
        if not results:
            return "（未检索到相关内容）"
        lines = []
        for r in results:
            name = r.metadata.get("document_name", "")
            lines.append(f"- {name}: {r.document[:200]}")
        return "\n".join(lines)

    return [
        Tool(
            name="knowledge_search",
            description="在知识库中搜索与查询相关的文档片段，返回带来源的原文片段",
            parameters={"query": {"type": "string", "description": "搜索查询"}},
            func=search,
        ),
        Tool(
            name="calculator",
            description="执行数学计算（仅支持数字与四则/幂运算）",
            parameters={"expression": {"type": "string", "description": "数学表达式，如 '100 * 1.08'"}},
            func=safe_calculate,
        ),
    ]