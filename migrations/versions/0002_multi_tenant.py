"""多租户与角色：users/documents/conversations/evaluation_runs 增加 tenant_id。

Revision ID: 0002_multi_tenant
Revises: 0001_initial
Create Date: 2026-01-02 00:00:00

为什么必须用迁移而不是 `CREATE TABLE IF NOT EXISTS`：
`IF NOT EXISTS` 对**已存在**的表什么都不做，所以存量库不会自动长出新列
（这正是本项目踩过的坑，见 docs/review-v1-audit.md §2.4 的外键缺失）。

本 revision 做四件事：

1. `users`：加 `tenant_id` / `role`；唯一键 `uk_username(username)` →
   **`uk_tenant_username(tenant_id, username)`**（不同租户可重名）。
2. `documents` / `conversations` / `evaluation_runs`：加 `tenant_id` + 租户索引。
3. **存量数据回填**：`ADD COLUMN ... NOT NULL DEFAULT 'default'` 会让既有行自动落到
   `default` 租户；`role` 默认 `user`。
4. `messages` 的 `fk_messages_conversation` 外键**按需补齐**（存量库通常没有）。
   这一步只在**连库执行**时判断，离线 `--sql` 渲染里不出现 —— 因为新建库走完
   `0001_initial` 时外键已经存在，无条件 ADD CONSTRAINT 会直接报错。

注意：`role` 的默认值是 `user`，所以**升级完存量库后没有任何 admin**。
首个管理员请二选一（见 docs/deployment.md §9）：
用 `BOOTSTRAP_ADMIN_USERNAME` 注册一个新账号，或手工
`UPDATE users SET role='admin' WHERE username='<你的账号>' AND tenant_id='default';`
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0002_multi_tenant"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

#: 租户列的定义（与 app/models/database.py::SCHEMA 逐列一致）
_TENANT_COLUMN = mysql.VARCHAR(64)
_TENANT_DEFAULT = sa.text("'default'")
_ROLE_COLUMN = mysql.VARCHAR(16)
_ROLE_DEFAULT = sa.text("'user'")

#: 表名 -> 租户索引名
_TENANT_INDEXES = {
    "documents": "idx_documents_tenant",
    "conversations": "idx_conversations_tenant",
    "evaluation_runs": "idx_evaluation_runs_tenant",
}


def _messages_fk_exists(bind) -> bool:  # noqa: ANN001
    """检查 messages 表上是否已有 fk_messages_conversation。"""
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages' "
            "AND CONSTRAINT_NAME = 'fk_messages_conversation'"
        )
    ).fetchone()
    return bool(row) and int(row[0]) > 0


def upgrade() -> None:
    # ---- 1) users：租户 + 角色 + 唯一键改造 ----
    op.add_column("users", sa.Column("tenant_id", _TENANT_COLUMN, nullable=False, server_default=_TENANT_DEFAULT))
    op.add_column("users", sa.Column("role", _ROLE_COLUMN, nullable=False, server_default=_ROLE_DEFAULT))
    op.drop_constraint("uk_username", "users", type_="unique")
    op.create_unique_constraint("uk_tenant_username", "users", ["tenant_id", "username"])

    # ---- 2) 其余业务表：租户列 + 租户索引 ----
    op.add_column(
        "documents",
        sa.Column("tenant_id", _TENANT_COLUMN, nullable=False, server_default=_TENANT_DEFAULT),
    )
    op.add_column(
        "conversations",
        sa.Column("tenant_id", _TENANT_COLUMN, nullable=False, server_default=_TENANT_DEFAULT),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column("tenant_id", _TENANT_COLUMN, nullable=False, server_default=_TENANT_DEFAULT),
    )
    for table, index_name in _TENANT_INDEXES.items():
        op.create_index(index_name, table, ["tenant_id"])

    # ---- 3) messages 外键按需补齐（仅连库执行时判断）----
    ctx = op.get_context()
    if not getattr(ctx, "as_sql", False):
        bind = op.get_bind()
        if not _messages_fk_exists(bind):
            op.create_foreign_key(
                "fk_messages_conversation",
                "messages",
                "conversations",
                ["conversation_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    """回滚到 0001_initial。

    注意：先删租户索引/唯一键再删列；重建 `uk_username(username)` 时如果**不同租户存在
    同名用户**，MySQL 会因唯一键冲突而失败 —— 这是回滚的固有风险，需要先自行清理重名账号。
    """
    ctx = op.get_context()
    if not getattr(ctx, "as_sql", False):
        bind = op.get_bind()
        if _messages_fk_exists(bind):
            op.drop_constraint("fk_messages_conversation", "messages", type_="foreignkey")

    for table, index_name in _TENANT_INDEXES.items():
        op.drop_index(index_name, table_name=table)

    op.drop_constraint("uk_tenant_username", "users", type_="unique")
    op.create_unique_constraint("uk_username", "users", ["username"])

    op.drop_column("evaluation_runs", "tenant_id")
    op.drop_column("conversations", "tenant_id")
    op.drop_column("documents", "tenant_id")
    op.drop_column("users", "role")
    op.drop_column("users", "tenant_id")
