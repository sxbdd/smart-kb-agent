"""RAG 问答服务：检索 → 重排 → 组装 Prompt → LLM 生成。"""
from __future__ import annotations

from typing import List, Optional

from app.core.prompt_templates import build_prompt
from app.models.schemas import Source


class RAGService:
    def __init__(self, embedder, vector_store, llm, reranker, top_k: int, rerank_top_k: int, max_history_messages: int) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.llm = llm
        self.reranker = reranker
        self.top_k = top_k
        self.rerank_top_k = rerank_top_k
        self.max_history_messages = max_history_messages

    def search(self, query: str, top_k: Optional[int] = None):
        embedding = self.embedder.encode([query])[0]
        return self.vector_store.query(embedding, top_k or self.top_k)

    def answer(self, question: str, top_k: Optional[int] = None, history: Optional[List[dict]] = None) -> tuple[str, List[Source]]:
        results = self.search(question, top_k)
        results = self.reranker.rerank(question, results, self.rerank_top_k)

        context = "\n\n---\n\n".join([r.document for r in results]) or "（无相关文档片段）"
        prompt = build_prompt(question, context, history, self.max_history_messages)
        answer = self.llm.chat([{"role": "user", "content": prompt}])

        sources = [
            Source(
                document_id=r.metadata.get("document_id", ""),
                document_name=r.metadata.get("document_name", ""),
                chunk_id=r.id,
                content=r.document,
                score=round(r.score, 4),
            )
            for r in results
        ]
        return answer, sources