"""应用配置：统一从环境变量 / .env 读取。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _path(key: str, default: str) -> str:
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / p
    return str(p)


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_name: str = _env("APP_NAME", "企业级 RAG + Agent 智能知识库系统")

    # LLM（OpenAI 兼容，默认 DeepSeek）
    llm_provider: str = _env("LLM_PROVIDER", "openai")  # openai | fake
    llm_api_base: str = _env("LLM_API_BASE", "https://api.deepseek.com/v1")
    llm_api_key: str = _env("LLM_API_KEY", "")
    llm_model: str = _env("LLM_MODEL", "deepseek-v4-flash")
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 1024)

    # Embedding（本地 sentence-transformers 默认；openai / hash 备选）
    embedding_provider: str = _env("EMBEDDING_PROVIDER", "sentence-transformers")
    embedding_api_base: str = _env("EMBEDDING_API_BASE", "https://api.siliconflow.cn/v1")
    embedding_api_key: str = _env("EMBEDDING_API_KEY", "")
    embedding_model: str = _env("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    hash_embedding_dim: int = _int("HASH_EMBEDDING_DIM", 384)

    # 向量库
    vector_store: str = _env("VECTOR_STORE", "chroma")
    chroma_persist_dir: str = _path("CHROMA_PERSIST_DIR", str(BASE_DIR / "data" / "chroma_db"))

    # MySQL
    mysql_host: str = _env("MYSQL_HOST", "127.0.0.1")
    mysql_port: int = _int("MYSQL_PORT", 3306)
    mysql_user: str = _env("MYSQL_USER", "root")
    mysql_password: str = _env("MYSQL_PASSWORD", "")
    mysql_db: str = _env("MYSQL_DB", "smart_kb")

    # 鉴权
    jwt_secret: str = _env("JWT_SECRET", "change-me")
    jwt_expire_minutes: int = _int("JWT_EXPIRE_MINUTES", 720)

    # 数据目录
    data_dir: str = _path("DATA_DIR", str(BASE_DIR / "data"))
    documents_dir: str = _path("DOCUMENTS_DIR", str(BASE_DIR / "data" / "documents"))

    # 切分与检索
    chunk_size: int = _int("CHUNK_SIZE", 512)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 50)
    top_k: int = _int("TOP_K", 5)
    rerank_top_k: int = _int("RERANK_TOP_K", 3)
    enable_rerank: bool = _bool("ENABLE_RERANK", False)
    rerank_model: str = _env("RERANK_MODEL", "BAAI/bge-reranker-base")
    max_history_messages: int = _int("MAX_HISTORY_MESSAGES", 6)

    # Agent
    agent_max_iterations: int = _int("AGENT_MAX_ITERATIONS", 5)


settings = Settings()