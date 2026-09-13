"""Alembic 迁移：离线 SQL 断言（不连库）+ 可选的真库往返。

离线优先：`alembic upgrade head --sql` 只渲染 SQL，不建立任何数据库连接；
真库 upgrade → downgrade 的用例标了 integration，且只在**临时库**里跑，
绝不会碰到业务库 smart_kb。
"""
from __future__ import annotations

import argparse
import io
import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.database import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"
VERSIONS_DIR = ROOT / "migrations" / "versions"
EXPECTED_TABLES = ("users", "documents", "conversations", "messages", "evaluation_runs", "invites")

# 建表语句里这些开头的行不是列定义
_CONSTRAINT_PREFIXES = (
    "primary key",
    "unique",
    "key ",
    "index ",
    "constraint ",
    "foreign key",
    "references",
    "on delete",
    "engine=",
    "default charset",
    "comment=",
)

# 表尾的类型规范化：MySQL 里 INT 与 INTEGER 等价
_END_OF_TYPE_MARKERS = (" not null", " null", " default", " auto_increment", " on update")


def _make_config(db_url: str | None = None, output_buffer: io.StringIO | None = None) -> Config:
    """构造 alembic Config；db_url 通过 -x db_url=... 传（与命令行一致）。"""
    cfg = Config(
        str(ALEMBIC_INI),
        output_buffer=output_buffer if output_buffer is not None else io.StringIO(),
    )
    if db_url:
        cfg.cmd_opts = argparse.Namespace(x=[f"db_url={db_url}"])
    return cfg


def _normalize_type(rest: str) -> str:
    """把一列的类型声明规范化：截掉约束部分、统一 INT/INTEGER 写法。"""
    for marker in _END_OF_TYPE_MARKERS:
        index = rest.lower().find(marker)
        if index != -1:
            rest = rest[:index]
    return re.sub(r"\bINT\b", "INTEGER", rest.upper().strip())


#: 0002_multi_tenant 之类的增量迁移用 ALTER TABLE ... ADD COLUMN 加列，
#: 只解析 CREATE TABLE 会漏掉这些列，从而把"迁移结果 == SCHEMA"这个不变量误判为不成立。
_ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE\s+`?([A-Za-z_]\w*)`?\s+ADD COLUMN\s+`?([A-Za-z_]\w*)`?\s+([^;]+);",
    re.IGNORECASE,
)


def _table_columns(ddl: str) -> dict[str, dict[str, str]]:
    """把 DDL 文本解析成 `表名 -> {列名: 规范化类型}`（按出现顺序）。

    同时处理 `CREATE TABLE` 与后续的 `ALTER TABLE ... ADD COLUMN`（增量迁移）。
    """
    blocks: dict[str, dict[str, str]] = {}
    for chunk in ddl.split("CREATE TABLE"):
        lines = [line.strip() for line in chunk.strip().splitlines()]
        if not lines:
            continue
        match = re.match(r"(?:IF NOT EXISTS\s+)?`?([A-Za-z_]\w*)`?\s*\($", lines[0])
        if match is None:
            continue
        columns: dict[str, str] = {}
        for line in lines[1:]:
            if line.startswith(")"):  # 表定义结束
                break
            line = line.rstrip(",").strip()
            if not line:
                continue
            lowered = line.lower()
            if any(lowered.startswith(prefix) for prefix in _CONSTRAINT_PREFIXES):
                continue
            name = line.split()[0].strip("`")
            rest = line[len(line.split()[0]):].strip()
            columns[name] = _normalize_type(rest)
        blocks[match.group(1)] = columns

    # 增量迁移的加列：追加到对应表的列定义里
    for add in _ADD_COLUMN_RE.finditer(ddl):
        table, name, declaration = add.group(1), add.group(2), add.group(3)
        if table in blocks:
            blocks[table][name] = _normalize_type(declaration)
    return blocks


@pytest.fixture(scope="module")
def head_sql() -> str:
    """`alembic upgrade head --sql` 的离线输出。"""
    cfg = _make_config()
    command.upgrade(cfg, "head", sql=True)
    return cfg.output_buffer.getvalue()


@pytest.fixture(scope="module")
def base_sql() -> str:
    """`alembic downgrade head:base --sql` 的离线输出。"""
    cfg = _make_config()
    command.downgrade(cfg, "head:base", sql=True)
    return cfg.output_buffer.getvalue()


# ---------------- revision 文件与链 ----------------


def test_initial_revision_file_exists_and_defines_downgrade() -> None:
    path = VERSIONS_DIR / "0001_initial.py"
    assert path.is_file(), f"缺少首个 revision 文件：{path}"
    source = path.read_text(encoding="utf-8")
    assert "def upgrade()" in source
    assert "def downgrade()" in source


def test_revision_chain_is_single_initial_head() -> None:
    script = ScriptDirectory.from_config(_make_config())
    assert script.get_heads() == ["0003_invites"]
    initial = script.get_revision("0001_initial")
    assert initial.down_revision is None
    assert callable(initial.module.upgrade)
    assert callable(initial.module.downgrade)
    # 每个增量 revision 都必须串在上一版之后，且自身可正反执行
    chain = [
        ("0002_multi_tenant", "0001_initial"),
        ("0003_invites", "0002_multi_tenant"),
    ]
    for revision_id, down_revision in chain:
        revision = script.get_revision(revision_id)
        assert revision.down_revision == down_revision, revision_id
        assert callable(revision.module.upgrade)
        assert callable(revision.module.downgrade)


def test_alembic_ini_does_not_hardcode_credentials() -> None:
    """连接串必须由 env.py 拼，ini 里不能出现凭据。"""
    text = ALEMBIC_INI.read_text(encoding="ascii")
    assert "mysql+pymysql://" not in text
    assert "script_location = %(here)s/migrations" in text


# ---------------- 离线 upgrade ----------------


def test_offline_sql_creates_all_business_tables(head_sql: str) -> None:
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} (" in head_sql, f"{table} 没有建表语句"


def test_offline_sql_has_foreign_key_index_and_engine_options(head_sql: str) -> None:
    assert "CONSTRAINT fk_messages_conversation FOREIGN KEY(conversation_id)" in head_sql
    assert "REFERENCES conversations (id) ON DELETE CASCADE" in head_sql
    assert "CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at)" in head_sql
    # V2：用户名唯一性作用域由全局改为租户内
    assert "CONSTRAINT uk_tenant_username UNIQUE (tenant_id, username)" in head_sql
    # V2：三张业务表的租户索引
    for index_name, table in (
        ("idx_documents_tenant", "documents"),
        ("idx_conversations_tenant", "conversations"),
        ("idx_evaluation_runs_tenant", "evaluation_runs"),
        ("idx_invites_tenant", "invites"),
    ):
        assert f"CREATE INDEX {index_name} ON {table} (tenant_id)" in head_sql, index_name
    # 6 张业务表都是 InnoDB + utf8mb4（alembic_version 用默认选项，不计入）
    assert head_sql.count("ENGINE=InnoDB") == len(EXPECTED_TABLES)
    assert head_sql.count("DEFAULT CHARSET=utf8mb4") == len(EXPECTED_TABLES)


def test_offline_sql_columns_match_schema_exactly(head_sql: str) -> None:
    """迁移建出来的列（名字 + 类型）必须与 app.models.database.SCHEMA 完全一致。"""
    expected = _table_columns(SCHEMA)
    actual = _table_columns(head_sql)
    assert set(expected) == set(EXPECTED_TABLES)
    for table, columns in expected.items():
        assert table in actual, f"离线 SQL 里缺少表 {table}"
        assert actual[table] == columns, f"{table} 的列定义与 SCHEMA 不一致"


def test_offline_sql_defaults_match_schema(head_sql: str) -> None:
    assert head_sql.count("DEFAULT CURRENT_TIMESTAMP") == SCHEMA.count("DEFAULT CURRENT_TIMESTAMP")
    assert head_sql.count("ON UPDATE CURRENT_TIMESTAMP") == SCHEMA.count("ON UPDATE CURRENT_TIMESTAMP") == 1
    assert "title VARCHAR(255) NOT NULL DEFAULT ''" in head_sql


def test_offline_sql_records_revision(head_sql: str) -> None:
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in head_sql


# ---------------- 离线 downgrade ----------------


def test_offline_downgrade_drops_every_table(base_sql: str) -> None:
    for table in EXPECTED_TABLES:
        assert f"DROP TABLE {table}" in base_sql, f"downgrade 没有删除 {table}"
    # 先删子表再删父表，否则会撞外键约束
    assert base_sql.index("DROP TABLE messages") < base_sql.index("DROP TABLE conversations")
    # 不能显式 DROP INDEX idx_messages_conversation：外键依赖该索引，
    # MySQL 会报 errno 1553（真库往返踩过）；DROP TABLE 会连带清掉它。
    assert "DROP INDEX idx_messages_conversation" not in base_sql
    # 但 0002 加的租户索引没有任何外键依赖，必须显式删除（否则列删了索引还在）
    for index_name in ("idx_documents_tenant", "idx_conversations_tenant", "idx_evaluation_runs_tenant"):
        assert f"DROP INDEX {index_name}" in base_sql, index_name
    # 回滚顺序：先删索引/列（0002），再删表（0001）
    assert base_sql.index("DROP INDEX idx_documents_tenant") < base_sql.index("DROP TABLE documents")


# ---------------- 真库往返（默认跳过） ----------------


def _mysql_server_params() -> dict[str, str]:
    from app.config import settings

    return {
        "host": settings.mysql_host,
        "port": str(settings.mysql_port),
        "user": settings.mysql_user,
        "password": settings.mysql_password,
        "db": settings.mysql_db,
    }


def _list_tables(conn, database: str) -> set[str]:  # noqa: ANN001
    with conn.cursor() as cur:
        cur.execute(f"SHOW TABLES FROM `{database}`")
        return {row[0] for row in cur.fetchall()}


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="需要 RUN_INTEGRATION=1")
def test_real_upgrade_then_downgrade_in_temporary_database() -> None:
    """真库往返：在独立临时库里 upgrade → downgrade，业务库不受影响。"""
    pymysql = pytest.importorskip("pymysql")
    params = _mysql_server_params()
    temp_db = f"{params['db']}_alembic_test"

    try:
        conn = pymysql.connect(
            host=params["host"],
            port=int(params["port"]),
            user=params["user"],
            password=params["password"],
            charset="utf8mb4",
            autocommit=True,
        )
    except Exception as exc:  # pragma: no cover - 取决于本机环境
        pytest.skip(f"MySQL 不可用，跳过：{exc}")

    url = (
        f"mysql+pymysql://{quote_plus(params['user'])}:{quote_plus(params['password'])}"
        f"@{params['host']}:{params['port']}/{temp_db}?charset=utf8mb4"
    )

    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{temp_db}`")
            cur.execute(f"CREATE DATABASE `{temp_db}` CHARACTER SET utf8mb4")

        command.upgrade(_make_config(db_url=url), "head")
        tables = _list_tables(conn, temp_db)
        assert set(EXPECTED_TABLES) <= tables, f"upgrade 后缺表：{set(EXPECTED_TABLES) - tables}"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'messages'",
                (temp_db,),
            )
            constraints = {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'messages'",
                (temp_db,),
            )
            indexes = {row[0] for row in cur.fetchall()}
        assert "fk_messages_conversation" in constraints
        assert "idx_messages_conversation" in indexes

        command.downgrade(_make_config(db_url=url), "base")
        assert not (set(EXPECTED_TABLES) & _list_tables(conn, temp_db)), "downgrade 后业务表应全部删除"
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{temp_db}`")
        finally:
            conn.close()
