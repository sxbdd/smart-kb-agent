"""Router 意图分类：规则优先 + LLM 分类可选 + RAG 兜底。"""
from __future__ import annotations

import pytest

from app.services.router import Router, is_calculation


@pytest.mark.parametrize(
    "question,expected",
    [
        ("你好", "chat"),
        ("hello!", "chat"),
        ("谢谢", "chat"),
        ("100 * 1.08", "agent"),
        ("(3+5)*2", "agent"),
        ("出差住宿标准是多少？", "rag"),
        ("年假有几天", "rag"),
        ("报销流程是什么", "rag"),
        ("今天天气怎么样", "rag"),      # 规则判不出 → 兜底 RAG
        ("", "rag"),                    # 空问题 → 兜底 RAG
    ],
)
def test_rule_based_routing(question, expected):
    assert Router().route(question) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [("100 * 1.08", True), ("1+1", True), (" 2 ^ 3 ", True), ("100元怎么算", False), ("", False)],
)
def test_is_calculation(expr, expected):
    assert is_calculation(expr) is expected


def test_llm_classify_disabled_by_default():
    """默认不启用 LLM 分类：不应产生任何 LLM 调用。"""

    class BoomLLM:
        def chat(self, *a, **k):  # pragma: no cover - 被调用即失败
            raise AssertionError("默认不应调用 LLM 分类")

    assert Router(llm=BoomLLM(), enable_llm=False).route("随便问问") == "rag"


def test_llm_classify_enabled_uses_llm():
    class StubLLM:
        def __init__(self, answer):
            self.answer = answer
            self.calls = 0

        def chat(self, messages, max_tokens=None):
            self.calls += 1
            return self.answer

    llm = StubLLM("chat")
    assert Router(llm=llm, enable_llm=True).route("随便问问") == "chat"
    assert llm.calls == 1


def test_llm_classify_failure_falls_back_to_rag():
    class BrokenLLM:
        def chat(self, *a, **k):
            raise RuntimeError("网络炸了")

    # 分类失败必须回退 RAG，而不是抛出去影响可用性
    assert Router(llm=BrokenLLM(), enable_llm=True).route("随便问问") == "rag"


def test_llm_classify_unexpected_answer_falls_back_to_rag():
    class StubLLM:
        def chat(self, messages, max_tokens=None):
            return "我不知道"

    assert Router(llm=StubLLM(), enable_llm=True).route("随便问问") == "rag"
