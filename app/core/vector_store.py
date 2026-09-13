"""向量库：Chroma（默认）/ InMemory（开发测试回退）。"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, List, Protocol


@dataclass
class SearchResult:
    id: str
    document: str
    metadata: dict
    score: float


class VectorStore(Protocol):
    def add(self, id: str, embedding: List[float], metadata: dict, document: str) -> None: ...
    def query(self, embedding: List[float], top_k: int) -> List[SearchResult]: ...
    def delete_document(self, document_id: str) -> None: ...
    def reset(self) -> None: ...
    def count(self) -> int: ...


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._items: List[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, id: str, embedding: List[float], metadata: dict, document: str) -> None:
        with self._lock:
            self._items = [it for it in self._items if it["id"] != id]
            self._items.append({"id": id, "embedding": embedding, "metadata": metadata, "document": document})

    def query(self, embedding: List[float], top_k: int) -> List[SearchResult]:
        with self._lock:
            scored = []
            for it in self._items:
                score = _cosine(embedding, it["embedding"])
                scored.append(SearchResult(it["id"], it["document"], it["metadata"], score))
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def delete_document(self, document_id: str) -> None:
        with self._lock:
            self._items = [it for it in self._items if it["metadata"].get("document_id") != document_id]

    def reset(self) -> None:
        with self._lock:
            self._items = []

    def count(self) -> int:
        with self._lock:
            return len(self._items)


class ChromaVectorStore:
    def __init__(self, persist_dir: str, collection_name: str = "kb_documents") -> None:
        import chromadb

        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, id: str, embedding: List[float], metadata: dict, document: str) -> None:
        self.collection.upsert(ids=[id], embeddings=[embedding], metadatas=[metadata], documents=[document])

    def query(self, embedding: List[float], top_k: int) -> List[SearchResult]:
        res = self.collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        results: List[SearchResult] = []
        for i, (doc, meta, dist) in enumerate(
            zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
        ):
            results.append(
                SearchResult(
                    id=res["ids"][0][i],
                    document=doc or "",
                    metadata=meta or {},
                    score=max(0.0, 1.0 - float(dist)),
                )
            )
        return results

    def delete_document(self, document_id: str) -> None:
        self.collection.delete(where={"document_id": document_id})

    def reset(self) -> None:
        """清空集合。

        直接删集合再重建，比逐条删除更快，也避免残留 HNSW 索引段
        （`kb_documents` 曾累积 47 个来自测试脚本的 chunk，见 docs/review-v1-audit.md §2.5）。
        """
        name = self.collection.name
        self.client.delete_collection(name)
        self.collection = self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def count(self) -> int:
        return int(self.collection.count())


def get_vector_store(settings) -> VectorStore:
    provider = settings.vector_store.strip().lower()
    if provider == "memory":
        return InMemoryVectorStore()
    if provider == "chroma":
        return ChromaVectorStore(settings.chroma_persist_dir)
    raise ValueError(f"未知 VECTOR_STORE：{provider}")
