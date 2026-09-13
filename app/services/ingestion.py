"""文档索引服务：解析 → 切分 → 向量化 → 入库。"""
from __future__ import annotations

import uuid
from pathlib import Path

from app.models.schemas import DocumentUploadResponse
from app.utils.document_parser import parse_document
from app.utils.exceptions import AppError
from app.utils.logger import get_logger
from app.utils.text_splitter import split_text

logger = get_logger("ingestion")


class IngestionService:
    def __init__(self, embedder, vector_store, db, documents_dir: str, chunk_size: int, chunk_overlap: int) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.db = db
        self.documents_dir = documents_dir
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def ingest(self, file_path: str, filename: str) -> DocumentUploadResponse:
        path = Path(file_path)
        raw_text = parse_document(str(path))
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
        )
        logger.info("ingested document=%s chunks=%d", filename, len(chunks))

        return DocumentUploadResponse(
            document_id=doc_id,
            filename=filename,
            chunk_count=len(chunks),
            status="success",
        )

    def delete(self, document_id: str) -> None:
        self.vector_store.delete_document(document_id)
        self.db.delete_document(document_id)
