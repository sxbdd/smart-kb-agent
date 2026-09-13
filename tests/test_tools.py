"""工具安全：calculator 必须是 AST 白名单，不能变成任意代码执行。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.tools import build_tools, safe_calculate


@pytest.mark.parametrize(
    "expr,expected",
    [("100 * 1.08", 108.0), ("1 + 2 * 3", 7), ("(1+2)*3", 9), ("2 ** 3", 8), ("-5 + 2", -3), ("10 / 4", 2.5)],
)
def test_calculator_allows_arithmetic(expr, expected):
    assert safe_calculate(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "(1).__class__.__bases__[0].__subclasses__()",
        "lambda: 1",
        "[x for x in range(3)]",
        "'a' * 3",
        "1 if True else 2",
        "exec('x=1')",
    ],
)
def test_calculator_rejects_everything_else(expr):
    with pytest.raises(Exception):
        safe_calculate(expr)


def test_tools_registry_exposes_exactly_two_tools(services):
    tools = {t.name: t for t in build_tools(services["rag"])}
    assert set(tools) == {"knowledge_search", "calculator"}


def test_knowledge_search_on_empty_store(services):
    tools = {t.name: t for t in build_tools(services["rag"])}
    assert tools["knowledge_search"].execute(query="任何问题") == "（未检索到相关内容）"


def test_knowledge_search_returns_indexed_chunk_with_filename(services):
    docs_dir = Path(services["settings"].documents_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "kb.txt"
    path.write_text("员工考勤制度：出差住宿标准：一线城市每晚不超过 600 元。", encoding="utf-8")
    services["ingestion"].ingest(str(path), "考勤制度.txt")

    tools = {t.name: t for t in build_tools(services["rag"])}
    out = tools["knowledge_search"].execute(query="出差住宿标准")

    assert "考勤制度.txt" in out
    assert "600" in out
    assert out.startswith("- ")


def test_knowledge_search_has_no_score_threshold(services):
    """记录一个已知行为：检索只做 top-k，不做相似度截断。

    因此单文档知识库里任何查询都会命中该文档 —— 拒答完全依赖 Prompt 约束
    （见 docs/review-v1-audit.md 的"可选优化"一节）。若将来引入 MIN_SCORE 阈值，
    本用例需要同步更新。
    """
    docs_dir = Path(services["settings"].documents_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "only.txt"
    path.write_text("员工考勤制度：上班时间 9:00-18:00。", encoding="utf-8")
    services["ingestion"].ingest(str(path), "唯一制度.txt")

    tools = {t.name: t for t in build_tools(services["rag"])}
    out = tools["knowledge_search"].execute(query="完全不相关的外星语 zzzqqq")

    assert "唯一制度.txt" in out, "当前实现无阈值，不相关查询仍会命中"
