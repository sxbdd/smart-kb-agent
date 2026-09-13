"""Alembic 运行环境：连接信息全部来自 app.config.settings（不硬编码凭据）。

用法::

    .venv\\Scripts\\python -m alembic upgrade head                     # 在线执行
    .venv\\Scripts\\python -m alembic upgrade head --sql               # 离线生成 SQL（不连库）
    .venv\\Scripts\\python -m alembic downgrade base                   # 回滚
    .venv\\Scripts\\python -m alembic -x db_url=mysql+pymysql://U:P@HOST:3306/DB upgrade head

优先级：``-x db_url=...`` > ``app.config.settings``（.env / 环境变量）。

设计说明：本项目的数据访问层是手写 SQL（``app/models/database.py`` 的 ``SCHEMA``），
没有 SQLAlchemy 声明式模型，因此 ``target_metadata`` 为 ``None``，迁移脚本手写 DDL，
不做 autogenerate 对比。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import create_engine, pool

# 保证从任意工作目录执行 `python -m alembic` 都能 import app.*
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logger = logging.getLogger("alembic.env")

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False：嵌入式（测试）调用时不把应用/ pytest 的
    # logging 配置清掉
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 手写 SQL 项目没有 SQLAlchemy 元数据可对比（见模块 docstring）
target_metadata = None

# app.config 导入失败的原因（缺 JWT_SECRET 等），延迟到真正需要时再告警
_settings_import_error: Exception | None = None
try:
    from app.config import settings as app_settings  # noqa: E402
except Exception as exc:  # pragma: no cover - 仅在 .env 不完整时触发
    app_settings = None  # type: ignore[assignment]
    _settings_import_error = exc


def _mysql_params() -> dict[str, str]:
    """取 MySQL 连接参数：优先 settings，不可用时退回环境变量。"""
    if app_settings is not None:
        return {
            "host": app_settings.mysql_host,
            "port": str(app_settings.mysql_port),
            "user": app_settings.mysql_user,
            "password": app_settings.mysql_password,
            "db": app_settings.mysql_db,
        }
    logger.warning("无法加载 app.config.settings（%s），改用环境变量里的 MySQL 连接信息", _settings_import_error)
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": os.getenv("MYSQL_PORT", "3306"),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "db": os.getenv("MYSQL_DB", "smart_kb"),
    }


def build_database_url() -> str:
    """按 settings 拼 MySQL URL；用户名/密码做 URL 编码（含 @ : / # 等特殊字符也不炸）。"""
    p = _mysql_params()
    return (
        f"mysql+pymysql://{quote_plus(p['user'])}:{quote_plus(p['password'])}"
        f"@{p['host']}:{p['port']}/{p['db']}?charset=utf8mb4"
    )


def get_database_url() -> str:
    """优先级：``-x db_url=...`` > settings / 环境变量。"""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        logger.info("使用 -x db_url 覆盖连接串")
        return override
    return build_database_url()


def run_migrations_offline() -> None:
    """离线模式：只渲染 SQL（``--sql``），不建立数据库连接。"""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：按 settings 建 Engine 并执行迁移。"""
    connectable = create_engine(get_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
