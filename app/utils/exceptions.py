"""自定义异常与错误码约定。"""
from __future__ import annotations


class AppError(Exception):
    status_code = 400

    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = 404

    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(detail, 404)


class UnsupportedFileTypeError(AppError):
    status_code = 415

    def __init__(self, detail: str = "Unsupported file type") -> None:
        super().__init__(detail, 415)


class LLMError(AppError):
    status_code = 502

    def __init__(self, detail: str = "LLM call failed") -> None:
        super().__init__(detail, 502)
