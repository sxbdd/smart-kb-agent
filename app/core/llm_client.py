"""LLM 客户端：OpenAI 兼容接口（默认） / Fake（测试回退）。"""
from __future__ import annotations

import json
import time
from typing import Iterator, List, Protocol

from app.utils.exceptions import LLMError


class LLMClient(Protocol):
    def chat(self, messages: List[dict], max_tokens: int | None = None) -> str: ...

    def chat_stream(self, messages: List[dict], max_tokens: int | None = None) -> Iterator[str]: ...


class OpenAICompatClient:
    #: 空内容且 finish_reason=length 时的预算上限（推理模型的思考会吃掉 max_tokens）
    MAX_TOKEN_CEILING = 8192

    def __init__(self, api_base: str, api_key: str, model: str, timeout: float = 60.0, max_tokens: int = 4096, max_retries: int = 3) -> None:
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
        budget = max_tokens or self.max_tokens

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            payload = {
                "model": self.model,
                "messages": self._truncate_messages(messages),
                "temperature": 0.2,
                "max_tokens": budget,
            }
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
                choice = data["choices"][0]
                message = choice["message"]
                content = message.get("content")
                finish_reason = choice.get("finish_reason")
                reasoning = message.get("reasoning_content") or ""
            except (KeyError, IndexError, TypeError) as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
                continue

            if content and content.strip():
                return content

            # 空内容分两种情况，必须区分开（否则会把"预算不足"误判成"模型抽风"）：
            #  - finish_reason == "length"：max_tokens 被 reasoning 吃光，翻倍重试
            #  - 其它：真正的空返回，按原样重试
            if finish_reason == "length" and budget < self.MAX_TOKEN_CEILING:
                last_err = LLMError(
                    f"LLM 因 max_tokens={budget} 不足被截断（reasoning 输出 {len(reasoning)} 字符），"
                    "已自动提高预算重试"
                )
                budget = min(budget * 2, self.MAX_TOKEN_CEILING)
                time.sleep(1.0 * (attempt + 1))
                continue

            last_err = LLMError(
                f"LLM 返回空内容（finish_reason={finish_reason}, max_tokens={budget}, "
                f"reasoning={len(reasoning)} 字符）"
            )
            time.sleep(1.0 * (attempt + 1))

        raise LLMError(f"LLM 调用失败：{last_err}") from last_err

    def chat_stream(self, messages: List[dict], max_tokens: int | None = None) -> Iterator[str]:
        """流式对话：逐帧产出增量文本。

        与 `chat()` 同源的健壮性（两者互不影响，`chat()` 的行为未做任何改动）：

        - 网络异常 / 非 200 / 空内容：退避重试；
        - `finish_reason == "length"` 且**一个字都没产出**：说明预算被推理模型的思考吃光，
          翻倍 `max_tokens` 重试（上限 `MAX_TOKEN_CEILING`）；
        - **已经开始产出后又失败**：不静默吞掉，抛 `LLMError`（外层可据此发 `error` 事件）。

        实现上先把整条流读完再开始 yield，因此对调用方是**全有或全无**的：
        要么拿到完整答案，要么拿到 `LLMError`，不会出现"半截答案 + 异常"。
        """
        url = f"{self.api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        budget = max_tokens or self.max_tokens
        # 走模块变量而非直接引用 time.sleep，便于测试 monkeypatch 掉真实退避等待
        sleep = time.sleep

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            payload = {
                "model": self.model,
                "messages": self._truncate_messages(messages),
                "temperature": 0.2,
                "max_tokens": budget,
                "stream": True,
            }
            collected: List[str] = []
            reasoning_chars = 0
            finish_reason = None
            failed_with_status: int | None = None
            broken: Exception | None = None

            try:
                resp = self.httpx.post(url, headers=headers, json=payload, timeout=self.timeout, stream=True)
            except Exception as exc:
                last_err = exc
                sleep(1.0 * (attempt + 1))
                continue

            try:
                if resp.status_code != 200:
                    failed_with_status = resp.status_code
                    last_err = LLMError(f"LLM API 错误 {resp.status_code}：{resp.text[:500]}")
                else:
                    done_seen = False
                    for event in self._iter_sse_events(resp):
                        if event == "[DONE]":
                            done_seen = True
                            break
                        try:
                            chunk = json.loads(event)
                            choice = chunk["choices"][0]
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            # 心跳 / 非标准帧：跳过，不影响整条流
                            continue
                        delta = choice.get("delta") or {}
                        reasoning_chars += len(delta.get("reasoning_content") or "")
                        piece = delta.get("content")
                        if piece:
                            collected.append(piece)
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    if not done_seen:
                        # 连接提前结束：没收到 [DONE] 也没有 finish_reason，等价于"流被截断"
                        broken = LLMError("上游连接在 [DONE] 之前关闭")
            except Exception as exc:
                # 流中途失败：此时可能已产出内容，绝不能当成"空返回"重试，更不能不报错
                broken = exc
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()

            if broken is not None:
                if collected:
                    raise LLMError(
                        f"LLM 流式中断（{type(broken).__name__}）：{broken}；已产出 {len(collected)} 个片段，"
                        "为避免返回残缺答案，此处直接报错"
                    ) from broken
                last_err = LLMError(f"LLM 流式中断（{type(broken).__name__}）：{broken}")
                sleep(1.0 * (attempt + 1))
                continue

            if failed_with_status is not None:
                sleep(1.0 * (attempt + 1))
                continue

            if collected:
                yield from collected
                return

            # 与 chat() 一致：只有"被 length 截断"才翻倍预算，且不超过上限
            if finish_reason == "length" and budget < self.MAX_TOKEN_CEILING and last_err is None:
                last_err = LLMError(
                    f"LLM 因 max_tokens={budget} 不足被截断（reasoning 输出 {reasoning_chars} 字符），"
                    "已自动提高预算重试"
                )
                budget = min(budget * 2, self.MAX_TOKEN_CEILING)
                sleep(1.0 * (attempt + 1))
                continue

            if last_err is None:
                last_err = LLMError(
                    f"LLM 流式返回空内容（finish_reason={finish_reason}, max_tokens={budget}, "
                    f"reasoning={reasoning_chars} 字符）"
                )
            sleep(1.0 * (attempt + 1))

        raise LLMError(f"LLM 调用失败：{last_err}") from last_err

    @staticmethod
    def _iter_sse_events(resp) -> Iterator[str]:
        """逐行读取 SSE，产出每个 `data:` 帧的内容（含终止帧 `[DONE]`）。"""
        lines = resp.iter_lines() if hasattr(resp, "iter_lines") else resp.text.splitlines()
        for line in lines:
            if not isinstance(line, str):
                line = line.decode("utf-8", errors="ignore")
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                yield line[5:].strip()

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

    #: chat_stream 的固定切片大小（字符数）
    STREAM_CHUNK_SIZE = 8

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

    def chat_stream(self, messages: List[dict], max_tokens: int | None = None) -> Iterator[str]:
        """把 `chat()` 的确定性回答按固定大小切片吐出。

        不变式（有专门用例守护）：多次切片拼接 == `chat()` 的返回值。
        """
        text = self.chat(messages, max_tokens)
        for i in range(0, len(text), self.STREAM_CHUNK_SIZE):
            yield text[i:i + self.STREAM_CHUNK_SIZE]


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