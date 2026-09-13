# 压测语料（load-test corpus）—— 不参与日常评测

本目录由 `scripts/gen_corpus.py` 生成，用途只有一个：**为「大语料下 Rerank 是否值得」
提供可控的测试场**。

## 和 `data/kb/` 的区别

| | `data/kb/`（日常评测语料） | `data/kb_ext/`（本目录，压测语料） |
| --- | --- | --- |
| 规模 | 6 个文档 / 13 chunk | 默认 60 个文档 / 数百 chunk |
| 参与 `source_accuracy` 评测 | **是**（`data/evaluation/test_set_smart.json` 全部指向它） | **否** |
| 参与 `scripts/rerank_ab.py` | 是（`--kb-dir data/kb`） | 仅在做大语料 AB 时临时灌入 |
| 事实核对 | 逐句可查（`gold_evidence` 硬约束） | 不需核对，仅为制造检索干扰 |
| 是否版本管理 | 是 | 生成物，可随时重建 |

**结论：日常评测语料在 `data/kb/`，本目录的文件不要当评测依据。**

## 怎么用

```powershell
# 1. 生成（固定种子，可复现）
.venv\Scripts\python scripts\gen_corpus.py --docs 60

# 2. 灌进索引（会清空当前索引，建议先确认已备份）
.venv\Scripts\python scripts\rebuild_kb.py --kb-dir data/kb_ext --yes

# 3. 跑大语料 Rerank A/B
.venv\Scripts\python scripts\rerank_ab.py --candidates 20 --final 5 --json data/rerank_ab_large.json

# 4. 务必恢复日常索引，否则 V1 基线被破坏
.venv\Scripts\python scripts\rebuild_kb.py --yes
```

## 设计意图

- **话题相近**：同一主题（差旅、考勤、付款、培训…）在多个部门各有一份，
  用词高度相似（"住宿标准""审批权限""报销时限"），向量检索很容易把它们混在一起；
- **事实各异**：所有金额、天数、比例、时限都由固定随机种子生成，每篇数值都不同 ——
  干扰是真的干扰，不是文字游戏；
- **结构真实**：每条制度都是「条款 + 具体数值 + 审批层级」的正式文体，
  按段落切分后每篇产生多个 chunk，索引规模达到数百量级。

生成参数与随机种子见 `scripts/gen_corpus.py` 的 `--seed`（默认 20260101）。
