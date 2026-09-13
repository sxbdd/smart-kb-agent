"""LLM 客户端：空返回、推理模型截断、重试策略。

守护的真实问题：推理类模型（如 deepseek-v4-flash）会先输出 reasoning_content 再输出 content。
`max_tokens` 给小了（例如 1024）时，预算全被思考吃掉，content 为空且 finish_reason=length ——
旧实现把它当成普通"空返回"重试同样预算，最终抛 LLMError，导致 RAG/对话/Agent 全线失败。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.llm_client import FakeLLMClient, OpenAICompatClient
from app.utils.exceptions import LLMError


class _Resp:
    def __init__(self, payload, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def _truncated(reasoning_chars: int = 500):
    return _Resp({"choices": [{
        "message": {"content": "", "reasoning_content": "思" * reasoning_chars},
        "finish_reason": "length",
    }]})


def _ok(content: str = "答案"):
    return _Resp({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """去掉重试退避，避免用例真的等 1~3 秒。"""
    monkeypatch.setattr("app.core.llm_client.time.sleep", lambda *_: None)


def _client(responses, max_tokens: int = 256, max_retries: int = 3):
    client = OpenAICompatClient("http://x/v1", "k", "m", max_tokens=max_tokens, max_retries=max_retries)
    budgets: list[int] = []

    def post(url, headers=None, json=None, timeout=None):
        budgets.append(json["max_tokens"])
        return responses.pop(0)

    client.httpx = SimpleNamespace(post=post)
    return client, budgets


def test_truncated_then_ok_retries_with_doubled_budget():
    client, budgets = _client([_truncated(), _ok("真答案")], max_tokens=256)
    assert client.chat([{"role": "user", "content": "hi"}]) == "真答案"
    assert budgets == [256, 512], "被 length 截断后应翻倍预算重试"


def test_doubling_is_capped_at_ceiling():
    client, budgets = _client([_truncated()] * 3, max_tokens=4096, max_retries=3)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    assert budgets == [4096, 8192, 8192], "预算不得超过 MAX_TOKEN_CEILING"


def test_truncation_error_message_is_actionable():
    client, _ = _client([_truncated()] * 3, max_tokens=8192, max_retries=3)
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "max_tokens" in str(exc.value) or "截断" in str(exc.value)


def test_empty_without_length_does_not_inflate_budget():
    empty = _Resp({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})
    client, budgets = _client([empty, empty, empty], max_tokens=256, max_retries=3)
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "空内容" in str(exc.value)
    assert budgets == [256, 256, 256], "真正的空返回不应无脑翻倍"


def test_api_error_retries_then_raises_with_status():
    bad = _Resp({}, status_code=500, text="boom")
    client, _ = _client([bad, bad, bad], max_retries=3)
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "500" in str(exc.value)


def test_malformed_payload_is_retried_not_crashed():
    client, _ = _client([_Resp({"nope": 1}), _ok("恢复")], max_retries=3)
    assert client.chat([{"role": "user", "content": "hi"}]) == "恢复"


def test_default_budget_is_generous_enough_for_reasoning_models():
    """默认 4096：给推理模型留出思考+作答的空间（历史值是 1024，会被思考吃光）。"""
    client = OpenAICompatClient("http://x/v1", "k", "m")
    assert client.max_tokens >= 4096


def test_fake_client_is_deterministic():
    out = FakeLLMClient().chat([{"role": "user", "content": "你好"}])
    assert "测试回答" in out
    assert "你好" in out
