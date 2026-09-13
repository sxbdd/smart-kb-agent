"""Agent 工具：知识库检索 + 安全计算器。"""
from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable, List


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


def build_tools(rag_service) -> List[Tool]:
    def search(query: str) -> str:
        results = rag_service.search(query, top_k=3)
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