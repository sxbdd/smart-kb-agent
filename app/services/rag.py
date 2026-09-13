"""RAG 问答服务：检索 → 重排 → 组装 Prompt → LLM 生成。"""
from __future__ import annotations

from typing import Iterator, List, Optional

from app.core.prompt_templates import build_prompt
from app.models.schemas import Source


class RAGService:
    def __init__(self, embedder, vector_store, llm, reranker, top_k: int, rerank_top_k: int,
                 max_history_messages: int, min_score: float = 0.0) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.llm = llm
        self.reranker = reranker
        self.top_k = top_k
        self.rerank_top_k = rerank_top_k
        self.max_history_messages = max_history_messages
        #: 相似度阈值，0.0 表示不过滤（与 V1 行为完全一致）
        self.min_score = min_score

    def search(self, query: str, top_k: Optional[int] = None, tenant_id: Optional[str] = None):
        """检索相关片段。

        `tenant_id` 是**末尾可选参数**，默认 `None` = 不过滤：
        既保持 V1 的返回形状与既有单测不变，也给服务层留出显式传租户的位置。
        注意 `"default"` 是**真实租户**（不是 None），必须原样透传下去 ——
        否则默认租户的用户会查到全部租户的数据。
        """
        embedding = self.embedder.encode([query])[0]
        return self.vector_store.query(
            embedding, top_k or self.top_k, min_score=self.min_score, tenant_id=tenant_id
        )

    def _prepare(self, question: str, top_k: Optional[int] = None,
                 history: Optional[List[dict]] = None,
                 tenant_id: Optional[str] = None) -> tuple[List[Source], str]:
        """检索 + 重排 + 组装 Prompt（`answer()` 与 `stream_answer()` 共用）。

        返回 `(sources, prompt)`；`stream_answer()` 在拿到 prompt 后不立即调用 LLM，
        因此检索异常发生时"还没开始流式输出"，接口层可以在 meta 之后直接发 error 事件。

        `tenant_id` 只影响检索（透传给 `search`）；重排与 Prompt 组装与租户无关。
        """
        results = self.search(question, top_k, tenant_id=tenant_id)
        results = self.reranker.rerank(question, results, self.rerank_top_k)

        context = "\n\n---\n\n".join([r.document for r in results]) or "（无相关文档片段）"
        prompt = build_prompt(question, context, history, self.max_history_messages)

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
        return sources, prompt

    def answer(self, question: str, top_k: Optional[int] = None, history: Optional[List[dict]] = None,
               tenant_id: Optional[str] = None) -> tuple[str, List[Source]]:
        """非流式问答；`tenant_id` 只作用于检索，生成侧与租户无关。"""
        sources, prompt = self._prepare(question, top_k, history, tenant_id=tenant_id)
        answer = self.llm.chat([{"role": "user", "content": prompt}])
        return answer, sources

    def stream_answer(self, question: str, top_k: Optional[int] = None,
                      history: Optional[List[dict]] = None,
                      tenant_id: Optional[str] = None) -> tuple[List[Source], Iterator[str]]:
        """流式 RAG：与 `answer()` 共用检索与 Prompt 组装，只把"生成"换成流式。

        返回 `(sources, 增量迭代器)`。**注意**：检索等准备工作在调用时同步完成，
        真正的生成发生在消费迭代器的时候。
        """
        sources, prompt = self._prepare(question, top_k, history, tenant_id=tenant_id)
        return sources, self.llm.chat_stream([{"role": "user", "content": prompt}])
