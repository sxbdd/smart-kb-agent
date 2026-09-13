"""Embedding 提供者：OpenAI 兼容 API（默认）/ sentence-transformers / Hash（开发测试回退）。"""
from __future__ import annotations

import hashlib
import math
import re
from typing import List, Protocol

from app.core.hf_cache import prefer_offline_if_cached

_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


class EmbeddingProvider(Protocol):
    def encode(self, texts: List[str]) -> List[List[float]]: ...


def _tokenize(text: str) -> List[str]:
    tokens = _WORD.findall(text.lower())
    cjk = _CJK.findall(text)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return tokens


class HashEmbedding:
    """确定性哈希向量，仅用于开发/测试，保证无外网、无模型也能跑通链路。"""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def encode(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for tok in _tokenize(text):
            digest = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = digest % self.dim
            sign = 1.0 if (digest >> self.dim) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class SentenceTransformerEmbedding:
    def __init__(self, model_name: str, offline_auto: bool = True) -> None:
        if offline_auto:
            # 必须在 import sentence_transformers 之前设置，否则无效
            prefer_offline_if_cached(model_name)
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)

    def encode(self, texts: List[str]) -> List[List[float]]:
        embeddings = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()


class OpenAIEmbedding:
    """OpenAI 兼容 /embeddings 接口（可指向硅基流动、智谱、OpenAI 等）。"""

    def __init__(self, api_base: str, api_key: str, model: str) -> None:
        import httpx

        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.httpx = httpx

    def encode(self, texts: List[str]) -> List[List[float]]:
        url = f"{self.api_base}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = self.httpx.post(url, headers=headers, json={"model": self.model, "input": texts}, timeout=60)
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def get_embedding_provider(settings) -> EmbeddingProvider:
    provider = settings.embedding_provider.strip().lower()
    if provider == "hash":
        return HashEmbedding(settings.hash_embedding_dim)
    if provider == "sentence-transformers":
        return SentenceTransformerEmbedding(
            settings.embedding_model,
            offline_auto=getattr(settings, "hf_offline_auto", True),
        )
    if provider == "openai":
        api_key = settings.embedding_api_key or settings.llm_api_key
        return OpenAIEmbedding(settings.embedding_api_base, api_key, settings.embedding_model)
    raise ValueError(f"未知 EMBEDDING_PROVIDER：{provider}")
