"""FastAPI 应用入口：`uvicorn app.main:app`。

create_app 定义在 `app.factory`，这里只负责在模块级构造一个实例供 uvicorn 使用。
测试请 `from app.factory import create_app`，以免 import 本模块时连带加载真实容器。
"""
from __future__ import annotations

from app.factory import create_app  # noqa: F401  （对外保持 create_app 可用）

app = create_app()
