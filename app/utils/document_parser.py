"""文档解析：PDF / Markdown / TXT → 纯文本。"""
from __future__ import annotations

from pathlib import Path

from app.utils.exceptions import UnsupportedFileTypeError

_TEXT_EXTS = {".txt", ".md", ".markdown", ".text"}


def parse_document(file_path: str) -> str:
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in _TEXT_EXTS:
        return _parse_text(path)
    raise UnsupportedFileTypeError(f"不支持的文档类型：{ext}（支持 PDF / Markdown / TXT）")


def _parse_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _parse_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFileTypeError("缺少 pypdf，请先安装：pip install pypdf") from exc

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)
