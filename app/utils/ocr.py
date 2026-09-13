"""OCR 引擎插件层：协议 + 三种实现 + 工厂。

解决的问题：扫描件（图片 / 无文本层 PDF）必须靠 OCR 才能进知识库，但 OCR 依赖是
**重量级且可选**的（rapidocr + onnxruntime + opencv，模型几百 MB），而本项目要求
"离线可测、依赖缺失时优雅降级"。因此这里把引擎抽象成 `OcrEngine` 协议：

- `FakeOcrEngine`  —— 确定性输出，让解析链路可以在 CI/离线环境下完整被测；
- `RapidOcrEngine` —— 真实本地引擎，**惰性 import**，未安装时给出可操作的 `AppError`；
- `NoopOcrEngine`  —— 显式关闭（`OCR_PROVIDER=none`），始终返回空串。

解析层只依赖协议，不依赖任何具体引擎，因此新增引擎（百度云 / PaddleOCR …）无需改解析代码。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.utils.exceptions import AppError

#: 支持扫描件解析的图片后缀（解析层与引擎层共用，避免两处维护）
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"})

#: 合法的 OCR_PROVIDER 取值
KNOWN_PROVIDERS = ("fake", "rapidocr", "none")


@runtime_checkable
class OcrEngine(Protocol):
    """OCR 引擎协议：图片字节 → 文本（识别不出内容时返回空串，不要抛异常）。"""

    def extract_text(self, image_bytes: bytes) -> str:  # pragma: no cover - 协议声明
        ...


class FakeOcrEngine:
    """确定性假引擎：输出"固定前缀 + 字节长度"，供离线测试断言。

    之所以用字节长度而不是固定串：测试可以据此验证"每页各识别一次、页序不乱"，
    同时保证同样的输入永远得到同样的输出（无随机性、无外部依赖）。
    """

    PREFIX = "OCR_FAKE"

    def __init__(self, prefix: str | None = None) -> None:
        self.prefix = prefix or self.PREFIX

    def extract_text(self, image_bytes: bytes) -> str:
        if not image_bytes:
            return ""
        return f"{self.prefix}:{len(image_bytes)}"


class RapidOcrEngine:
    """基于 rapidocr_onnxruntime 的本地离线 OCR（惰性加载，进程内复用）。

    "惰性"体现在两处：
    1. 构造函数**不** import 任何重依赖 —— 只配置 provider 不该拖慢启动；
    2. 真正的 `RapidOCR()` 初始化推迟到第一次 `extract_text`，并缓存在实例上，
       避免每张图片都重新加载模型（首次约 1~3s，之后每页几十 ms 量级）。
    """

    def __init__(self, lang: str = "ch") -> None:
        self.lang = lang or "ch"
        self._engine: Any = None

    def _get_engine(self) -> Any:
        """加载（并缓存）rapidocr 引擎；依赖缺失 / 初始化失败都给清晰提示。"""
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise AppError(
                "OCR 引擎不可用：未安装 rapidocr_onnxruntime。\n"
                "请执行 pip install rapidocr-onnxruntime（首次运行会自动下载模型，约几百 MB），"
                "或改用 OCR_PROVIDER=fake 做离线联调，或设 OCR_PROVIDER=none 关闭扫描件识别。",
                400,
            ) from exc
        try:
            self._engine = RapidOCR()
        except Exception as exc:  # 模型文件损坏 / onnxruntime 与平台不匹配等
            raise AppError(f"OCR 引擎初始化失败：{exc}", 400) from exc
        return self._engine

    def extract_text(self, image_bytes: bytes) -> str:
        if not image_bytes:
            return ""
        engine = self._get_engine()
        image = self._to_ndarray(image_bytes)
        try:
            result = engine(image)
        except Exception as exc:
            raise AppError(f"OCR 识别失败：{exc}", 400) from exc
        return self._join_result(result)

    @staticmethod
    def _to_ndarray(image_bytes: bytes) -> Any:
        """bytes → BGR ndarray。

        rapidocr 各版本对 bytes 入参的接受度不一致，统一在这里用 cv2 解码，
        把"入参形态"这件事收敛到一个地方（依赖随 rapidocr 一起安装）。
        """
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise AppError("OCR 引擎不可用：缺少 opencv-python / numpy，请随 rapidocr 一并安装", 400) from exc
        array = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise AppError("OCR 识别失败：无法解码图片内容", 400)
        return array

    @staticmethod
    def _join_result(result: Any) -> str:
        """rapidocr 返回 `(list[[box, text, score]], elapsed)` 或 `(None, elapsed)`。"""
        if not result:
            return ""
        boxes = result[0] if isinstance(result, tuple) else result
        if not boxes:
            return ""
        lines: list[str] = []
        for item in boxes:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
        return "\n".join(lines)


class NoopOcrEngine:
    """显式关闭的引擎：始终返回空串，调用方据此回退到已有文本。"""

    def extract_text(self, image_bytes: bytes) -> str:
        return ""


def get_ocr_engine(settings: Any) -> OcrEngine:
    """按 `settings.ocr_provider` 构造引擎；未知取值直接报错（配置错误不要静默降级）。"""
    provider = str(getattr(settings, "ocr_provider", "") or "").strip().lower()
    if provider == "fake":
        return FakeOcrEngine()
    if provider == "rapidocr":
        return RapidOcrEngine(lang=str(getattr(settings, "ocr_lang", "ch") or "ch"))
    if provider == "none":
        return NoopOcrEngine()
    raise AppError(
        f"未知的 OCR_PROVIDER：{provider!r}（可选 {' / '.join(KNOWN_PROVIDERS)}）",
        400,
    )
