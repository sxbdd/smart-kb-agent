"""V2 多租户改造的**真机验收**（docs/v2-plan.md §9.3）。

离线单测跑的是 `FakeDatabase` + `InMemoryVectorStore`；本脚本跑的是**真实 MySQL + 真实 Chroma**。
为什么必须分开：假库的隔离语义是"照着真库写的"，SQL 里 `tenant_id` 条件写漏、
Chroma 的 `where` 用法不对、唯一键没改，这些**只有真机才暴露**。

四部分（可单独跑，全部成功退出码 0）：

| 部分 | 做什么 |
| --- | --- |
| `temp-db` | 临时库跑 `0001_initial → 0002_multi_tenant → base` 往返，用 information_schema 校验 |
| `upgrade` | **真实库**：先逻辑备份 → `stamp 0001_initial` → `upgrade head` → 校验列/索引/唯一键/外键/存量数据 |
| `chroma` | 真实 Chroma 双租户隔离（含"老数据缺 tenant_id 字段"的已知偏差演示） |
| `api` | 临时库 + 真实 Chroma 上跑一遍角色矩阵与跨租户隔离（走真实 HTTP） |

用法::

    .venv\\Scripts\\python scripts/verify_tenancy.py --part all
    .venv\\Scripts\\python scripts/verify_tenancy.py --part chroma
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import settings  # noqa: E402

TABLES = ("users", "documents", "conversations", "messages", "evaluation_runs")
ALEMBIC_INI = ROOT / "alembic.ini"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """记录一条断言结果；不抛异常，跑完全部再汇总（一次看全所有问题）。"""
    mark = "  [OK]  " if ok else "  [FAIL]"
    print(f"{mark} {label}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(label)
    return ok


# ---------------- 连接与配置 ----------------

def server_conn(database: str | None = None):
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


def db_url(database: str) -> str:
    return (
        f"mysql+pymysql://{quote_plus(settings.mysql_user)}:{quote_plus(settings.mysql_password)}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{database}?charset=utf8mb4"
    )


def alembic_config(database: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI), output_buffer=io.StringIO())
    if database is not None:
        cfg.cmd_opts = argparse.Namespace(x=[f"db_url={db_url(database)}"])
    return cfg


def recreate_database(conn, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")


def drop_database(conn, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")


# ---------------- information_schema 查询 ----------------

def columns(conn, db: str, table: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY ORDINAL_POSITION",
            (db, table),
        )
        return {r[0]: {"type": r[1], "nullable": r[2], "default": r[3]} for r in cur.fetchall()}


def indexes(conn, db: str, table: str) -> dict[str, dict]:
    """索引名 -> {列: 顺序}（复合索引的列顺序也有意义，比如唯一键必须以 tenant_id 打头）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE, COLUMN_NAME "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (db, table),
        )
        out: dict[str, dict] = {}
        for name, col, seq, non_unique, _ in cur.fetchall():
            entry = out.setdefault(name, {"columns": {}, "unique": not int(non_unique)})
            entry["columns"][int(seq)] = col
        return out


def constraints(conn, db: str, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (db, table),
        )
        return {r[0] for r in cur.fetchall()}


def table_exists(conn, db: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (db, table),
        )
        return bool(cur.fetchone()[0])


def row_counts(conn, db: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in TABLES:
            if not table_exists(conn, db, table):
                continue
            cur.execute(f"SELECT COUNT(*) FROM `{db}`.{table}")
            out[table] = int(cur.fetchone()[0])
    return out


# ---------------- 校验一套「迁移后」的表结构 ----------------

def verify_migrated_schema(conn, db: str, label: str, expect_users_default_tenant: bool = True) -> None:
    for table in TABLES:
        check(f"{label}：{table} 表存在", table_exists(conn, db, table))

    # users：租户 + 角色 + 租户内唯一
    uc = columns(conn, db, "users")
    check(f"{label}：users.tenant_id 存在且 NOT NULL", uc.get("tenant_id", {}).get("type") == "varchar(64)"
          and uc.get("tenant_id", {}).get("nullable") == "NO", str(uc.get("tenant_id")))
    check(f"{label}：users.role 存在且默认 user",
          uc.get("role", {}).get("type") == "varchar(16)" and uc.get("role", {}).get("default") == "user",
          str(uc.get("role")))
    uidx = indexes(conn, db, "users")
    check(f"{label}：uk_username 已被删除", "uk_username" not in uidx)
    uk = uidx.get("uk_tenant_username")
    check(
        f"{label}：uk_tenant_username 是唯一键且以 tenant_id 打头",
        bool(uk) and uk["unique"] and uk["columns"].get(1) == "tenant_id" and uk["columns"].get(2) == "username",
        str(uk),
    )

    # 其余三张表：租户列 + 租户索引
    for table, index_name in (
        ("documents", "idx_documents_tenant"),
        ("conversations", "idx_conversations_tenant"),
        ("evaluation_runs", "idx_evaluation_runs_tenant"),
    ):
        cols = columns(conn, db, table)
        check(f"{label}：{table}.tenant_id 存在",
              cols.get("tenant_id", {}).get("type") == "varchar(64)" and cols["tenant_id"]["nullable"] == "NO",
              str(cols.get("tenant_id")))
        idx = indexes(conn, db, table)
        check(f"{label}：{table} 有 {index_name}(tenant_id)", index_name in idx and idx[index_name]["columns"] == {1: "tenant_id"},
              str(idx.get(index_name)))

    # messages 外键（存量库通常缺失，0002 要补齐）
    check(f"{label}：messages 有 fk_messages_conversation", "fk_messages_conversation" in constraints(conn, db, "messages"))

    if expect_users_default_tenant:
        with conn.cursor() as cur:
            cur.execute(f"SELECT tenant_id, role, COUNT(*) FROM `{db}`.users GROUP BY tenant_id, role")
            groups = cur.fetchall()
        check(
            f"{label}：存量用户全部落在 default 租户",
            all(g[0] == "default" for g in groups) if groups else True,
            str(groups),
        )


# ---------------- 各 part ----------------

def part_temp_db() -> None:
    print("\n=== [1/4] 临时库：0001 → 0002 → base 往返 ===")
    temp_db = f"{settings.mysql_db}_v2verify"
    conn = server_conn()
    try:
        recreate_database(conn, temp_db)
        command.upgrade(alembic_config(temp_db), "head")
        verify_migrated_schema(conn, temp_db, "临时库", expect_users_default_tenant=False)

        command.downgrade(alembic_config(temp_db), "base")
        left = [t for t in TABLES if table_exists(conn, temp_db, t)]
        check("临时库：downgrade 后业务表全部删除", not left, str(left))
        check("临时库：downgrade 后 messages 唯一键/外键一并消失", "uk_tenant_username" not in indexes(conn, temp_db, "users") or not table_exists(conn, temp_db, "users"))
    finally:
        drop_database(conn, temp_db)
        conn.close()


def _dump_real_db(conn, db: str) -> Path:
    """逻辑备份：不用 mysqldump（本机没装），直接把 5 张表全量导出成 JSON。"""
    backup_dir = ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"mysql-{db}-pre-v2-{stamp}.json"

    dump: dict[str, list] = {}
    with conn.cursor() as cur:
        for table in TABLES:
            if not table_exists(conn, db, table):
                continue
            cur.execute(f"SHOW COLUMNS FROM `{db}`.{table}")
            cols = [r[0] for r in cur.fetchall()]
            cur.execute(f"SELECT * FROM `{db}`.{table}")
            rows = []
            for row in cur.fetchall():
                item = {}
                for i, col in enumerate(cols):
                    value = row[i]
                    item[col] = value.isoformat() if hasattr(value, "isoformat") else value
                rows.append(item)
            dump[table] = rows
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def part_upgrade() -> None:
    db = settings.mysql_db
    print(f"\n=== [2/4] 真实库 {db}：备份 → stamp → upgrade → 校验 ===")
    conn = server_conn(db)
    try:
        backup = _dump_real_db(conn, db)
        print(f"  逻辑备份：{backup.relative_to(ROOT)}（{backup.stat().st_size} 字节）")

        before = row_counts(conn, db)
        print(f"  升级前行数：{before}")
        had_alembic = table_exists(conn, db, "alembic_version")
        had_tenant = "tenant_id" in columns(conn, db, "users")

        if had_alembic:
            current = _current_revision(conn, db)
            print(f"  库里已有 alembic_version，当前版本：{current}")
            if current != "0002_multi_tenant":
                command.upgrade(alembic_config(), "head")
        elif had_tenant:
            # 极端情况：列已经手工加过（或从别处拷来的库），但没有版本表。
            # 此时**绝不能**从 0001 再 upgrade —— 0002 会重复 ADD COLUMN 而报错。
            print("  已有 tenant_id 但缺 alembic_version：按「0002 已应用」处理，直接 stamp")
            command.stamp(alembic_config(), "0002_multi_tenant")
        else:
            print("  库里没有 alembic_version：先 stamp 到 0001_initial（标记「已有 V1 表结构」），再 upgrade")
            command.stamp(alembic_config(), "0001_initial")
            command.upgrade(alembic_config(), "head")

        check("真实库：版本已到 0002_multi_tenant", _current_revision(conn, db) == "0002_multi_tenant")
        verify_migrated_schema(conn, db, "真实库")

        after = row_counts(conn, db)
        print(f"  升级后行数：{after}")
        check("真实库：升级不丢数据（各表行数不变）", before == after, f"{before} -> {after}")

        # 幂等：再跑一次 upgrade 应当无事发生
        command.upgrade(alembic_config(), "head")
        check("真实库：重复 upgrade 幂等", row_counts(conn, db) == after)
    finally:
        conn.close()


def _current_revision(conn, db: str) -> str | None:
    if not table_exists(conn, db, "alembic_version"):
        return None
    with conn.cursor() as cur:
        cur.execute(f"SELECT version_num FROM `{db}`.alembic_version")
        row = cur.fetchone()
        return row[0] if row else None


def part_chroma() -> None:
    print("\n=== [3/4] 真实 Chroma：双租户隔离 ===")
    import shutil
    import tempfile as _tempfile

    from app.core.vector_store import ChromaVectorStore

    # 注意：Windows 上 Chroma 的 PersistentClient 会一直持有 sqlite 句柄，
    # `TemporaryDirectory` 的自动清理会抛 PermissionError —— 所以手动建目录 + 忽略清理错误。
    tmp = _tempfile.mkdtemp(prefix="v2-verify-chroma-")
    try:
        store = ChromaVectorStore(persist_dir=tmp, collection_name="verify_tenancy")
        vec = [1.0, 0.0, 0.0]

        store.add("a-1", vec, {"document_id": "doc-a", "document_name": "a.txt", "tenant_id": "t-a"}, "A 租户片段")
        store.add("b-1", vec, {"document_id": "doc-b", "document_name": "b.txt", "tenant_id": "t-b"}, "B 租户片段")
        # V1 时期灌进去的老 chunk：没有 tenant_id 字段
        store.add("legacy", vec, {"document_id": "doc-old", "document_name": "old.txt"}, "V1 老片段")

        check("真实 Chroma：count() == 3", store.count() == 3, str(store.count()))

        ids_a = {r.id for r in store.query(vec, top_k=10, tenant_id="t-a")}
        ids_b = {r.id for r in store.query(vec, top_k=10, tenant_id="t-b")}
        ids_all = {r.id for r in store.query(vec, top_k=10)}
        check("真实 Chroma：t-a 只看到自己的片段", ids_a == {"a-1"}, str(ids_a))
        check("真实 Chroma：t-b 只看到自己的片段", ids_b == {"b-1"}, str(ids_b))
        check("真实 Chroma：tenant_id=None 不过滤（V1 行为）", ids_all == {"a-1", "b-1", "legacy"}, str(ids_all))
        check("真实 Chroma：top_k 放大也捞不到别的租户",
              all(r.metadata.get("tenant_id") == "t-a" for r in store.query(vec, top_k=1000, tenant_id="t-a")))

        # 已知偏差（fail-closed）：Chroma 的 where 对"缺字段"的记录不匹配，且 query 不支持 $exists
        legacy_invisible = {r.id for r in store.query(vec, top_k=10, tenant_id="default")} == set()
        show_divergence = store.query(vec, top_k=10, tenant_id="default")
        print(f"  [注意] tenant_id='default' 查到的：{[r.id for r in show_divergence]}")
        check(
            "真实 Chroma：缺 tenant_id 的老数据在带租户查询下不可见（fail-closed，已知偏差）",
            legacy_invisible,
            "若此处失败说明 Chroma 行为变了，需重新评估「缺字段=default」的兼容策略",
        )

        # 删除也带租户：用别的租户删不掉
        store.delete_document("doc-a", tenant_id="t-b")
        check("真实 Chroma：跨租户 delete_document 无效", store.count() == 3, str(store.count()))
        store.delete_document("doc-a", tenant_id="t-a")
        check("真实 Chroma：本租户 delete_document 生效", store.count() == 2, str(store.count()))
    finally:
        # 句柄未释放时 rmtree 也会失败，直接忽略：临时目录清理失败不该让验收失败
        shutil.rmtree(tmp, ignore_errors=True)


def part_api() -> None:
    print("\n=== [4/4] 真实 MySQL + 真实 Chroma：角色矩阵与跨租户隔离（HTTP） ===")
    from fastapi.testclient import TestClient

    from app.factory import create_app

    temp_db = f"{settings.mysql_db}_v2api"
    conn = server_conn()
    chroma_dir = tempfile.mkdtemp(prefix="v2-verify-chroma-")
    try:
        recreate_database(conn, temp_db)
        command.upgrade(alembic_config(temp_db), "head")

        cfg = dataclasses.replace(
            settings,
            mysql_db=temp_db,
            llm_provider="fake",
            embedding_provider="hash",
            vector_store="chroma",
            chroma_persist_dir=chroma_dir,
            enable_rerank=False,
            rate_limit_backend="memory",
            auth_rate_limit_per_minute=10000,
            bootstrap_admin_username="root",
        )
        client = TestClient(create_app(cfg))
        PWD = "test123456"

        def reg(username, tenant=None):
            payload = {"username": username, "password": PWD}
            if tenant:
                payload["tenant"] = tenant
            return client.post("/auth/register", json=payload)

        root = reg("root")
        check("HTTP：bootstrap 账号注册即 admin", root.status_code == 200 and root.json()["role"] == "admin", root.text)
        admin_h = {"Authorization": f"Bearer {root.json()['token']}"}

        user = reg("tester")
        user_h = {"Authorization": f"Bearer {user.json()['token']}"}

        # 真实库上的唯一键：同名 + 不同租户必须成功（uk_tenant_username 生效）
        other = reg("tester", tenant="tenant-b")
        check("真实库：不同租户可同名注册（uk_tenant_username 生效）", other.status_code == 200, other.text)
        other_h = {"Authorization": f"Bearer {other.json()['token']}"}

        dup = reg("tester")
        check("真实库：同租户重名仍返回 409", dup.status_code == 409, dup.text)

        # viewer 由 admin 代建
        made = client.post("/admin/users", json={"username": "a-viewer", "password": PWD, "role": "viewer"}, headers=admin_h)
        check("HTTP：admin 可代建 viewer", made.status_code == 200 and made.json()["role"] == "viewer", made.text)
        viewer_h = {"Authorization": f"Bearer {client.post('/auth/login', json={'username': 'a-viewer', 'password': PWD}).json()['token']}"}

        # 角色矩阵
        check("HTTP：viewer 可提问", client.post("/ask", json={"question": "你好"}, headers=viewer_h).status_code == 200)
        check("HTTP：viewer 上传被拒 403",
              client.post("/upload", files={"file": ("v.txt", b"x", "text/plain")}, headers=viewer_h).status_code == 403)

        up = client.post(
            "/upload",
            files={"file": ("a-only.txt", "租户 A 专属：代号 ALPHA-ONE。".encode("utf-8"), "text/plain")},
            headers=user_h,
        )
        check("HTTP：user 可上传", up.status_code == 200, up.text)
        doc_id = up.json()["document_id"]

        check("HTTP：跨租户看不到文档", client.get("/documents", headers=other_h).json() == [])
        check("HTTP：user 删文档 403", client.delete(f"/documents/{doc_id}", headers=user_h).status_code == 403)
        check("HTTP：user 跑评测 403", client.post("/evaluation/run", json={}, headers=user_h).status_code == 403)
        check("HTTP：user 看用户列表 403", client.get("/admin/users", headers=user_h).status_code == 403)

        conv = client.post("/ask", json={"question": "你好"}, headers=user_h).json()["conversation_id"]
        check("HTTP：跨租户读会话 404", client.get(f"/conversations/{conv}", headers=other_h).status_code == 404)
        check("HTTP：本租户读会话 200", client.get(f"/conversations/{conv}", headers=user_h).status_code == 200)

        # 真实 Chroma 检索隔离（走容器内的真实 RAGService）
        container = client.app.state.container
        b_file = Path(chroma_dir) / "b-only.txt"
        b_file.write_text("租户 B 专属：代号 BRAVO-TWO。", encoding="utf-8")
        container.ingestion.ingest(str(b_file), "b-only.txt", "tenant-b")

        def names(tenant_id):
            return {r.metadata.get("document_name") for r in container.rag.search("专属 代号", top_k=10, tenant_id=tenant_id)}

        check("HTTP+Chroma：默认租户检索不到 tenant-b 的文档", "b-only.txt" not in names("default"), str(names("default")))
        check("HTTP+Chroma：tenant-b 检索不到默认租户的文档", "a-only.txt" not in names("tenant-b"), str(names("tenant-b")))

        check("HTTP：admin 删文档 200", client.delete(f"/documents/{doc_id}", headers=admin_h).status_code == 200)
        check("HTTP：admin 跑评测 200", client.post("/evaluation/run", json={}, headers=admin_h).status_code == 200)
        check("HTTP：admin 用户列表只含本租户",
              {r["tenant_id"] for r in client.get("/admin/users", headers=admin_h).json()} == {"default"})
    finally:
        drop_database(conn, temp_db)
        conn.close()


PARTS = {
    "temp-db": part_temp_db,
    "upgrade": part_upgrade,
    "chroma": part_chroma,
    "api": part_api,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="V2 多租户真机验收")
    parser.add_argument("--part", choices=[*PARTS, "all"], default="all")
    args = parser.parse_args()

    print(f"MySQL: {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}  用户={settings.mysql_user}")
    todo = list(PARTS) if args.part == "all" else [args.part]
    for name in todo:
        PARTS[name]()

    print("\n" + "=" * 60)
    if _failures:
        print(f"结果：{len(_failures)} 项未通过")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
