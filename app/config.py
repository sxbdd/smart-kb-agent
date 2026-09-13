"""应用配置：统一从环境变量 / .env 读取。

设计要点：
- 所有字段用 `default_factory` 读取环境变量，**在实例化时**取值，
  而不是在类定义（= import）时固化。这样测试可以用 monkeypatch 换一套环境
  再 new 一个 Settings，无需依赖 import 顺序（历史坑见 docs/review-v1-audit.md §2.3）。
- JWT_SECRET 缺失或仍为占位值时**直接拒绝构造**（fail-fast），
  避免静默降级成"任何人可伪造 token"。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# 视为"未配置"的 JWT 占位值
_JWT_PLACEHOLDERS = {"", "change-me", "changeme", "secret", "your-secret-key"}


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


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "企业级 RAG + Agent 智能知识库系统"))

    # LLM（OpenAI 兼容，默认 DeepSeek）
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai"))  # openai | fake
    llm_api_base: str = field(default_factory=lambda: _env("LLM_API_BASE", "https://api.deepseek.com/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-v4-flash"))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 4096))

    # Embedding（本地 sentence-transformers 默认；openai / hash 备选）
    embedding_provider: str = field(default_factory=lambda: _env("EMBEDDING_PROVIDER", "sentence-transformers"))
    embedding_api_base: str = field(default_factory=lambda: _env("EMBEDDING_API_BASE", "https://api.siliconflow.cn/v1"))
    embedding_api_key: str = field(default_factory=lambda: _env("EMBEDDING_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    hash_embedding_dim: int = field(default_factory=lambda: _int("HASH_EMBEDDING_DIM", 384))
    # 本地模型已缓存时自动置 HF_HUB_OFFLINE，避免每次启动都往返 HuggingFace（冷启动 13.8s → <3s）
    hf_offline_auto: bool = field(default_factory=lambda: _bool("HF_OFFLINE_AUTO", True))

    # 向量库
    vector_store: str = field(default_factory=lambda: _env("VECTOR_STORE", "chroma"))
    chroma_persist_dir: str = field(
        default_factory=lambda: _path("CHROMA_PERSIST_DIR", str(BASE_DIR / "data" / "chroma_db"))
    )

    # MySQL
    mysql_host: str = field(default_factory=lambda: _env("MYSQL_HOST", "127.0.0.1"))
    mysql_port: int = field(default_factory=lambda: _int("MYSQL_PORT", 3306))
    mysql_user: str = field(default_factory=lambda: _env("MYSQL_USER", "root"))
    mysql_password: str = field(default_factory=lambda: _env("MYSQL_PASSWORD", ""))
    mysql_db: str = field(default_factory=lambda: _env("MYSQL_DB", "smart_kb"))

    # 鉴权
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", ""))
    jwt_expire_minutes: int = field(default_factory=lambda: _int("JWT_EXPIRE_MINUTES", 720))
    auth_rate_limit_per_minute: int = field(default_factory=lambda: _int("AUTH_RATE_LIMIT_PER_MINUTE", 10))

    # 数据目录
    data_dir: str = field(default_factory=lambda: _path("DATA_DIR", str(BASE_DIR / "data")))
    documents_dir: str = field(default_factory=lambda: _path("DOCUMENTS_DIR", str(BASE_DIR / "data" / "documents")))

    # 切分与检索
    chunk_size: int = field(default_factory=lambda: _int("CHUNK_SIZE", 512))
    chunk_overlap: int = field(default_factory=lambda: _int("CHUNK_OVERLAP", 50))
    top_k: int = field(default_factory=lambda: _int("TOP_K", 5))
    rerank_top_k: int = field(default_factory=lambda: _int("RERANK_TOP_K", 3))
    enable_rerank: bool = field(default_factory=lambda: _bool("ENABLE_RERANK", False))
    rerank_model: str = field(default_factory=lambda: _env("RERANK_MODEL", "BAAI/bge-reranker-base"))
    max_history_messages: int = field(default_factory=lambda: _int("MAX_HISTORY_MESSAGES", 6))

    # 请求边界（防成本 / 内存放大）
    max_question_chars: int = field(default_factory=lambda: _int("MAX_QUESTION_CHARS", 2000))
    max_top_k: int = field(default_factory=lambda: _int("MAX_TOP_K", 20))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 20))

    # Agent
    agent_max_iterations: int = field(default_factory=lambda: _int("AGENT_MAX_ITERATIONS", 5))

    # Router：规则判不出时是否调用 LLM 分类（默认关，省调用）
    router_enable_llm: bool = field(default_factory=lambda: _bool("ROUTER_ENABLE_LLM", False))

    # ---------------- V2 ----------------
    # 文档解析扩展：Excel / CSV / OCR（扫描件）
    enable_ocr: bool = field(default_factory=lambda: _bool("ENABLE_OCR", False))
    ocr_provider: str = field(default_factory=lambda: _env("OCR_PROVIDER", "fake"))  # rapidocr | fake | none
    ocr_lang: str = field(default_factory=lambda: _env("OCR_LANG", "ch"))
    ocr_min_chars: int = field(default_factory=lambda: _int("OCR_MIN_CHARS", 20))

    # 流式输出（SSE）
    enable_stream: bool = field(default_factory=lambda: _bool("ENABLE_STREAM", True))
    stream_timeout_s: int = field(default_factory=lambda: _int("STREAM_TIMEOUT_S", 180))

    # 检索相似度阈值：0 表示不启用（保持 V1 行为）
    min_score: float = field(default_factory=lambda: _float("MIN_SCORE", 0.0))

    # 限流后端：memory（进程内）| redis（跨副本共享，不可用时回退 memory）
    rate_limit_backend: str = field(default_factory=lambda: _env("RATE_LIMIT_BACKEND", "memory"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://127.0.0.1:6379/0"))

    # 多租户与 RBAC
    default_tenant: str = field(default_factory=lambda: _env("DEFAULT_TENANT", "default"))
    allow_self_register: bool = field(default_factory=lambda: _bool("ALLOW_SELF_REGISTER", True))
    # 指定该用户名在首次注册时自动成为所在租户的管理员（留空则不设）
    bootstrap_admin_username: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_USERNAME", ""))
    #: 注册是否必须持邀请码。
    #: 开启后只有持有 admin 签发的邀请码才能注册，且租户与角色**由邀请码决定**，
    #: 注册请求里的 `tenant` 字段一律忽略 —— 否则任何人都能自选租户，
    #: 隔离强制点做得再严也没有意义（这就是 V2「演示级准入」的缺口）。
    #:
    #: 代码里默认 **false**，是为了**不打断已在运行的部署**：存量库里所有账号都是免码注册的，
    #: 一旦默认开启就没人能再注册，而管理员此时可能还没有可用的邀请码。
    #: `.env.example` 给的是 `true` —— 新部署照抄即可，生产环境必须开启。
    require_invite: bool = field(default_factory=lambda: _bool("REQUIRE_INVITE", False))
    #: 邀请码默认有效期（小时）；0 表示永不过期
    invite_ttl_hours: int = field(default_factory=lambda: _int("INVITE_TTL_HOURS", 0))
    #: 邀请码默认可用次数；0 表示不限次数
    invite_default_max_uses: int = field(default_factory=lambda: _int("INVITE_DEFAULT_MAX_USES", 1))

    # MCP server（把知识库暴露给支持 MCP 的客户端）
    enable_mcp: bool = field(default_factory=lambda: _bool("ENABLE_MCP", True))
    mcp_server_name: str = field(default_factory=lambda: _env("MCP_SERVER_NAME", "smart-kb-agent"))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def __post_init__(self) -> None:
        if self.jwt_secret.strip().lower() in _JWT_PLACEHOLDERS:
            raise ValueError(
                "JWT_SECRET 未配置或仍为占位值（change-me 等），拒绝启动。\n"
                "请在 .env 中设置一个强随机值，例如：\n"
                "  JWT_SECRET=<64 位十六进制>\n"
                "可用以下命令生成：python -c \"import secrets;print(secrets.token_hex(32))\""
            )


settings = Settings()
