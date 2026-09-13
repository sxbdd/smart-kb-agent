"""文档解析：PDF / Markdown / TXT / DOCX / Excel(xlsx,xlsm) / CSV / 图片(OCR) → 纯文本。

二进制格式（PDF / DOCX / XLSX / 图片）解析失败时统一转成 `AppError`（400），
而不是把底层异常抛到接口层变成无信息的 500 —— 用户上传损坏文件是可预期的输入。

OCR 相关的三条硬规则（`app/utils/ocr.py` 提供引擎）：
1. 图片必须开 `ENABLE_OCR`，否则报错并明确提示变量名；
2. PDF 只有在**文本层稀疏到疑似扫描件**（抽出文本 < `OCR_MIN_CHARS` 且 OCR 已开启）
   时才尝试渲染 + OCR，普通文本 PDF 绝不受 OCR 可用性影响；
3. OCR 引擎自身抛出的异常一律向上传递，不吞（否则用户拿到"内容为空"的假象）。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.utils.exceptions import AppError, UnsupportedFileTypeError
from app.utils.ocr import IMAGE_EXTS, get_ocr_engine

_TEXT_EXTS = {".txt", ".md", ".markdown", ".text"}
_EXCEL_EXTS = {".xlsx", ".xlsm"}
_CSV_EXTS = {".csv"}

#: CSV 分隔符候选（, ; 制表符 全角分号），顺序即投票优先级
_CSV_DELIMITERS = ",;\t；"
#: csv.Sniffer 只喂 ASCII 分隔符：它对全角分号判不出来，失败后走 _guess_delimiter 兜底
_CSV_ASCII_DELIMITERS = ",;\t"
#: 分隔符嗅探只看文件头部，避免超大 CSV 反复扫描
_CSV_SNIFF_CHARS = 8192

#: PDF 渲染成图片时的分辨率：200dpi 在 OCR 清晰度与内存占用之间取平衡
_PDF_RENDER_DPI = 200


def parse_document(file_path: str, settings: Any | None = None) -> str:
    """按扩展名分派解析器，返回纯文本。

    `settings` 只需在 OCR 分支（图片 / 扫描件 PDF）用到；不传则回退到应用单例配置，
    这样既保持 `parse_document(path)` 的旧调用方式不变，又让测试能注入定制配置。
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path, settings)
    if ext in _TEXT_EXTS:
        return _parse_text(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext in _EXCEL_EXTS:
        return _parse_excel(path)
    if ext in _CSV_EXTS:
        return _parse_csv(path)
    if ext in IMAGE_EXTS:
        return _parse_image(path, settings)
    supported = "PDF / Markdown / TXT / DOCX / XLSX / XLSM / CSV / 图片(PNG,JPG,JPEG,BMP,WEBP,TIF,TIFF)"
    raise UnsupportedFileTypeError(f"不支持的文档类型：{ext}（支持 {supported}）")


def _parse_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _parse_pdf(path: Path, settings: Any | None = None) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnsupportedFileTypeError("缺少 pypdf，请先安装：pip install pypdf") from exc

    try:
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            raise AppError("PDF 已加密，请先解除密码后再上传", 400)
        pages = [page.extract_text() or "" for page in reader.pages]
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"PDF 解析失败，文件可能已损坏：{exc}", 400) from exc
    text = "\n\n".join(pages)

    # ---- 扫描件判定：文本层稀疏 + OCR 开启，才走渲染识别；否则直接返回原文本 ----
    resolved = _resolve_settings(settings)
    if not _ocr_enabled(resolved) or len(text.strip()) >= _min_chars(resolved):
        return text
    ocr_text = _ocr_pdf_pages(path, resolved, has_text_layer=bool(text.strip()))
    # 引擎识别不出内容（如 NoopOcrEngine）时不要丢掉 pypdf 已经抽到的文本
    return ocr_text.strip() or text


def _parse_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise UnsupportedFileTypeError("缺少 python-docx，请先安装：pip install python-docx") from exc

    try:
        doc = Document(str(path))
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"DOCX 解析失败，文件可能已损坏：{exc}", 400) from exc

    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


# ---------- Excel / CSV ----------

def _parse_excel(path: Path) -> str:
    """xlsx / xlsm → 文本：每个 sheet 一行 `【sheet名】`，数据行用 ` | ` 连接。

    约定（与 DOCX 表格的既有行为保持一致）：
    - 单元格先 strip，**空单元格直接丢弃**，只保留有内容的格子；
    - 整行没有任何内容（含全空白行、Excel 常见的尾部空行）则跳过；
    - 空的 sheet 仍然输出自己的标题行，便于后续按 sheet 归属切片。
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise UnsupportedFileTypeError("缺少 openpyxl，请先安装：pip install openpyxl") from exc

    try:
        # read_only=True 流式遍历，避免大表把整个工作簿读进内存
        workbook = load_workbook(str(path), read_only=True, data_only=True)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"Excel 解析失败，文件可能已损坏：{exc}", 400) from exc

    try:
        lines: list[str] = []
        for sheet in workbook.worksheets:
            lines.append(f"【{sheet.title}】")
            try:
                rows = sheet.iter_rows(values_only=True)
            except Exception as exc:
                raise AppError(f"Excel 解析失败，文件可能已损坏：{exc}", 400) from exc
            for row in rows:
                cells = [text for text in (_cell_to_text(v) for v in row) if text]
                if cells:
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        try:
            workbook.close()
        except Exception:  # pragma: no cover - 关闭失败不应掩盖解析结果
            pass


def _cell_to_text(value: object) -> str:
    """单元格值 → 文本；None 表示空，纯日期去掉多余的 00:00:00，整数浮点去掉 .0。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        # openpyxl 把日期也读成 datetime，零点的按纯日期输出，避免污染词面
        if value.time() == time.min:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_csv(path: Path) -> str:
    """CSV → 文本：复用文本解析的编码回退链，嗅探分隔符后逐行 ` | ` 连接。"""
    raw = _parse_text(path)
    if not raw.strip():
        return ""

    sample = raw[:_CSV_SNIFF_CHARS]
    try:
        dialect: csv.Dialect | None = csv.Sniffer().sniff(sample, delimiters=_CSV_ASCII_DELIMITERS)
    except csv.Error:
        dialect = None

    try:
        stream = io.StringIO(raw)
        # 嗅探成功时连引号规则一起用；失败（如全角分号）则退回按频率投票的分隔符
        if dialect is not None:
            reader: Any = csv.reader(stream, dialect)
        else:
            reader = csv.reader(stream, delimiter=_guess_delimiter(sample))
        lines: list[str] = []
        for row in reader:
            cells = [c.strip() for c in row if c.strip()]
            if cells:
                lines.append(" | ".join(cells))
        return "\n".join(lines)
    except csv.Error as exc:
        raise AppError(f"CSV 解析失败，文件可能已损坏：{exc}", 400) from exc


def _guess_delimiter(sample: str) -> str:
    """csv.Sniffer 判不出分隔符时的兜底：按"出现该分隔符的行数"投票，多者胜。

    主要为了全角分号（；）这类非 ASCII 分隔符，以及只有一行的迷你 CSV。
    """
    lines = [line for line in sample.splitlines() if line.strip()]
    if not lines:
        return ","
    best, best_hits = ",", 0
    for candidate in _CSV_DELIMITERS:
        hits = sum(1 for line in lines if candidate in line)
        if hits > best_hits:
            best, best_hits = candidate, hits
    return best


# ---------- OCR（图片 / 扫描件 PDF） ----------

def _parse_image(path: Path, settings: Any | None = None) -> str:
    """图片 → OCR 文本；未开启 OCR 时给出可操作的错误而不是默默返回空。"""
    resolved = _resolve_settings(settings)
    if not _ocr_enabled(resolved):
        raise AppError(
            "图片文档需要 OCR 才能解析：请在 .env 中设置 ENABLE_OCR=true 后再上传"
            "（OCR 引擎由 OCR_PROVIDER 选择：rapidocr / fake / none）",
            400,
        )
    engine = get_ocr_engine(resolved)
    text = engine.extract_text(path.read_bytes()).strip()
    if not text:
        provider = getattr(resolved, "ocr_provider", "?")
        raise AppError(
            f"OCR 未能从图片中识别出文本（OCR_PROVIDER={provider}）；"
            "若使用的是 none，请改为 rapidocr（需安装）或 fake（离线联调）",
            400,
        )
    return text


def _ocr_pdf_pages(path: Path, settings: Any, has_text_layer: bool) -> str:
    """扫描件 PDF：用 PyMuPDF 逐页渲染成 PNG，再交给 OCR 引擎。

    `has_text_layer` 为真表示 pypdf 已经抽出了一些文本（只是少于阈值）——
    此时即便渲染库缺失也应该放行，绝不让普通文本 PDF 因为 OCR 不可用而失败。
    """
    rendered = _render_pdf_pages(path, has_text_layer=has_text_layer)
    if not rendered:
        return ""
    engine = get_ocr_engine(settings)
    page_texts = [engine.extract_text(image).strip() for image in rendered]
    return "\n\n".join(t for t in page_texts if t)


def _render_pdf_pages(path: Path, has_text_layer: bool = False) -> list[bytes]:
    """把 PDF 每页渲染成 PNG 字节；PyMuPDF 未安装时按 `has_text_layer` 决定报错还是放弃。

    单独抽出来是为了让"依赖缺失"与"渲染失败"两类错误各只有一处，便于测试引用。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        if has_text_layer:
            # 已有文本层 → 直接放弃 OCR，让调用方回退到 pypdf 的文本
            return []
        raise AppError(
            "扫描件 PDF 需要 PyMuPDF 渲染页面后才能 OCR，请先安装：pip install pymupdf"
            "（或设置 ENABLE_OCR=false 跳过扫描件识别）",
            400,
        ) from exc

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise AppError(f"扫描件 PDF 渲染失败，文件可能已损坏：{exc}", 400) from exc

    try:
        images: list[bytes] = []
        for index, page in enumerate(doc):
            try:
                pixmap = page.get_pixmap(dpi=_PDF_RENDER_DPI)
                images.append(pixmap.tobytes("png"))
            except Exception as exc:
                raise AppError(f"第 {index + 1} 页渲染失败：{exc}", 400) from exc
        return images
    finally:
        try:
            doc.close()
        except Exception:  # pragma: no cover - 关闭失败不应掩盖渲染结果
            pass


# ---------- 配置读取（延迟解析，避免 import 期依赖配置） ----------

def _resolve_settings(settings: Any | None) -> Any:
    """显式传入优先；否则用应用单例（只在真正需要 OCR 配置时才 import）。"""
    if settings is not None:
        return settings
    from app.config import settings as app_settings

    return app_settings


def _ocr_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "enable_ocr", False))


def _min_chars(settings: Any) -> int:
    try:
        return int(getattr(settings, "ocr_min_chars", 20))
    except (TypeError, ValueError):
        return 20
