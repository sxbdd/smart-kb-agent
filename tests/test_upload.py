"""上传接口：解析入库、类型校验、大小上限、失败回滚。"""
from __future__ import annotations

import dataclasses
import io
from pathlib import Path

from fastapi.testclient import TestClient

POLICY = "员工考勤制度：上班时间 9:00-18:00。"


def _upload(client, auth, name: str, data: bytes, mime: str = "text/plain"):
    return client.post("/upload", files={"file": (name, io.BytesIO(data), mime)}, headers=auth)


def test_upload_txt_creates_index(client, auth):
    resp = _upload(client, auth, "制度.txt", POLICY.encode("utf-8"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["chunk_count"] >= 1
    assert body["document_id"]

    docs = client.get("/documents", headers=auth).json()
    assert [d["filename"] for d in docs] == ["制度.txt"]


def test_upload_docx(client, auth):
    docx = __import__("pytest").importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("员工福利：每年体检一次。")
    buf = io.BytesIO()
    doc.save(buf)

    resp = _upload(client, auth, "福利.docx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert resp.status_code == 200, resp.text
    assert resp.json()["chunk_count"] >= 1


def test_upload_unsupported_type_415(client, auth):
    resp = _upload(client, auth, "表格.xlsx", b"binary-data")
    assert resp.status_code == 415


def test_upload_empty_file_400(client, auth):
    assert _upload(client, auth, "空.txt", b"").status_code == 400


def test_upload_corrupt_pdf_400_not_500(client, auth):
    """损坏的 PDF：接口应返回 400（可预期的输入问题），不是 500。"""
    from tests.test_parser_splitter import _minimal_pdf  # 复用同一个 PDF 构造器

    broken = _upload(client, auth, "坏了.pdf", b"%PDF-1.4\nnot really a pdf\n%%EOF\n", "application/pdf")
    assert broken.status_code == 400, broken.text
    assert "PDF" in broken.json()["detail"]

    good = _upload(client, auth, "正常.pdf", _minimal_pdf(["Travel policy: hotel cap 600 CNY."]), "application/pdf")
    assert good.status_code == 200, good.text
    assert good.json()["chunk_count"] >= 1
    assert client.get("/documents", headers=auth).json()[0]["filename"] == "正常.pdf"


def test_upload_too_large_413(settings, fake_db):
    """大小上限：分块读取，超限即中断，避免整个文件进内存。"""
    from app.factory import create_app

    tight = dataclasses.replace(settings, max_upload_mb=1)
    client = TestClient(create_app(tight))
    token = client.post("/auth/register", json={"username": "big", "password": "secret123"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    resp = _upload(client, auth, "大文件.txt", b"a" * (1024 * 1024 + 10))
    assert resp.status_code == 413

    # 失败后不应留下半截文件，也不应写入文档表
    docs_dir = Path(tight.documents_dir)
    leftovers = [p for p in docs_dir.glob("*") if p.is_file()] if docs_dir.exists() else []
    assert leftovers == [], f"失败上传残留文件: {leftovers}"
    assert client.get("/documents", headers=auth).json() == []


def test_upload_rejects_empty_parsed_content(client, auth):
    """纯空白文件能落盘但解析后为空 → 应报错并回滚。"""
    resp = _upload(client, auth, "空白.txt", "   \n\n".encode("utf-8"))
    assert resp.status_code == 400
    assert client.get("/documents", headers=auth).json() == []


def test_filename_path_traversal_is_neutralised(client, auth, settings):
    """文件名里的路径必须被剥离，不能写到 documents_dir 之外。"""
    resp = _upload(client, auth, "../../evil.txt", POLICY.encode("utf-8"))
    assert resp.status_code == 200
    assert resp.json()["filename"] == "evil.txt"

    docs_dir = Path(settings.documents_dir)
    assert not (docs_dir.parent.parent / "evil.txt").exists()
    assert list(docs_dir.glob("*evil.txt"))


def test_delete_document(client, auth):
    doc_id = _upload(client, auth, "待删.txt", POLICY.encode("utf-8")).json()["document_id"]
    assert client.delete(f"/documents/{doc_id}", headers=auth).status_code == 200
    assert client.get("/documents", headers=auth).json() == []


def test_delete_unknown_document_404(client, auth):
    assert client.delete("/documents/nope", headers=auth).status_code == 404
