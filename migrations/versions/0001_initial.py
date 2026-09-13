"""初始表结构：users / documents / conversations / messages / evaluation_runs。

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00

与 `app/models/database.py` 里手写的 `SCHEMA` 保持**逐列一致**：
列名、类型、可空性、默认值（含 conversations.updated_at 的 ON UPDATE）、
唯一键 uk_username、索引 idx_messages_conversation，以及
messages → conversations 的 ON DELETE CASCADE 外键。

注意：项目此前用 `CREATE TABLE IF NOT EXISTS` 建表，存量库可能存在**缺外键**的
messages 表（见 docs/review-v1-audit.md §2.4）。对这类库请先用 alembic stamp
标记当前版本、再按需手工补外键，不要直接 upgrade。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# 与 SCHEMA 保持一致的建表选项
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_default_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("username", mysql.VARCHAR(50), nullable=False),
        sa.Column("password_hash", mysql.VARCHAR(255), nullable=False),
        sa.Column("created_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uk_username"),
        mysql_comment="用户表",
        **_TABLE_OPTS,
    )

    op.create_table(
        "documents",
        sa.Column("id", mysql.VARCHAR(64), nullable=False),
        sa.Column("filename", mysql.VARCHAR(255), nullable=False),
        sa.Column("file_type", mysql.VARCHAR(20), nullable=False),
        sa.Column("file_size", mysql.BIGINT(), nullable=False),
        sa.Column("chunk_count", mysql.INTEGER(), nullable=False),
        sa.Column("uploaded_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        mysql_comment="文档元数据",
        **_TABLE_OPTS,
    )

    op.create_table(
        "conversations",
        sa.Column("id", mysql.VARCHAR(64), nullable=False),
        sa.Column("title", mysql.VARCHAR(255), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_comment="会话",
        **_TABLE_OPTS,
    )

    op.create_table(
        "messages",
        sa.Column("id", mysql.VARCHAR(64), nullable=False),
        sa.Column("conversation_id", mysql.VARCHAR(64), nullable=False),
        sa.Column("role", mysql.VARCHAR(20), nullable=False),
        sa.Column("content", mysql.TEXT(), nullable=False),
        sa.Column("sources", mysql.TEXT(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation",
            ondelete="CASCADE",
        ),
        mysql_comment="消息",
        **_TABLE_OPTS,
    )
    op.create_index("idx_messages_conversation", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", mysql.VARCHAR(64), nullable=False),
        sa.Column("metrics", mysql.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        mysql_comment="评测记录",
        **_TABLE_OPTS,
    )


def downgrade() -> None:
    """回滚：按依赖倒序删表（messages 的外键指向 conversations）。

    这里**不显式 drop_index**：idx_messages_conversation 是外键
    fk_messages_conversation 依赖的索引，MySQL 会直接报
    errno 1553「Cannot drop index ... needed in a foreign key constraint」。
    DROP TABLE 会连带清掉索引与外键，因此只删表即可（真库往返已验证）。
    """
    op.drop_table("evaluation_runs")
    op.drop_table("messages")
    op.drop_table("documents")
    op.drop_table("conversations")
    op.drop_table("users")
