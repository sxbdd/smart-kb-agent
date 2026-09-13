"""清理 Chroma 持久化目录里的**孤立 HNSW 段目录**。

问题：`ChromaVectorStore.reset()` 是"删集合 + 重建"，删集合会清掉 sqlite 里的
`segments` 记录，但**磁盘上的 HNSW 段目录不会被删**。反复重建索引（评测/压测会反复灌库）
就会在 `data/chroma_db/` 下堆一堆再也没人引用的 UUID 目录 —— issue 里常见的"Chroma 目录越来越大"。

判据（不做猜测，直接问 sqlite）：
1. 从 `chroma.sqlite3` 读出当前所有 `collections`；
2. 读出这些集合引用的 `segments.id`；
3. 目录名**不在**被引用集合里的 UUID 目录 = 孤立目录，可以删。

用法::

    .venv\\Scripts\\python scripts/cleanup_chroma.py             # 只报告，不删（默认 dry-run）
    .venv\\Scripts\\python scripts/cleanup_chroma.py --apply     # 真的删除
    .venv\\Scripts\\python scripts/cleanup_chroma.py --apply --keep-count 13

**必须在服务停止时运行**（客户端持有 sqlite 句柄）。脚本删完会用 `count()` 复核索引规模。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

#: sqlite 文件名（Chroma PersistentClient 固定用它）
DB_FILE = "chroma.sqlite3"


def _referenced_segments(db_path: Path) -> tuple[set[str], dict[str, str]]:
    """返回（被引用的 segment id 集合，集合名 -> 集合 id）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "segments" not in tables:
            raise RuntimeError(f"{db_path} 里没有 segments 表，可能不是 Chroma 的库；实际表：{sorted(tables)}")

        collections = {str(cid): name for cid, name in conn.execute("SELECT id, name FROM collections")}
        collection_ids = set(collections)

        segments: set[str] = set()
        for (seg_id, collection) in conn.execute("SELECT id, collection FROM segments"):
            if str(collection) in collection_ids:
                segments.add(str(seg_id))
        return segments, collections
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Chroma 孤立 HNSW 段目录")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只报告）")
    parser.add_argument("--dir", default=settings.chroma_persist_dir, help="Chroma 持久化目录")
    parser.add_argument("--keep-count", type=int, default=None,
                        help="删完后复核 chunk 总数，不一致则报错（例如 13）")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}", file=sys.stderr)
        return 2
    db_path = root / DB_FILE
    if not db_path.is_file():
        print(f"[错误] 找不到 {db_path}", file=sys.stderr)
        return 2

    referenced, collections = _referenced_segments(db_path)
    print(f"Chroma 目录：{root}")
    print(f"集合：{collections or '（无）'}")
    print(f"被引用的 segment：{len(referenced)} 个")

    orphans: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in referenced:
            continue
        # 只动看起来像 UUID/segment id 的目录，其它目录一律不碰（避免误删用户的备份）
        if len(child.name) == 36 and child.name.count("-") == 4:
            orphans.append(child)
        else:
            print(f"  [跳过] 不像 segment 目录：{child.name}")

    if not orphans:
        print("\n没有孤立目录，无需清理")
        return 0

    total_bytes = sum(
        f.stat().st_size for orphan in orphans for f in orphan.rglob("*") if f.is_file()
    )
    print(f"\n发现 {len(orphans)} 个孤立目录（共约 {total_bytes / 1024:.1f} KB）：")
    for orphan in orphans:
        size = sum(f.stat().st_size for f in orphan.rglob("*") if f.is_file())
        print(f"  - {orphan.name}  ({size / 1024:.1f} KB)")

    if not args.apply:
        print("\n[dry-run] 未删除任何东西；确认无误后加 --apply 执行")
        return 0

    removed = 0
    for orphan in orphans:
        try:
            shutil.rmtree(orphan)
            removed += 1
        except OSError as exc:
            # Chroma 还在跑时句柄被占用：报错但继续处理其余目录
            print(f"  [失败] {orphan.name}：{exc}")
    print(f"\n已删除 {removed}/{len(orphans)} 个孤立目录")

    # 复核：删目录**不应该**影响集合里已有的 chunk
    try:
        from app.core.vector_store import ChromaVectorStore

        store = ChromaVectorStore(str(root))
        count = store.count()
        print(f"复核：chunk 总数 = {count}")
        if args.keep_count is not None and count != args.keep_count:
            print(f"[错误] 期望 {args.keep_count}，实际 {count} —— 清理动作影响了索引！", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 复核失败（不影响清理结果）：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
