"""文档解析与文本切分。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.document_parser import parse_document
from app.utils.exceptions import AppError, UnsupportedFileTypeError
from app.utils.text_splitter import split_text


# ---------- text_splitter ----------

def test_short_text_is_one_chunk():
    chunks = split_text("员工考勤与休假制度\n1. 上班时间 9:00-18:00。", chunk_size=512, overlap=50)
    assert len(chunks) == 1
    assert "9:00-18:00" in chunks[0]


def test_long_text_splits_without_runt_tail():
    text = "这是一句测试内容。" * 200      # 1800 字
    chunks = split_text(text, chunk_size=200, overlap=30)
    assert len(chunks) > 5
    assert all(len(c) <= 200 for c in chunks), [len(c) for c in chunks]
    # 不应出现只剩几个字的碎尾
    assert all(len(c) >= 50 for c in chunks), [len(c) for c in chunks]


def test_paragraphs_are_merged_up_to_chunk_size():
    text = "\n\n".join(f"第{i}条：简短规定。" for i in range(20))
    chunks = split_text(text, chunk_size=200, overlap=0)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, 100), (100, 200), (100, -1)])
def test_invalid_parameters_rejected(size, overlap):
    with pytest.raises(ValueError):
        split_text("任意文本", chunk_size=size, overlap=overlap)


def test_crlf_normalized():
    chunks = split_text("第一段。\r\n\r\n第二段。", chunk_size=512, overlap=0)
    assert len(chunks) == 1
    assert "\r" not in chunks[0]


# ---------- document_parser ----------

def test_parse_txt_utf8(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("员工年假 5 天。", encoding="utf-8")
    assert "年假" in parse_document(str(p))


def test_parse_txt_gb18030(tmp_path: Path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("员工年假 5 天。".encode("gb18030"))
    assert "年假" in parse_document(str(p))


def test_parse_markdown_treated_as_text(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    text = parse_document(str(p))
    assert "标题" in text and "正文内容" in text


def test_parse_docx_including_tables(tmp_path: Path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("员工福利制度", 0)
    doc.add_paragraph("每月团建一次。")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "标准"
    p = tmp_path / "a.docx"
    doc.save(str(p))

    text = parse_document(str(p))
    assert "员工福利制度" in text
    assert "每月团建一次" in text
    assert "项目 | 标准" in text          # 表格行被拼成 " | " 文本


def test_unsupported_extension_rejected(tmp_path: Path):
    p = tmp_path / "a.xlsx"
    p.write_bytes(b"binary")
    with pytest.raises(UnsupportedFileTypeError) as exc:
        parse_document(str(p))
    assert exc.value.status_code == 415


# ---------- PDF ----------
# pypdf 只提供读写结构的 API，没有"写文本"能力；项目也不想为此引入 reportlab。
# 所以这里手工拼一个合法 PDF（对象 + 正确的 xref 偏移），保持零新增依赖。

def _minimal_pdf(texts: list[str]) -> bytes:
    """生成含 len(texts) 页的最小 PDF。文本仅支持 ASCII（用内置 Helvetica 字体）。"""
    page_ids: list[int] = []
    content_ids: list[int] = []
    next_id = 4                                    # 1=Catalog 2=Pages 3=Font
    for _ in texts:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2

    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(texts)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for pid, cid, text in zip(page_ids, content_ids, texts):
        objs[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objs[cid] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objs[oid] + b"\nendobj\n"

    xref_pos = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for oid in range(1, size):
        out += f"{offsets.get(oid, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def test_parse_pdf_extracts_text(tmp_path: Path):
    p = tmp_path / "policy.pdf"
    p.write_bytes(_minimal_pdf(["Annual leave: 5 days after 1 year."]))
    text = parse_document(str(p))
    assert "Annual leave" in text
    assert "5 days" in text


def test_parse_pdf_multiple_pages_all_extracted(tmp_path: Path):
    p = tmp_path / "multi.pdf"
    p.write_bytes(_minimal_pdf(["Page one content.", "Page two content."]))
    text = parse_document(str(p))
    assert "Page one content" in text
    assert "Page two content" in text
    assert text.index("Page one") < text.index("Page two"), "页序不能乱"


def test_parse_pdf_without_text_returns_empty(tmp_path: Path):
    """纯空白内容的 PDF → 解析结果为空（后续入库应因此被拒）。"""
    p = tmp_path / "blank.pdf"
    p.write_bytes(_minimal_pdf([""]))
    assert parse_document(str(p)).strip() == ""


def test_parse_pdf_escaped_parentheses(tmp_path: Path):
    """PDF 字符串里的括号需要转义 —— 确保我们构造器与解析行为一致。"""
    p = tmp_path / "esc.pdf"
    p.write_bytes(_minimal_pdf(["Rule (a) applies."]))
    assert "Rule (a) applies." in parse_document(str(p))


def test_parse_corrupt_pdf_raises_app_error(tmp_path: Path):
    """损坏的 PDF 必须是 AppError(400)，不能把底层异常抛成 500。"""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n%%EOF\n")
    with pytest.raises(AppError) as exc:
        parse_document(str(p))
    assert exc.value.status_code == 400
    assert "PDF 解析失败" in exc.value.detail


def test_parse_plain_text_named_pdf_raises_app_error(tmp_path: Path):
    """后缀是 .pdf 但内容是纯文本 → 同样是 400。"""
    p = tmp_path / "fake.pdf"
    p.write_bytes("这其实是一个文本文件".encode("utf-8"))
    with pytest.raises(AppError) as exc:
        parse_document(str(p))
    assert exc.value.status_code == 400


def test_parse_corrupt_docx_raises_app_error(tmp_path: Path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"PK\x03\x04 not a real docx")
    with pytest.raises(AppError) as exc:
        parse_document(str(p))
    assert exc.value.status_code == 400
    assert "DOCX 解析失败" in exc.value.detail
