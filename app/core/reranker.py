"""重排序：可选。默认 Noop，开启后加载 cross-encoder（模型名可配置）。"""
from __future__ import annotations

import logging
from typing import List, Protocol

from app.core.hf_cache import prefer_offline_if_cached
from app.core.vector_store import SearchResult


class Reranker(Protocol):
    def rerank(self, question: str, results: List[SearchResult], top_k: int) -> List[SearchResult]: ...


class NoopReranker:
    def rerank(self, question: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        return results[:top_k]


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", offline_auto: bool = True) -> None:
        if offline_auto:
            # 必须在 import sentence_transformers 之前设置
            prefer_offline_if_cached(model_name)
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)

    def rerank(self, question: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        pairs = [(question, r.document) for r in results]
        scores = self.model.predict(pairs)
        ordered = sorted(zip(results, scores), key=lambda x: x[1], reverse=True)
        return [r for r, _ in ordered[:top_k]]


def get_reranker(settings) -> Reranker:
    if settings.enable_rerank:
        try:
            return CrossEncoderReranker(
                settings.rerank_model,
                offline_auto=getattr(settings, "hf_offline_auto", True),
            )
        except Exception as exc:
            logging.getLogger("reranker").warning("Rerank 模型加载失败，回退 Noop：%s", exc)
            return NoopReranker()
    return NoopReranker()