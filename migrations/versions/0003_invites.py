"""邀请码表：把多租户准入从"注册时自报租户"升级为"持码注册"。

Revision ID: 0003_invites
Revises: 0002_multi_tenant
Create Date: 2026-01-03 00:00:00

**要解决的问题**：V2 的隔离强制点（DAO 带 tenant_id + 向量库 metadata 过滤）是对的，
但"用户属于哪个租户"由**注册请求的 body 决定** —— 任何人都能注册进任意租户，
隔离再严也没有意义。本表让 admin 签发邀请码，注册时：
租户与角色**由邀请码决定**，请求里的 `tenant` 字段一律忽略。

字段语义（与 `app/models/database.py::SCHEMA` 逐列一致）：

| 列 | 说明 |
| --- | --- |
| `code` | 邀请码本体（主键），URL-safe 随机串 |
| `tenant_id` | 该码只能注册进这个租户（管理员只能签发本租户的码） |
| `role` | 该码注册出来的角色，便于邀请"只读账号" |
| `created_by` | 签发人 user id（可空：允许脚本/运维直接插库） |
| `max_uses` | 最大可用次数；**0 表示不限次数** |
| `used_count` | 已用次数；消费靠一条带条件的 `UPDATE` 原子扣减，避免并发超发 |
| `expires_at` | 过期时间；`NULL` 表示永不过期 |
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0003_invites"
down_revision = "0002_multi_tenant"
branch_labels = None
depends_on = None

_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_default_charset": "utf8mb4"}


def _invites_table_exists(bind) -> bool:  # noqa: ANN001
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'invites'"
        )
    ).fetchone()
    return bool(row) and int(row[0]) > 0


def upgrade() -> None:
    # **幂等**：本项目里建表有两个入口 —— 运行时的 `db.init()`（执行 `SCHEMA`，全是
    # `CREATE TABLE IF NOT EXISTS`）和 Alembic 迁移。应用只要启动过一次，`invites` 就已经
    # 被 `init()` 建出来了；此时如果无条件 `create_table`，upgrade 会直接报
    # `1050 Table 'invites' already exists`（真机踩过：verify_streaming.py 用真实配置起过一次服务）。
    #
    # 所以这里做存在性判断：**仅在连库执行时**跳过（离线 `--sql` 渲染看不到库，
    # 必须假定它不存在才能把完整 DDL 渲染出来，供 `tests/test_migrations.py` 与人工审查使用）。
    # 两个入口的 DDL 都来自同一份定义（本文件与 `database.py::SCHEMA` 逐列一致，有测试守住），
    # 因此"跳过"不会造成结构漂移。
    ctx = op.get_context()
    if not getattr(ctx, "as_sql", False) and _invites_table_exists(op.get_bind()):
        return

    op.create_table(
        "invites",
        sa.Column("code", mysql.VARCHAR(64), nullable=False),
        sa.Column("tenant_id", mysql.VARCHAR(64), nullable=False, server_default=sa.text("'default'")),
        sa.Column("role", mysql.VARCHAR(16), nullable=False, server_default=sa.text("'user'")),
        sa.Column("created_by", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("max_uses", mysql.INTEGER(), nullable=False, server_default=sa.text("1")),
        sa.Column("used_count", mysql.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", mysql.DATETIME(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("code"),
        mysql_comment="邀请码",
        **_TABLE_OPTS,
    )
    op.create_index("idx_invites_tenant", "invites", ["tenant_id"])


def downgrade() -> None:
    # 先删索引再删表。invites 没有任何外键依赖它的索引，所以这里显式 DROP INDEX 是安全的
    # （messages 的 idx_messages_conversation 就不行：外键依赖它会报 errno 1553）。
    op.drop_index("idx_invites_tenant", table_name="invites")
    op.drop_table("invites")
