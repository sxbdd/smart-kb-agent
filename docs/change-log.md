# 变更记录（Change Log）

> 规则：V1 范围内的需求变更记录于此；超出范围的先进 V2 backlog，V1 期间不实现。

## V1 开发记录

| 阶段 | 内容 | 结果 |
| --- | --- | --- |
| M0 | PM 定稿 + 项目经理/架构师方案 | 范围与优先级冻结 |
| M1 | 复用审计 + 骨架（config/MySQL/LLM/Prompt/Tools） | 36 文件语法过、/healthz 通 |
| M2 | 核心 RAG 链路（解析/切分/Embedding/Chroma/Top-K/引用/拒答） | 真实验证通过 |
| M3 | Router + Agent + Tool Calling | 三路验证通过，修复 2 个真 bug |
| P0 | 登录鉴权接入 + 评测 | 鉴权 401/200、评测 5 题 100% |
| P1 | DOCX + 多轮 + 评测可视化 + Rerank A/B | 全部验证通过 |

## V1.1 审计整改（2026-09-14）

对 V1 做了一轮**以实际执行为依据**的审计（见 `docs/review-v1-audit.md`），
结论是"功能已完成，可信度尚未完成"，随后按优先级整改。

### 缺陷修复

| # | 问题 | 修复 |
| --- | --- | --- |
| P0-1 | 评测默认测试集指向不存在的 `test_set.json`，不传参必 500 | 改为 `test_set_smart.json` + 回归用例 |
| P0-2 | `test_real_rag.py` / `test_m3_agent.py` 缺鉴权头，必 401 | 整套脚本重构为 pytest 用例（见下） |
| P0-3 | `pytest` 声明了但未安装，无 `conftest.py` | 安装 pytest + 独立 `requirements-ci.txt` + `conftest.py` 隔离 |
| P0-4 | **跑基线时发现：`LLM_MAX_TOKENS=1024` 被推理模型的 `reasoning_content` 吃光，`content` 为空 → 客户端判定"空返回"重试同预算后抛错，RAG/对话/Agent 全线失败** | 客户端区分"被 `finish_reason=length` 截断"与"真·空返回"，前者自动加倍预算重试（封顶 8192）；默认预算 1024 → 4096；补 8 条 `test_llm_client.py` 用例 |
| P1-1 | 删会话留下孤儿消息（实测 2 条） | 先删消息再删会话 + `messages` 加 `ON DELETE CASCADE` 外键 + `purge_orphan_messages()` |
| P1-2 | 语料被 `testing.md`/`decisions.md` 污染（47 chunk 中 39 个无关） | 新增 `data/kb/` 标准语料 + `scripts/rebuild_kb.py`，已重建为 2 chunk |
| P1-3 | `.env.example` 末行缺换行，`RERANK_MODEL` 从未生效 | 重写 `.env.example` |
| P1-4 | Router 的 LLM 分类是死代码，异常被静默吞掉 | 新增 `ROUTER_ENABLE_LLM` 开关真正接通 + 失败改为 warning 日志 |
| P2-1 | 输入无上限（50 万字 question、`top_k=10^6` 均被接受） | `MAX_QUESTION_CHARS` / `MAX_TOP_K` 约束 |
| P2-2 | 上传无大小限制，整文件读进内存 | 分块落盘 + `MAX_UPLOAD_MB` 上限（413） |
| P2-3 | 登录/注册无限流（PBKDF2 可被放大成 CPU DoS） | 按 IP 滑动窗口限流（429） |
| P2-4 | `JWT_SECRET` 缺失时静默使用 `change-me` | 缺失或占位值**拒绝启动**；`scripts/init_env.py` 自动生成随机值 |
| P2-5 | 前端 `marked.parse` 结果未消毒直接 `innerHTML` | 引入本地 DOMPurify，`renderMarkdown` 强制消毒，缺库时降级纯文本 |
| P3-1 | 冷启动每次往返 HuggingFace（13.8s） | 新增 `app/core/hf_cache.py`，模型已缓存则自动离线加载 |
| P3-2 | 未使用 import / 依赖 / 死代码 | 清理 `settings`、`field`、`Optional`、`markdown` 依赖；补上"取消上传"按钮 |
| P3-3 | 裸 500 无提示、异常未记录 | 新增兜底异常处理器，记完整堆栈、对外只回通用信息 |
| P3-4 | Agent 对纯计算题也做一次知识库检索；提示词每轮尾随追加 | 计算题跳过检索；提示词改为首位单次注入 |
| P3-5 | `start.bat` 在服务监听端口前就打开浏览器 → 用户看到 `ERR_CONNECTION_REFUSED` | 新增 `scripts/open_when_ready.py`：轮询 `/healthz`，就绪后才开浏览器；超时弹提示并写 `data/startup.log` |
| P3-6 | 浏览器请求 `/favicon.ico` 恒 404，污染启动日志 | 前端加 `<link rel="icon" href="data:,">` 阻止该请求 |
| P3-7 | PDF/DOCX 损坏时抛未处理异常 → 接口 500 且无有用信息 | 解析器统一转 `AppError(400)`；PDF 加密单独提示；补 4 条损坏文件用例 |
| P3-8 | 单题 LLM 失败会让整轮评测崩溃（47 题批量下不可接受） | `run_evaluation` 逐题捕获异常记为不正确，并输出 `error_count` |
| P3-9 | requirements 文件里的中文注释使 `pip-audit` 抛 `UnicodeDecodeError`（无 BOM 时按 cp936 解码） | 三个 requirements 文件改为纯 ASCII，并注释说明原因 |
| P3-10 | CrossEncoder 推理时打印进度条，污染服务日志 | `predict(..., show_progress_bar=False)` |

### 工程改造

- `create_app` 从 `app/main.py` 抽到 `app/factory.py`，消除 `import app.main` 的容器副作用；
- `Settings` 字段改为 `default_factory`，**实例化时**读环境，测试不再受 import 顺序影响；
- 6 个脚本式测试重构为 **13 个测试模块 / 152 个用例**（148 离线 + 4 真实环境默认跳过），
  配 `FakeDatabase` + `tmp_path` 全离线隔离；
- 新增 `.github/workflows/ci.yml`（Python 3.10 / 3.12）+ **`pip-audit` 依赖漏洞扫描任务**
  （测试依赖集阻断、生产依赖集仅提示）；
- 新增脚本：`init_env.py`、`rebuild_kb.py`、`open_when_ready.py`、
  **`run_evaluation.py`**（命令行评测 + `--repeat` 稳定性）、
  **`rerank_ab.py`**（排序质量 A/B）、**`benchmark.py`**（检索/并发压测）；
- `docs/testing.md` 扩写为**完整测试方案**（分层策略、用例清单、评测方法、验收标准、已知缺口、维护规范）；
- **补 PDF 解析用例**（用例内手工构造最小 PDF，零新增依赖），并顺带修掉"损坏 PDF/DOCX 抛未处理异常 → 500"；
- **评测语料与测试集扩容**：语料 2 → **6 个文档**（含 2 个近邻干扰），测试集 5 → **47 题**
  （可答 39 + 拒答 8；分类 fact 31 / paraphrase 5 / multi_fact 3 / refusal 8），
  并新增 **8 条测试集一致性用例**守住"出处真实存在""关键词在 gold_evidence 中""不凭空编造事实"。

### 验证结果（V1 收尾，2026-09-14）

```
python -m pytest   →  148 passed, 4 skipped（8.8s）
python -m compileall -q app tests scripts   →  exit 0
```

**评测基线**（6 文档 / 13 chunk；47 题；`top_k=5`；同一配置连跑 2 次）

| 指标 | 第 1 次 | 第 2 次 | 极差 |
| --- | --- | --- | --- |
| `keyword_accuracy` | 100.0% | 100.0% | 0.0pp |
| `source_accuracy` | 100.0% | 100.0% | 0.0pp |
| `refusal_accuracy` | 100.0% | 100.0% | 0.0pp |
| `overall_accuracy` | 100.0% | 100.0% | 0.0pp |

**Rerank A/B**：Recall@3 两组均 100%；首命中 92.3% → 94.9%（MRR +0.017），
延迟 9.3ms → 1236ms（**133×**）→ **维持 `ENABLE_RERANK=false`**（ADR-011 已更新为实测结论）。

**检索性能**（CPU 单进程）：串行 8.3ms / 120.7 QPS；并发 4 达 143.9 QPS 后**见顶**，延迟随并发线性上涨。

**依赖审计**（`pip-audit`）：测试/CI 依赖集无已知漏洞；生产依赖集 9 个（`chromadb` 5 个**无修复版本**，`setuptools` 已升级）。

### 遗留（V2）

- 检索无相似度阈值（top-k 必返回结果），拒答完全依赖 Prompt 约束 —— 是否需要 `MIN_SCORE` 待评估；
- 语料 6 文档 / 13 chunk，仍属小规模，指标**不可外推**；继续扩容到数百 chunk 时应重跑 Rerank A/B；
- 并发 4 之后吞吐见顶（瓶颈是 CPU 上的查询向量化，不是 Chroma）；
- `chromadb` 存在 5 个无修复版本的漏洞（上游问题，CI 仅提示）；
- 前端无 E2E；单租户；限流为进程内实现；无 DB 迁移工具。

## 范围控制记录

| 决策 | 依据 |
| --- | --- |
| Rerank 不预设启用 | 2026-09-14 复测：Recall@3 两组均 100%，首命中 +2.6pp 但延迟 133× → **维持关闭**（ADR-011 已更新为实测结论） |
| 不做多租户/RBAC | V1 单租户定位，避免范围膨胀 |
| 多轮对话列为 P1 | 核心 RAG 先行，多轮后置 |
| V1.1 只做"恢复可信度 + 补边界"，不扩功能 | 审计结论：短板在可验证性与健壮性，不在功能 |

## V2 backlog

OCR · Excel · 流式输出 · MCP · 多租户 · RBAC · 更大语料 Rerank 重测 ·
检索相似度阈值 · 限流改共享存储（多副本）
