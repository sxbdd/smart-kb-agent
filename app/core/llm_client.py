"""LLM 客户端：OpenAI 兼容接口（默认） / Fake（测试回退）。"""
from __future__ import annotations

import time
from typing import List, Protocol

from app.utils.exceptions import LLMError


class LLMClient(Protocol):
    def chat(self, messages: List[dict], max_tokens: int | None = None) -> str: ...


class OpenAICompatClient:
    def __init__(self, api_base: str, api_key: str, model: str, timeout: float = 60.0, max_tokens: int = 1024, max_retries: int = 3) -> None:
        import httpx

        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.httpx = httpx

    def chat(self, messages: List[dict], max_tokens: int | None = None) -> str:
        url = f"{self.api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "messages": self._truncate_messages(messages),
            "temperature": 0.2,
            "max_tokens": max_tokens or self.max_tokens,
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            except Exception as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                last_err = LLMError(f"LLM API 错误 {resp.status_code}：{resp.text[:500]}")
                time.sleep(1.0 * (attempt + 1))
                continue
            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
                continue
            if content and content.strip():
                return content
            last_err = LLMError("LLM 返回空内容")
            time.sleep(1.0 * (attempt + 1))

        raise LLMError(f"LLM 调用失败：{last_err}") from last_err

    @staticmethod
    def _truncate_messages(messages: List[dict], max_chars: int = 12000) -> List[dict]:
        """粗略控制输入长度，避免超出模型 token 上限。"""
        if not messages:
            return messages

        out: List[dict] = []
        budget = max_chars
        for m in messages:
            if m.get("role") == "system":
                out.append(m)
                budget -= len(m.get("content", ""))

        tail: List[dict] = []
        rest = [m for m in messages if m.get("role") != "system"]
        for m in reversed(rest):
            c = m.get("content", "")
            if budget - len(c) >= 0:
                tail.append(m)
                budget -= len(c)
            else:
                m2 = dict(m)
                m2["content"] = c[: max(budget, 0)]
                tail.append(m2)
                break
        tail.reverse()
        return out + tail


class FakeLLMClient:
    """确定性回答，仅用于开发/测试，无外网依赖。"""

    def chat(self, messages: List[dict], max_tokens: int | None = None) -> str:
        text = "\n".join(str(m.get("content", "")) for m in messages)
        question = ""
        if "用户问题：" in text:
            question = text.split("用户问题：", 1)[-1].split("\n", 1)[0].strip()

        if "文档内容开始" in text:
            ctx = text.split("文档内容开始", 1)[-1].split("文档内容结束", 1)[0].strip()
            snippet = ctx[:120].replace("\n", " ")
            return f"（测试回答）根据资料「{snippet}…」，对“{question}”的确定性回答。"

        user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        return f"（测试回答）已收到问题：“{user}”。"


def get_llm_client(settings) -> LLMClient:
    provider = settings.llm_provider.strip().lower()
    if provider == "fake":
        return FakeLLMClient()
    if provider == "openai":
        return OpenAICompatClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            max_tokens=settings.llm_max_tokens,
        )
    raise ValueError(f"未知 LLM_PROVIDER：{provider}")