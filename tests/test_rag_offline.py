"""离线 RAG 链路：解析 → 切分 → 向量化 → 入库 → 检索 → 生成 → 引用。"""
from __future__ import annotations

from pathlib import Path

POLICY = (
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00，午休 12:00-13:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
)


def _ingest_policy(services, name: str = "员工考勤制度.txt"):
    docs_dir = Path(services["settings"].documents_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / name
    path.write_text(POLICY, encoding="utf-8")
    return services["ingestion"].ingest(str(path), name)


def test_ingest_creates_chunks_and_metadata(services):
    resp = _ingest_policy(services)
    assert resp.status == "success"
    assert resp.chunk_count >= 1
    assert resp.filename == "员工考勤制度.txt"


def test_search_returns_relevant_chunk(services):
    _ingest_policy(services)
    results = services["rag"].search("出差住宿标准是多少", top_k=3)
    assert results, "应检索到相关片段"
    assert any("600" in r.document for r in results)
    assert results[0].metadata["document_name"] == "员工考勤制度.txt"


def test_answer_returns_citation_sources(services):
    _ingest_policy(services)
    answer, sources = services["rag"].answer("出差住宿标准是多少？", top_k=3)
    assert answer.strip()
    assert sources, "应返回引用来源"
    assert sources[0].document_name == "员工考勤制度.txt"
    assert 0.0 <= sources[0].score <= 1.0
    assert sources[0].content


def test_ingest_rejects_empty_document(services, tmp_path):
    from app.utils.exceptions import AppError

    path = Path(services["settings"].documents_dir) / "empty.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n\n  ", encoding="utf-8")
    try:
        services["ingestion"].ingest(str(path), "empty.txt")
    except AppError as exc:
        assert "解析后内容为空" in exc.detail
    else:  # pragma: no cover
        raise AssertionError("空文档应被拒绝")


def test_delete_removes_from_vector_store(services):
    resp = _ingest_policy(services)
    assert services["rag"].search("出差住宿标准", top_k=3)
    services["ingestion"].delete(resp.document_id)
    assert services["rag"].search("出差住宿标准", top_k=3) == []
