"""MySQL 数据访问：users / documents / conversations / messages / evaluation_runs。

V2 多租户改造（见 `docs/v2-plan.md` §6）：

- **隔离强制点在 DAO**：所有读写方法都带 `tenant_id`，SQL 里必须出现 `tenant_id = %s`。
  禁止"先查出来再在服务层判断归属"——那种写法漏一处就是跨租户数据泄漏。
- `users` 的唯一键从 `uk_username(username)` 改为 **`uk_tenant_username(tenant_id, username)`**：
  不同租户可以重名。
- `messages` 表**不加** `tenant_id`：消息归属由会话决定，会话已按租户隔离。

存量库注意：`CREATE TABLE IF NOT EXISTS` **不会**改动已存在的表，因此老库必须走
Alembic `0002_multi_tenant`（见 `docs/deployment.md`），不能指望这里的 `init()` 补列。
"""
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
    tenant_id     VARCHAR(64) NOT NULL DEFAULT 'default',
    role          VARCHAR(16) NOT NULL DEFAULT 'user',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_tenant_username (tenant_id, username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

CREATE TABLE IF NOT EXISTS documents (
    id           VARCHAR(64) NOT NULL,
    filename     VARCHAR(255) NOT NULL,
    file_type    VARCHAR(20) NOT NULL,
    file_size    BIGINT NOT NULL,
    chunk_count  INT NOT NULL,
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'default',
    uploaded_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_documents_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档元数据';

CREATE TABLE IF NOT EXISTS conversations (
    id         VARCHAR(64) NOT NULL,
    title      VARCHAR(255) NOT NULL DEFAULT '',
    tenant_id  VARCHAR(64) NOT NULL DEFAULT 'default',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_conversations_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话';

CREATE TABLE IF NOT EXISTS messages (
    id              VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(64) NOT NULL,
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    sources         TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_messages_conversation (conversation_id, created_at),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='消息';

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id         VARCHAR(64) NOT NULL,
    metrics    JSON NOT NULL,
    tenant_id  VARCHAR(64) NOT NULL DEFAULT 'default',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_evaluation_runs_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评测记录';

CREATE TABLE IF NOT EXISTS invites (
    code       VARCHAR(64) NOT NULL,
    tenant_id  VARCHAR(64) NOT NULL DEFAULT 'default',
    role       VARCHAR(16) NOT NULL DEFAULT 'user',
    created_by BIGINT UNSIGNED NULL,
    max_uses   INT NOT NULL DEFAULT 1,
    used_count INT NOT NULL DEFAULT 0,
    expires_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code),
    KEY idx_invites_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邀请码';
"""

#: 老数据（升级前写入、没有 tenant_id 字段）统一归到这个租户
DEFAULT_TENANT = "default"


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
    def create_user(
        self,
        username: str,
        password_hash: str,
        tenant_id: str = DEFAULT_TENANT,
        role: str = "user",
    ) -> int:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(username, password_hash, tenant_id, role) VALUES(%s, %s, %s, %s)",
                    (username, password_hash, tenant_id, role),
                )
                return cur.lastrowid

    def get_user_by_username(self, username: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict[str, Any]]:
        """按「租户 + 用户名」查用户——同一租户内用户名唯一，跨租户可重名。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM users WHERE username = %s AND tenant_id = %s",
                    (username, tenant_id),
                )
                return cur.fetchone()

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        """主键查询（唯一），返回行内含 tenant_id / role。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                return cur.fetchone()

    def list_users(self, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
        """列出**本租户**用户（供 `GET /admin/users`）。

        返回键名与 `app/models/schemas.py::UserInfo` 对齐，`created_at` 统一转成字符串
        （与 `get_conversation()` 的处理方式一致，避免路由层各写一遍格式化）。
        """
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, role, tenant_id, created_at FROM users "
                    "WHERE tenant_id = %s ORDER BY id ASC",
                    (tenant_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "user_id": int(r["id"]),
                "username": r["username"],
                "role": r["role"],
                "tenant_id": r["tenant_id"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    # ---- documents ----
    def save_document(
        self,
        doc_id: str,
        filename: str,
        file_type: str,
        file_size: int,
        chunk_count: int,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents(id, filename, file_type, file_size, chunk_count, tenant_id) "
                    "VALUES(%s, %s, %s, %s, %s, %s)",
                    (doc_id, filename, file_type, file_size, chunk_count, tenant_id),
                )

    def list_documents(self, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM documents WHERE tenant_id = %s ORDER BY uploaded_at DESC",
                    (tenant_id,),
                )
                return cur.fetchall()

    def get_document(self, doc_id: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM documents WHERE id = %s AND tenant_id = %s",
                    (doc_id, tenant_id),
                )
                return cur.fetchone()

    def delete_document(self, doc_id: str, tenant_id: str = DEFAULT_TENANT) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM documents WHERE id = %s AND tenant_id = %s",
                    (doc_id, tenant_id),
                )

    def delete_all_documents(self, tenant_id: str = DEFAULT_TENANT) -> int:
        """清空**本租户**的文档元数据（重建知识库时使用），返回删除行数。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM documents WHERE tenant_id = %s", (tenant_id,))
                return cur.rowcount

    # ---- conversations ----
    def create_conversation(self, title: str = "", tenant_id: str = DEFAULT_TENANT) -> str:
        conv_id = _new_id()
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO conversations(id, title, tenant_id) VALUES(%s, %s, %s)",
                    (conv_id, title or "", tenant_id),
                )
        return conv_id

    def set_conversation_title(self, conv_id: str, title: str, tenant_id: str = DEFAULT_TENANT) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE conversations SET title = %s WHERE id = %s AND tenant_id = %s",
                    (title, conv_id, tenant_id),
                )

    def list_conversations(self, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.id, c.title, c.created_at, c.updated_at, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
                    "FROM conversations c WHERE c.tenant_id = %s ORDER BY c.updated_at DESC",
                    (tenant_id,),
                )
                return cur.fetchall()

    def get_conversation(self, conv_id: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM conversations WHERE id = %s AND tenant_id = %s",
                    (conv_id, tenant_id),
                )
                row = cur.fetchone()
            if row is None:
                # 不属于本租户的会话一律按"不存在"处理，避免暴露他人会话是否存在
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

    def delete_conversation(self, conv_id: str, tenant_id: str = DEFAULT_TENANT) -> None:
        """删除本租户的会话及其全部消息。

        两个细节：

        1. 先显式删消息：老部署的 messages 表可能没有 fk_messages_conversation 外键
           （CREATE TABLE IF NOT EXISTS 不会改已存在的表），只删父表会留下孤儿消息
           （历史 bug，实测残留 2 条，见 docs/review-v1-audit.md §2.4）。
        2. 删消息时用 JOIN conversations 再带一次 tenant_id —— 保证**任何情况下**
           都不会删到别的租户会话下的消息，而不是依赖"调用方一定先校验过归属"。
        """
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE m FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE m.conversation_id = %s AND c.tenant_id = %s",
                    (conv_id, tenant_id),
                )
                cur.execute(
                    "DELETE FROM conversations WHERE id = %s AND tenant_id = %s",
                    (conv_id, tenant_id),
                )

    def count_orphan_messages(self) -> int:
        """统计没有对应会话的消息（运维排查用，跨全部租户）。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM messages m "
                    "LEFT JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.id IS NULL"
                )
                return int(cur.fetchone()["c"])

    def purge_orphan_messages(self) -> int:
        """清理历史遗留的孤儿消息，返回删除条数（运维操作，跨全部租户）。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE m FROM messages m "
                    "LEFT JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.id IS NULL"
                )
                return cur.rowcount

    # ---- invites（邀请码准入）----
    def create_invite(
        self,
        code: str,
        tenant_id: str = DEFAULT_TENANT,
        role: str = "user",
        created_by: Optional[int] = None,
        max_uses: int = 1,
        expires_in_hours: int = 0,
    ) -> None:
        """签发邀请码。`max_uses=0` 表示不限次数，`expires_in_hours=0` 表示永不过期。

        **过期时间由 MySQL 自己算**（`DATE_ADD(NOW(), INTERVAL n HOUR)`），不传 Python 的
        `datetime`：`consume_invite()` 用的是 `NOW()` 做比较，如果这里由应用进程生成时间戳，
        一旦应用与数据库时区不一致（本地开发很常见），有效期就会有整小时的偏差。
        """
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                if expires_in_hours and expires_in_hours > 0:
                    cur.execute(
                        "INSERT INTO invites(code, tenant_id, role, created_by, max_uses, used_count, expires_at) "
                        "VALUES(%s, %s, %s, %s, %s, 0, DATE_ADD(NOW(), INTERVAL %s HOUR))",
                        (code, tenant_id, role, created_by, max_uses, expires_in_hours),
                    )
                else:
                    cur.execute(
                        "INSERT INTO invites(code, tenant_id, role, created_by, max_uses, used_count, expires_at) "
                        "VALUES(%s, %s, %s, %s, %s, 0, NULL)",
                        (code, tenant_id, role, created_by, max_uses),
                    )

    def get_invite_by_code(self, code: str) -> Optional[dict[str, Any]]:
        """**按码全局查**（不带 tenant_id）。

        为什么允许这样查：注册时还不知道用户属于哪个租户 —— 租户正是由邀请码决定的。
        `code` 是主键（全局唯一），所以按码查不存在歧义，也不会泄漏其它租户的信息
        （调用方是注册流程，拿到的是"这个码对应哪个租户"，本来就是它该知道的）。
        管理面的列表/删除仍然严格按 `tenant_id` 过滤，见 `list_invites` / `delete_invite`。
        """
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM invites WHERE code = %s", (code,))
                return cur.fetchone()

    def get_invite(self, code: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM invites WHERE code = %s AND tenant_id = %s",
                    (code, tenant_id),
                )
                return cur.fetchone()

    def list_invites(self, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
        """列出**本租户**的邀请码（不返回任何其它租户的码）。"""
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT code, tenant_id, role, created_by, max_uses, used_count, expires_at, created_at "
                    "FROM invites WHERE tenant_id = %s ORDER BY created_at DESC",
                    (tenant_id,),
                )
                return cur.fetchall()

    def delete_invite(self, code: str, tenant_id: str = DEFAULT_TENANT) -> None:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM invites WHERE code = %s AND tenant_id = %s",
                    (code, tenant_id),
                )

    def consume_invite(self, code: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict[str, Any]]:
        """原子地"占用一次"邀请码：成功返回该码所在行，失败（不存在/已用尽/已过期）返回 None。

        为什么要用一条 `UPDATE` 当闸门，而不是"先 SELECT 判断再 UPDATE"：
        并发注册时两个请求可能同时读到 `used_count=0`、都判定"还能用"，
        于是一次性邀请码被用掉两次。把**校验条件写进 UPDATE 的 WHERE**，
        由数据库保证只有一行被扣减，`rowcount != 1` 即表示没抢到。

        `tenant_id` 也参与匹配：管理员只能消费**自己租户**的邀请码。
        """
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE invites SET used_count = used_count + 1 "
                    "WHERE code = %s AND tenant_id = %s "
                    "  AND (max_uses = 0 OR used_count < max_uses) "
                    "  AND (expires_at IS NULL OR expires_at > NOW())",
                    (code, tenant_id),
                )
                if cur.rowcount != 1:
                    return None
                cur.execute(
                    "SELECT * FROM invites WHERE code = %s AND tenant_id = %s",
                    (code, tenant_id),
                )
                return cur.fetchone()

    # ---- messages ----
    def add_message(self, conv_id: str, role: str, content: str, sources: Optional[list[dict]] = None) -> str:
        """追加消息。**不带 tenant_id**：消息归属由会话决定，会话已按租户隔离。"""
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
    def save_evaluation_run(self, metrics: dict, tenant_id: str = DEFAULT_TENANT) -> str:
        run_id = _new_id()
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO evaluation_runs(id, metrics, tenant_id) VALUES(%s, %s, %s)",
                    (run_id, json.dumps(metrics, ensure_ascii=False), tenant_id),
                )
        return run_id

    def list_evaluation_runs(self, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM evaluation_runs WHERE tenant_id = %s ORDER BY created_at DESC",
                    (tenant_id,),
                )
                return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
