"""重建知识库：清空向量库与文档元数据，再从标准语料目录重新灌入。

用法：
    python scripts/rebuild_kb.py --dry-run     # 只显示将删除/新增什么
    python scripts/rebuild_kb.py               # 执行（先自动备份）
    python scripts/rebuild_kb.py --kb-dir data/kb --yes

背景：项目早期由测试脚本"顺手上传"了大量与业务无关的文件（testing.md / decisions.md），
且从不清理，导致 Chroma 的 47 个 chunk 里 39 个是项目自身文档，评测失去意义。
见 docs/review-v1-audit.md §2.5。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.container import build_container  # noqa: E402

SUPPORTED = {".txt", ".md", ".markdown", ".text", ".pdf", ".docx"}


def _backup(container, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"kb-backup-{stamp}.json"
    payload = {
        "documents": [
            {k: str(v) for k, v in row.items()} for row in container.db.list_documents()
        ],
        "evaluation_runs": [
            {"run_id": r["id"], "metrics": r["metrics"], "created_at": str(r["created_at"])}
            for r in container.db.list_evaluation_runs()
        ],
        "chunk_count": container.vector_store.count(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="重建知识库（清空 + 重新灌入标准语料）")
    parser.add_argument("--kb-dir", default=str(ROOT / "data" / "kb"), help="标准语料目录")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何修改")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    parser.add_argument("--no-backup", action="store_true", help="不写备份文件（不推荐）")
    args = parser.parse_args()

    kb_dir = Path(args.kb_dir)
    if not kb_dir.is_dir():
        print(f"[错误] 语料目录不存在：{kb_dir}", file=sys.stderr)
        return 1

    seeds = sorted(
        p for p in kb_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED
        and not p.name.lower().startswith(("readme", "_", "."))   # README / 临时文件不算语料
    )
    if not seeds:
        print(f"[错误] {kb_dir} 下没有可灌入的语料", file=sys.stderr)
        return 1

    container = build_container()
    current_docs = container.db.list_documents()
    current_chunks = container.vector_store.count()
    orphans = container.db.count_orphan_messages()

    print("=" * 64)
    print("即将清空：")
    print(f"  向量库 chunk 数 : {current_chunks}")
    print(f"  文档元数据行数  : {len(current_docs)}")
    for row in current_docs:
        print(f"      - {row['filename']}  ({row['chunk_count']} chunk)")
    print(f"  孤儿消息        : {orphans}")
    print("即将灌入：")
    for p in seeds:
        print(f"      + {p.name}")
    print("=" * 64)

    if args.dry_run:
        print("[dry-run] 未做任何修改")
        return 0

    if not args.yes:
        answer = input("确认执行？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消")
            return 1

    if not args.no_backup:
        backup = _backup(container, ROOT / "data" / "backups")
        print(f"[1/4] 已备份到 {backup}")
    else:
        print("[1/4] 跳过备份")

    container.vector_store.reset()
    print("[2/4] 向量库已清空")

    removed = container.db.delete_all_documents()
    purged = container.db.purge_orphan_messages()
    print(f"[3/4] 文档元数据删除 {removed} 行，孤儿消息清理 {purged} 条")

    print("[4/4] 灌入标准语料 ...")
    for path in seeds:
        resp = container.ingestion.ingest(str(path), path.name)
        print(f"      + {resp.filename}  -> {resp.chunk_count} chunk")

    print("=" * 64)
    print(f"完成：向量库 {container.vector_store.count()} chunk，"
          f"文档 {len(container.db.list_documents())} 个")
    print("下一步：运行评测（POST /evaluation/run 或前端「运行评测」）获取真实指标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
