"""向量库：Chroma（默认）/ InMemory（开发测试回退）。"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, List, Protocol


@dataclass
class SearchResult:
    id: str
    document: str
    metadata: dict
    score: float


#: 向量库里的"默认租户"。升级前灌进来的老数据没有 `tenant_id` 字段，
#: 它们统统属于这个租户 —— 见 `_in_tenant()` 的兜底逻辑。
DEFAULT_TENANT = "default"


class VectorStore(Protocol):
    def add(self, id: str, embedding: List[float], metadata: dict, document: str) -> None: ...

    def query(self, embedding: List[float], top_k: int, min_score: float = 0.0,
              tenant_id: str | None = None) -> List[SearchResult]: ...

    def delete_document(self, document_id: str, tenant_id: str | None = None) -> None: ...
    def reset(self) -> None: ...
    def count(self) -> int: ...


def _in_tenant(metadata: dict, tenant_id: str | None) -> bool:
    """判断一条 chunk 是否属于 `tenant_id` 这个租户。

    两个刻意的设计（多租户 RAG 最容易漏的地方，见 docs/v2-plan.md §6.3）：

    1. `tenant_id is None` 表示**不过滤** —— V1 的单租户行为原样保留，
       既有单测（不传租户）必须一条不差地继续通过。
    2. 缺失 `tenant_id` 字段的老数据视为 `"default"` —— 升级前灌进库里
       的 chunk 没有这个字段，若直接判"不匹配"会凭空丢失历史数据；
       归到默认租户既兼容老数据，也不会让它们泄漏到别的租户。
    """
    if tenant_id is None:
        return True
    return metadata.get("tenant_id", DEFAULT_TENANT) == tenant_id


def _above_threshold(results: List[SearchResult], min_score: float) -> List[SearchResult]:
    """按相似度阈值过滤：保留 `score >= min_score`（等于阈值算通过）。

    `min_score <= 0.0` 时直接原样返回 —— 这是 V1 的默认路径，
    必须保证结果条数、顺序与开启阈值前**完全一致**。
    """
    if min_score <= 0.0:
        return results
    return [r for r in results if r.score >= min_score]


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

    def query(self, embedding: List[float], top_k: int, min_score: float = 0.0,
              tenant_id: str | None = None) -> List[SearchResult]:
        """按余弦相似度检索 top_k 个片段。

        Args:
            embedding: 查询向量。
            top_k: 最多返回条数。
            min_score: 相似度下限，只保留 `score >= min_score` 的结果。
                默认 0.0 表示不过滤（与 V1 行为一致）。
            tenant_id: 租户过滤；`None` 表示不过滤（既有行为）。
                非 None 时先按 metadata 里的 `tenant_id` 过滤，再算分数 ——
                过滤放在最前面是为了让跨租户片段**连分数都不参与比较**，
                结果里不可能出现别的租户。

        注意两者在实现中的位置不同，但对外语义一致：
        - `tenant_id` 在**打分之前**就把别的租户的片段排除掉（它们不参与排序）；
        - `min_score` 在**排序之后、截断 top_k 之前**过滤（V1 的既有语义，一字未改）。
        两个过滤都只做"减少"，因此提高阈值或加上租户条件只会让结果变少，
        剩余结果的相对顺序与"不过滤"时完全一致。
        """
        with self._lock:
            scored = []
            for it in self._items:
                # 租户过滤优先：别的租户的片段根本不进入打分与排序
                if not _in_tenant(it["metadata"], tenant_id):
                    continue
                score = _cosine(embedding, it["embedding"])
                scored.append(SearchResult(it["id"], it["document"], it["metadata"], score))
        scored.sort(key=lambda r: r.score, reverse=True)
        return _above_threshold(scored, min_score)[:top_k]

    def delete_document(self, document_id: str, tenant_id: str | None = None) -> None:
        """按 `document_id` 删除；`tenant_id` 非 None 时还要属于该租户。

        `document_id` 是 UUID，理论上不会跨租户重名；但删除是不可逆的写操作，
        加上租户条件后即使 ID 相同也只会删掉自己租户的片段（纵深防御，
        与 `ChromaVectorStore` 的 `$and` 条件保持一致）。
        """
        with self._lock:
            self._items = [
                it for it in self._items
                if it["metadata"].get("document_id") != document_id
                or not _in_tenant(it["metadata"], tenant_id)
            ]

    def reset(self) -> None:
        """清空全部条目。

        内存实现直接把列表置空即可；跨测试复用同一个 store 实例时靠它隔离状态。
        """
        with self._lock:
            self._items = []

    def count(self) -> int:
        """返回当前库内片段（chunk）总数，用于灌库后核对索引规模。"""
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

    def query(self, embedding: List[float], top_k: int, min_score: float = 0.0,
              tenant_id: str | None = None) -> List[SearchResult]:
        """按余弦相似度检索 top_k 个片段。

        Args:
            embedding: 查询向量。
            top_k: 最多返回条数。
            min_score: 相似度下限，只保留 `score >= min_score`。
                默认 0.0 表示不过滤（与 V1 行为一致）。
            tenant_id: 租户过滤；`None` 表示不过滤（既有行为）。
                非 None 时给 `collection.query()` 传 `where={"tenant_id": ...}`，
                让过滤在 Chroma 内部完成，避免把别的租户的向量取出来再丢弃。

        实现细节：
        - 这里**先向 Chroma 取全量候选**（`n_results=总数`）再过滤、截断。
          原因是 Chroma 的 `where` 元数据过滤无法表达「相似度 >= 阈值」，
          若只取 `n_results=top_k` 就容易出现「前 k 个都低于阈值 → 返回空」的
          漏召回；全量比较则与 `InMemoryVectorStore` 的结果**逐条一致**
          （距离转分：`score = 1 - distance`，再 `max(0.0, ...)` 截断）。
        - **`where` 与 `n_results` 的关系（本地 chromadb 1.5.9 实测）**：加了 `where`
          之后匹配数可能小于 `n_results=count()`，Chroma **不会**因此报错，而是只返回
          匹配到的那几条（120 条里筛出 60 条时 `n_results=120` 返回 60 条；筛出 0 条
          时返回空列表），所以这里保持原写法，不额外 `get(where=...)` 预数一遍 ——
          少一次往返，也不改动既有代码路径。若将来升级到"匹配数 < n_results 就抛错"
          的版本，再退回"先 `collection.get(where=...)` 数出匹配数、再用该数做
          `n_results`"的写法。
        - **已知偏差（老数据）**：内存实现把缺 `tenant_id` 字段的 chunk 视为
          `"default"`，而 Chroma 的 `where` 对缺字段的记录天然不匹配（`$exists`
          不在 query 支持的算子列表里），因此**升级前灌进 Chroma 的老数据在带租户
          查询时不可见**。这是"宁可查不到、不可跨租户泄漏"的 fail-closed 取舍；
          老库需要按文档重新灌一次（或在迁移脚本里补齐 metadata）见交付说明。
        """
        total = self.count()
        if total <= 0:
            return []

        # 租户过滤下沉到 Chroma：只取本租户的候选（None = 不过滤，保持 V1 行为）
        where = {"tenant_id": tenant_id} if tenant_id is not None else None

        res = self.collection.query(
            query_embeddings=[embedding],
            n_results=total,
            where=where,
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
        results.sort(key=lambda r: r.score, reverse=True)
        return _above_threshold(results, min_score)[:top_k]

    def delete_document(self, document_id: str, tenant_id: str | None = None) -> None:
        """按 `document_id` 删除；`tenant_id` 非 None 时用 `$and` 同时限定租户。

        为什么用 `$and` 而不是两个 where 键：Chroma 的 `where` 字典形如
        `{"a": 1, "b": 2}` 本身就是 AND 语义，但显式写 `$and` 更清楚，也和
        "删除必须同时满足文档与租户"这条规则一一对应，避免以后有人误改成 OR。
        """
        if tenant_id is None:
            self.collection.delete(where={"document_id": document_id})
            return
        self.collection.delete(where={"$and": [{"document_id": document_id}, {"tenant_id": tenant_id}]})

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
        """返回当前集合内片段（chunk）总数。

        灌库脚本用它核对「语料 → chunk」的规模（V1 基线为 13），
        `query()` 也用它决定全量召回的上限。
        """
        return int(self.collection.count())


def get_vector_store(settings) -> VectorStore:
    provider = settings.vector_store.strip().lower()
    if provider == "memory":
        return InMemoryVectorStore()
    if provider == "chroma":
        return ChromaVectorStore(settings.chroma_persist_dir)
    raise ValueError(f"未知 VECTOR_STORE：{provider}")
