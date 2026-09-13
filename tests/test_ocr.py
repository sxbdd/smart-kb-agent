"""OCR 插件层 + 扫描件解析链路（离线）。

覆盖两条最容易出问题的边界：
1. **降级**：OCR 未开启 / 引擎缺失 / 引擎返回空时，各自要有清晰且可操作的报错，
   而"普通文本 PDF"在 OCR 完全不可用时**必须照常解析成功**；
2. **不吞异常**：OCR 引擎自身抛出的 AppError 要原样冒泡，不能被包装成"内容为空"。

离线手段：
- 用 `FakeOcrEngine`（确定性输出）替代真实识别；
- 用假 `fitz` 模块（monkeypatch sys.modules）替代 PyMuPDF 的页面渲染；
- 用 `sys.modules[...] = None` 让 `import` 稳定抛 ImportError，从而确定性地模拟
  "依赖未安装"，不依赖本机是否真的装了 rapidocr / pymupdf。
"""
from __future__ import annotations

import dataclasses
import io
import os
import sys
import types
from pathlib import Path

import pytest

from app.utils import document_parser
from app.utils.document_parser import parse_document
from app.utils.exceptions import AppError
from app.utils.ocr import (
    FakeOcrEngine,
    NoopOcrEngine,
    OcrEngine,
    RapidOcrEngine,
    get_ocr_engine,
)
from tests.test_parser_splitter import _minimal_pdf


# ---------- 工具 ----------

@pytest.fixture
def ocr_settings(settings):
    """开启 OCR 且使用 fake 引擎的配置（enable_ocr 默认是关的）。"""
    return dataclasses.replace(settings, enable_ocr=True, ocr_provider="fake", ocr_min_chars=20)


def _without_module(monkeypatch, name: str) -> None:
    """把 sys.modules[name] 置为 None：`import name` 会稳定抛 ImportError。"""
    monkeypatch.setitem(sys.modules, name, None)


class _FakePixmap:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def tobytes(self, fmt: str = "png") -> bytes:
        return self._data


class _FakePage:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.dpi_seen: int | None = None

    def get_pixmap(self, dpi: int = 200) -> _FakePixmap:
        self.dpi_seen = dpi
        return _FakePixmap(self._data)


class _FakeDoc:
    def __init__(self, n_pages: int) -> None:
        # 每页字节数递增 → FakeOcrEngine 的输出可区分，便于断言"逐页识别且页序不乱"
        self.pages = [_FakePage(b"x" * (10 + i)) for i in range(n_pages)]
        self.closed = False

    def __iter__(self):
        return iter(self.pages)

    def close(self) -> None:
        self.closed = True


def _install_fake_fitz(monkeypatch, n_pages: int = 1) -> list[_FakeDoc]:
    """注入假 fitz 模块，返回它创建过的 doc 列表（用于断言 close 被调用）。"""
    module = types.ModuleType("fitz")
    docs: list[_FakeDoc] = []

    def _open(path: str) -> _FakeDoc:
        doc = _FakeDoc(n_pages)
        docs.append(doc)
        return doc

    module.open = _open  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", module)
    return docs


class _BoomEngine:
    """模拟"引擎内部出错"：异常必须原样冒泡。"""

    def extract_text(self, image_bytes: bytes) -> str:
        raise AppError("OCR 引擎内部错误：模拟失败", 400)


def _write_image(tmp_path: Path, name: str = "scan.png", payload: bytes = b"\x89PNG\r\n\x1a\nfake") -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


# ---------- 引擎实现 ----------

def test_fake_engine_is_deterministic_and_length_based():
    engine = FakeOcrEngine()
    first = engine.extract_text(b"abcdef")
    assert first == engine.extract_text(b"abcdef")
    assert first == "OCR_FAKE:6"


def test_fake_engine_custom_prefix_and_empty_input():
    assert FakeOcrEngine("X").extract_text(b"ab") == "X:2"
    assert FakeOcrEngine().extract_text(b"") == ""


def test_noop_engine_always_empty():
    assert NoopOcrEngine().extract_text(b"anything") == ""
    assert NoopOcrEngine().extract_text(b"") == ""


def test_engines_satisfy_protocol():
    """协议用 runtime_checkable，方便容器/插件做 isinstance 校验。"""
    assert isinstance(FakeOcrEngine(), OcrEngine)
    assert isinstance(NoopOcrEngine(), OcrEngine)
    assert isinstance(RapidOcrEngine(), OcrEngine)


def test_rapidocr_engine_is_lazy(monkeypatch, ocr_settings):
    """构造 RapidOcrEngine 不应触发 import；依赖缺失只在真正识别时报错。"""
    _without_module(monkeypatch, "rapidocr_onnxruntime")
    engine = get_ocr_engine(dataclasses.replace(ocr_settings, ocr_provider="rapidocr"))
    assert isinstance(engine, RapidOcrEngine)
    with pytest.raises(AppError) as exc:
        engine.extract_text(b"png-bytes")
    assert exc.value.status_code == 400
    assert "rapidocr" in exc.value.detail
    assert "pip install" in exc.value.detail


def test_rapidocr_engine_empty_input_short_circuits(monkeypatch):
    """空字节不该去加载模型。"""
    _without_module(monkeypatch, "rapidocr_onnxruntime")
    assert RapidOcrEngine().extract_text(b"") == ""


def test_unknown_provider_rejected(settings):
    with pytest.raises(AppError) as exc:
        get_ocr_engine(dataclasses.replace(settings, ocr_provider="tesseract"))
    assert exc.value.status_code == 400
    assert "OCR_PROVIDER" in exc.value.detail
    assert "tesseract" in exc.value.detail


@pytest.mark.parametrize(
    "provider,expected",
    [("fake", FakeOcrEngine), ("none", NoopOcrEngine), ("RAPIDOCR", RapidOcrEngine)],
)
def test_provider_dispatch(settings, provider, expected):
    """大小写不敏感（env 里大小写写错不该炸）。"""
    assert isinstance(get_ocr_engine(dataclasses.replace(settings, ocr_provider=provider)), expected)


# ---------- 图片分支 ----------

def test_parse_image_with_fake_ocr(tmp_path: Path, ocr_settings):
    payload = b"\x89PNG\r\n\x1a\n" + b"scan-bytes"
    p = _write_image(tmp_path, payload=payload)
    assert parse_document(str(p), ocr_settings) == f"OCR_FAKE:{len(payload)}"


def test_parse_image_supported_extensions(tmp_path: Path, ocr_settings):
    for ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
        p = tmp_path / f"scan{ext}"
        p.write_bytes(b"1234567890")
        assert parse_document(str(p), ocr_settings) == "OCR_FAKE:10", ext


def test_parse_image_requires_enable_ocr(tmp_path: Path, settings):
    """OCR 关闭时给的是"去开 ENABLE_OCR"的可操作提示，而不是静默空文本。"""
    p = _write_image(tmp_path)
    off = dataclasses.replace(settings, enable_ocr=False, ocr_provider="fake")
    with pytest.raises(AppError) as exc:
        parse_document(str(p), off)
    assert exc.value.status_code == 400
    assert "ENABLE_OCR" in exc.value.detail


def test_parse_image_with_none_provider_reports_provider(tmp_path: Path, ocr_settings):
    """provider=none 时识别结果为空 → 报错文案要点出 OCR_PROVIDER=none。"""
    p = _write_image(tmp_path)
    none_settings = dataclasses.replace(ocr_settings, ocr_provider="none")
    with pytest.raises(AppError) as exc:
        parse_document(str(p), none_settings)
    assert exc.value.status_code == 400
    assert "OCR_PROVIDER=none" in exc.value.detail


def test_parse_image_unknown_provider_raises(tmp_path: Path, ocr_settings):
    p = _write_image(tmp_path)
    bad = dataclasses.replace(ocr_settings, ocr_provider="whatever")
    with pytest.raises(AppError) as exc:
        parse_document(str(p), bad)
    assert "OCR_PROVIDER" in exc.value.detail


# ---------- 扫描件 PDF 分支 ----------

def test_sparse_text_pdf_falls_back_to_ocr(tmp_path: Path, ocr_settings, monkeypatch):
    """文本层稀疏（"AB" < ocr_min_chars）→ 渲染 + 逐页 OCR，页序保持。"""
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf(["AB", "CD"]))
    docs = _install_fake_fitz(monkeypatch, n_pages=2)

    text = parse_document(str(p), ocr_settings)
    # 页与页之间用空行分隔（与 _parse_pdf 的页分隔一致），过滤空行后应保持页序
    assert [line for line in text.splitlines() if line.strip()] == ["OCR_FAKE:10", "OCR_FAKE:11"]
    assert docs and docs[0].closed, "渲染完必须关闭 PyMuPDF 文档，避免句柄泄漏"
    assert docs[0].pages[0].dpi_seen == 200, "渲染分辨率应可预期"


def test_scanned_pdf_without_text_layer_uses_ocr(tmp_path: Path, ocr_settings, monkeypatch):
    """完全没有文本层的 PDF（真扫描件）→ 走 OCR。"""
    p = tmp_path / "scan_blank.pdf"
    p.write_bytes(_minimal_pdf([""]))
    _install_fake_fitz(monkeypatch, n_pages=1)

    assert parse_document(str(p), ocr_settings) == "OCR_FAKE:10"


def test_text_pdf_is_untouched_when_ocr_disabled(tmp_path: Path, settings, monkeypatch):
    """enable_ocr=False（默认）时，稀疏文本 PDF 也直接返回 pypdf 抽到的文本。"""
    p = tmp_path / "short.pdf"
    p.write_bytes(_minimal_pdf(["AB"]))
    # 一旦进入 OCR 分支就会被调用 → 直接失败，证明分支没被走到
    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _BoomEngine())

    off = dataclasses.replace(settings, enable_ocr=False)
    assert parse_document(str(p), off).strip() == "AB"


def test_long_text_pdf_never_enters_ocr(tmp_path: Path, ocr_settings, monkeypatch):
    """文本量达标 → 即使 OCR 开着也不该碰 OCR（普通文本 PDF 的硬性保证）。"""
    long_text = "Annual leave: 5 days after 1 year of service."
    p = tmp_path / "policy.pdf"
    p.write_bytes(_minimal_pdf([long_text]))
    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _BoomEngine())

    assert long_text in parse_document(str(p), ocr_settings)


def test_text_pdf_not_broken_when_pymupdf_missing(tmp_path: Path, ocr_settings, monkeypatch):
    """硬性要求：稀疏文本 PDF + OCR 开启 + PyMuPDF 未安装 → 仍返回已有文本，不报错。"""
    _without_module(monkeypatch, "fitz")
    p = tmp_path / "short.pdf"
    p.write_bytes(_minimal_pdf(["AB"]))
    assert parse_document(str(p), ocr_settings).strip() == "AB"


def test_scanned_pdf_without_pymupdf_raises_clear_error(tmp_path: Path, ocr_settings, monkeypatch):
    """确实是扫描件（零文本层）而 PyMuPDF 缺失 → 明确提示 pip install pymupdf。"""
    _without_module(monkeypatch, "fitz")
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf([""]))
    with pytest.raises(AppError) as exc:
        parse_document(str(p), ocr_settings)
    assert exc.value.status_code == 400
    assert "pymupdf" in exc.value.detail
    assert "ENABLE_OCR" in exc.value.detail


def test_scanned_pdf_with_rapidocr_missing_raises(tmp_path: Path, ocr_settings, monkeypatch):
    """provider=rapidocr 但依赖未装 → 报错要点出 rapidocr，而不是"内容为空"。"""
    _install_fake_fitz(monkeypatch, n_pages=1)
    _without_module(monkeypatch, "rapidocr_onnxruntime")
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf([""]))

    rapid = dataclasses.replace(ocr_settings, ocr_provider="rapidocr")
    with pytest.raises(AppError) as exc:
        parse_document(str(p), rapid)
    assert "rapidocr" in exc.value.detail


def test_scanned_pdf_with_none_provider_falls_back_to_text_layer(tmp_path: Path, ocr_settings, monkeypatch):
    """provider=none 时 OCR 结果为空 → 回退到 pypdf 文本，不丢已有内容、不抛异常。"""
    _install_fake_fitz(monkeypatch, n_pages=1)
    p = tmp_path / "short.pdf"
    p.write_bytes(_minimal_pdf(["AB"]))
    assert parse_document(str(p), dataclasses.replace(ocr_settings, ocr_provider="none")).strip() == "AB"


def test_ocr_min_chars_boundary(tmp_path: Path, settings, monkeypatch):
    """恰好等于阈值 → 不触发 OCR（边界取">= 视为文本 PDF"）。"""
    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _BoomEngine())
    exact = "1234567890" * 2                     # 20 字，等于 ocr_min_chars
    p = tmp_path / "exact.pdf"
    p.write_bytes(_minimal_pdf([exact]))
    tuned = dataclasses.replace(settings, enable_ocr=True, ocr_min_chars=20, ocr_provider="fake")
    assert exact in parse_document(str(p), tuned)


def test_pdf_render_failure_is_app_error(tmp_path: Path, ocr_settings, monkeypatch):
    """fitz.open 失败（损坏文件）→ AppError(400)，不是底层异常。"""
    module = types.ModuleType("fitz")

    def _boom(path: str):
        raise RuntimeError("cannot open broken document")

    module.open = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", module)
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf([""]))
    with pytest.raises(AppError) as exc:
        parse_document(str(p), ocr_settings)
    assert exc.value.status_code == 400
    assert "渲染失败" in exc.value.detail


# ---------- 引擎异常不得被吞 ----------

def test_image_engine_error_propagates(tmp_path: Path, ocr_settings, monkeypatch):
    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _BoomEngine())
    p = _write_image(tmp_path)
    with pytest.raises(AppError) as exc:
        parse_document(str(p), ocr_settings)
    assert "模拟失败" in exc.value.detail, "引擎异常必须原样冒泡"


def test_pdf_engine_error_propagates(tmp_path: Path, ocr_settings, monkeypatch):
    _install_fake_fitz(monkeypatch, n_pages=1)
    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _BoomEngine())
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf([""]))
    with pytest.raises(AppError) as exc:
        parse_document(str(p), ocr_settings)
    assert "模拟失败" in exc.value.detail


def test_image_empty_result_is_not_silently_accepted(tmp_path: Path, ocr_settings, monkeypatch):
    """引擎返回空串 → 明确报错（否则上层只会看到"解析后内容为空"，排查困难）。"""

    class _EmptyEngine:
        def extract_text(self, image_bytes: bytes) -> str:
            return "   "

    monkeypatch.setattr(document_parser, "get_ocr_engine", lambda s: _EmptyEngine())
    p = _write_image(tmp_path)
    with pytest.raises(AppError) as exc:
        parse_document(str(p), ocr_settings)
    assert "未能从图片中识别出文本" in exc.value.detail


# ---------- 经 /upload 的端到端（确认解析器是被接口真正调用的） ----------

def _api_client(settings, monkeypatch):
    """用定制 Settings 起一个测试应用，并同步 `app.config.settings` 单例。

    IngestionService 目前不持有 settings，解析器在未显式传参时读的是全局单例；
    生产环境（.env 驱动）两者本就是同一个对象，这里对齐以模拟真实部署。
    """
    from fastapi.testclient import TestClient

    import app.config as app_config
    from app.factory import create_app

    monkeypatch.setattr(app_config, "settings", settings)
    return TestClient(create_app(settings))


def _upload_image(client, auth, payload: bytes, name: str = "扫描件.png"):
    return client.post("/upload", files={"file": (name, io.BytesIO(payload), "image/png")}, headers=auth)


def test_api_upload_image_with_fake_ocr_indexes(settings, fake_db, monkeypatch):
    ocr_on = dataclasses.replace(settings, enable_ocr=True, ocr_provider="fake")
    client = _api_client(ocr_on, monkeypatch)
    token = client.post("/auth/register", json={"username": "ocr", "password": "test123456"}).json()["token"]

    resp = _upload_image(client, {"Authorization": f"Bearer {token}"}, b"\x89PNG\r\n\x1a\n" + b"x" * 40)
    assert resp.status_code == 200, resp.text
    assert resp.json()["chunk_count"] >= 1


def test_api_upload_image_without_ocr_flag_is_400(settings, fake_db, monkeypatch):
    off = dataclasses.replace(settings, enable_ocr=False)
    client = _api_client(off, monkeypatch)
    token = client.post("/auth/register", json={"username": "noocr", "password": "test123456"}).json()["token"]

    resp = _upload_image(client, {"Authorization": f"Bearer {token}"}, b"\x89PNG\r\n\x1a\n" + b"x" * 40)
    assert resp.status_code == 400
    assert "ENABLE_OCR" in resp.text


# ---------- 真实引擎（需本机安装 + RUN_INTEGRATION=1） ----------
# 本机未安装 rapidocr-onnxruntime / pymupdf，默认跳过；装好后用 RUN_INTEGRATION=1 打开。

_1X1_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b4944415478da636000020000050001e9fadcd80000000049454e44ae426082"
)


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="需要本机安装 pymupdf")
def test_real_pymupdf_renders_pdf_page_to_png(tmp_path: Path):
    """真 PyMuPDF 渲染路径：每页都能渲染出 PNG 字节（假 fitz 无法验证这一点）。"""
    p = tmp_path / "scan.pdf"
    p.write_bytes(_minimal_pdf([""]))
    rendered = document_parser._render_pdf_pages(p, has_text_layer=True)
    assert rendered and rendered[0].startswith(b"\x89PNG")


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="需要本机安装 rapidocr-onnxruntime")
def test_real_rapidocr_engine_returns_text():
    """真 rapidocr 路径：空白小图识别为空串而不是抛异常。"""
    result = RapidOcrEngine().extract_text(_1X1_PNG)
    assert isinstance(result, str)
