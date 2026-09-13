"""文档索引服务：解析 → 切分 → 向量化 → 入库。"""
from __future__ import annotations

import uuid
from pathlib import Path

from app.models.schemas import DocumentUploadResponse
from app.services.tenancy import normalize_tenant
from app.utils.document_parser import parse_document
from app.utils.exceptions import AppError
from app.utils.logger import get_logger
from app.utils.text_splitter import split_text

logger = get_logger("ingestion")


class IngestionService:
    def __init__(self, embedder, vector_store, db, documents_dir: str, chunk_size: int, chunk_overlap: int,
                 settings=None) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.db = db
        self.documents_dir = documents_dir
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        #: 解析器需要它来判断 OCR 是否开启、用哪个 provider（传 None 时解析器回退全局配置）
        self.settings = settings

    def ingest(self, file_path: str, filename: str, tenant_id: str = "default") -> DocumentUploadResponse:
        """解析入库，并把租户维度写进 chunk metadata 与 documents 表。

        为什么 metadata 里必须有 `tenant_id`：向量检索的租户隔离**完全依赖它**
        （`ChromaVectorStore` 用 `where={"tenant_id": ...}` 过滤，内存库按同一字段
        过滤），见 docs/v2-plan.md §6.3。漏写就等于把文档灌进了"无租户"区，
        带租户的查询再也召不回来，且无法事后区分归属。

        `tenant_id` 走 `normalize_tenant` 归一：**`None` / 空串一律落到 `"default"`**，
        避免 metadata 里出现 `tenant_id=None`（Chroma 会把缺字段与 None 都当成
        "不匹配任何租户"，等于文档凭空消失）。
        """
        tenant = normalize_tenant(tenant_id)

        path = Path(file_path)
        raw_text = parse_document(str(path), self.settings)
        if not raw_text.strip():
            raise AppError("文档解析后内容为空，无法建立索引")

        chunks = split_text(raw_text, self.chunk_size, self.chunk_overlap)
        if not chunks:
            raise AppError("文档切分后没有可用片段")

        doc_id = str(uuid.uuid4())
        embeddings = self.embedder.encode(chunks)

        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            self.vector_store.add(
                id=f"{doc_id}_{i}",
                embedding=embedding,
                metadata={
                    "document_id": doc_id,
                    "document_name": filename,
                    "chunk_index": i,
                    "chunk_total": len(chunks),
                    # 租户维度：向量库过滤的唯一依据，既有字段一个不动
                    "tenant_id": tenant,
                },
                document=chunk,
            )

        file_type = path.suffix.lower().lstrip(".") or "txt"
        self.db.save_document(
            doc_id=doc_id,
            filename=filename,
            file_type=file_type,
            file_size=path.stat().st_size,
            chunk_count=len(chunks),
            tenant_id=tenant,
        )
        logger.info("ingested document=%s tenant=%s chunks=%d", filename, tenant, len(chunks))

        return DocumentUploadResponse(
            document_id=doc_id,
            filename=filename,
            chunk_count=len(chunks),
            status="success",
        )

    def delete(self, document_id: str, tenant_id: str = "default") -> None:
        """删除文档：向量库与元数据表都带租户条件。

        顺序是先删向量再删元数据（与 V1 一致）：向量库删除是幂等的、失败可重试；
        反过来先删元数据会留下"查得到索引却找不到文档"的孤儿片段。
        租户条件同时传给两边，跨租户的同名 `document_id` 一个都删不掉。
        """
        self.vector_store.delete_document(document_id, tenant_id)
        self.db.delete_document(document_id, tenant_id)
