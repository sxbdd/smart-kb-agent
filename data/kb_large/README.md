# 合并压测语料（6 篇原始 + 60 篇干扰）—— 不参与日常评测

本目录由 `scripts/gen_corpus.py --include-base` 生成，**同时包含**：

| 来源 | 数量 | 作用 |
| --- | --- | --- |
| `data/kb/`（原始标准语料） | 6 篇 | 评测集 39 道题的**金标准出处**，必须留在索引里 |
| `data/kb_ext/`（合成干扰） | 60 篇 | 制造"话题相近、事实各异"的检索噪声 |
| 合计 | 66 篇 | — |

## 为什么必须合并

`scripts/rebuild_kb.py --kb-dir X` 的行为是"**清空整个集合，然后只灌 X**"。
如果只灌 `data/kb_ext/`，那 6 篇金标准出处一篇都不在库里，
`data/evaluation/test_set_smart.json` 的所有题都会 `not_found` ——
Noop 和 Rerank 都拿 0 分，**测不出任何东西**（这是一次真实的踩坑记录）。

合并之后：正确答案仍在候选池里，但旁边多了 60 篇用词高度相似的干扰文档，
这才是"候选多、干扰大"下的真实检索压力，才谈得上"Rerank 值不值"。

## 怎么用

```powershell
# 1. 生成合并语料（原始 6 篇 + 60 篇干扰）
.venv\Scripts\python scripts\gen_corpus.py --out-dir data/kb_large --include-base --clean

# 2. 灌库
.venv\Scripts\python scripts\rebuild_kb.py --kb-dir data/kb_large --yes

# 3. 自检：6 篇原始文档必须在索引里，否则不要往下跑
.venv\Scripts\python scripts\rerank_ab.py --candidates 20 --final 5 --json data/rerank_ab_large.json

# 4. 务必恢复日常索引
.venv\Scripts\python scripts\rebuild_kb.py --yes
```

原始语料的角色说明见 `data/kb/README.md`；本目录不参与 `source_accuracy` 日常评测。
