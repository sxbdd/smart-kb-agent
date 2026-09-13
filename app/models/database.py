"""MySQL 数据访问：users / documents / conversations / messages / evaluation_runs。"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    username      VARCHAR(50) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

CREATE TABLE IF NOT EXISTS documents (
    id           VARCHAR(64) NOT NULL,
    filename     VARCHAR(255) NOT NULL,
    file_type    VARCHAR(20) NOT NULL,
    file_size    BIGINT NOT NULL,
    chunk_count  INT NOT NULL,
    uploaded_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档元数据';

CREATE TABLE IF NOT EXISTS conversations (
    id         VARCHAR(64) NOT NULL,
    title      VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话';

CREATE TABLE IF NOT EXISTS messages (
    id              VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(64) NOT NULL,
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    sources         TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_messages_conversation (conversation_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='消息';

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id         VARCHAR(64) NOT NULL,
    metrics    JSON NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评测记录';
"""


def _new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, host: str, port: int, user: str, password: str, db: str) -> None:
        self._cfg = dict(host=host, port=port, user=user, password=password, database=db)
        self._conn: Optional[pymysql.connections.Connection] = None
        self._lock = threading.RLock()

    def _connect(self):
        return pymysql.connect(
            **self._cfg,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )

    def _ensure_conn(self):
        # 调用方必须已持有 self._lock
        if self._conn is None:
            self._conn = self._connect()
        else:
            try:
                self._conn.ping(reconnect=True)
            except Exception:
                self._conn = self._connect()
        return self._conn

    def init(self) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                for stmt in SCHEMA.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)

    # ---- users ----
    def create_user(self, username: str, password_hash: str) -> int:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username, password_hash) VALUES(%s, %s)",
                    (username, password_hash),
                )
                return cur.lastrowid

    def get_user_by_username(self, username: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                return cur.fetchone()

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                return cur.fetchone()

    # ---- documents ----
    def save_document(self, doc_id: str, filename: str, file_type: str, file_size: int, chunk_count: int) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents(id, filename, file_type, file_size, chunk_count) "
                    "VALUES(%s, %s, %s, %s, %s)",
                    (doc_id, filename, file_type, file_size, chunk_count),
                )

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM documents ORDER BY uploaded_at DESC")
                return cur.fetchall()

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM documents WHERE id = %s", (doc_id,))
                return cur.fetchone()

    def delete_document(self, doc_id: str) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))

    # ---- conversations ----
    def create_conversation(self, title: str = "") -> str:
        conv_id = _new_id()
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO conversations(id, title) VALUES(%s, %s)", (conv_id, title or ""))
        return conv_id

    def set_conversation_title(self, conv_id: str, title: str) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE conversations SET title = %s WHERE id = %s", (title, conv_id))

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.id, c.title, c.created_at, c.updated_at, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
                    "FROM conversations c ORDER BY c.updated_at DESC"
                )
                return cur.fetchall()

    def get_conversation(self, conv_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM conversations WHERE id = %s", (conv_id,))
                row = cur.fetchone()
            if row is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role, content, sources, created_at FROM messages "
                    "WHERE conversation_id = %s ORDER BY created_at ASC",
                    (conv_id,),
                )
                msgs = cur.fetchall()
        messages = []
        for m in msgs:
            item = {"role": m["role"], "content": m["content"]}
            if m["sources"]:
                try:
                    item["sources"] = json.loads(m["sources"])
                except json.JSONDecodeError:
                    item["sources"] = []
            messages.append(item)
        return {
            "conversation_id": conv_id,
            "title": row["title"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "messages": messages,
        }

    def delete_conversation(self, conv_id: str) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))

    # ---- messages ----
    def add_message(self, conv_id: str, role: str, content: str, sources: Optional[list[dict]] = None) -> str:
        msg_id = _new_id()
        sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages(id, conversation_id, role, content, sources) "
                    "VALUES(%s, %s, %s, %s, %s)",
                    (msg_id, conv_id, role, content, sources_json),
                )
        return msg_id

    # ---- evaluation_runs ----
    def save_evaluation_run(self, metrics: dict) -> str:
        run_id = _new_id()
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO evaluation_runs(id, metrics) VALUES(%s, %s)",
                    (run_id, json.dumps(metrics, ensure_ascii=False)),
                )
        return run_id

    def list_evaluation_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM evaluation_runs ORDER BY created_at DESC")
                return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None