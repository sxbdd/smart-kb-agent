"""M2 RAG 链路端到端验证（回退组件：hash / memory / fake）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["VECTOR_STORE"] = "memory"
os.environ["LLM_PROVIDER"] = "fake"

from app.config import settings
from app.core.embedding import get_embedding_provider
from app.core.vector_store import get_vector_store
from app.core.llm_client import get_llm_client
from app.core.reranker import get_reranker
from app.services.ingestion import IngestionService
from app.services.rag import RAGService


class FakeDB:
    def __init__(self):
        self.documents = {}

    def save_document(self, doc_id, filename, file_type, file_size, chunk_count):
        self.documents[doc_id] = dict(filename=filename, file_type=file_type, file_size=file_size, chunk_count=chunk_count)

    def delete_document(self, doc_id):
        self.documents.pop(doc_id, None)


embedder = get_embedding_provider(settings)
vs = get_vector_store(settings)
llm = get_llm_client(settings)
reranker = get_reranker(settings)
db = FakeDB()

ingestion = IngestionService(embedder, vs, db, settings.documents_dir, settings.chunk_size, settings.chunk_overlap)
rag = RAGService(embedder, vs, llm, reranker, settings.top_k, settings.rerank_top_k, settings.max_history_messages)

docs_dir = Path(settings.documents_dir)
docs_dir.mkdir(parents=True, exist_ok=True)
doc = docs_dir / "员工制度.txt"
doc.write_text(
    "员工考勤制度：\n1. 上班时间 9:00-18:00。\n"
    "2. 年假：入职满1年享5天，满5年享10天。\n"
    "3. 出差住宿标准：每晚不超过500元。\n"
    "4. 市内交通费实报实销。",
    encoding="utf-8",
)

# 1) 上传入库
resp = ingestion.ingest(str(doc), "员工制度.txt")
print("ingest:", resp.document_id, "chunks =", resp.chunk_count)
assert resp.chunk_count >= 1

# 2) 检索
results = rag.search("出差住宿标准是多少", top_k=3)
print("search hits:", [r.document[:30] for r in results])
assert results, "应检索到相关片段"

# 3) 问答 + 引用
answer, sources = rag.answer("出差住宿标准是多少？", top_k=3)
print("answer:", answer)
print("sources:", [(s.document_name, round(s.score, 3)) for s in sources])
assert answer.strip() != ""
assert sources, "应返回引用来源"

print("M2 RAG 链路验证通过")