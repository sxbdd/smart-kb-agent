# 复用审计报告（M1）

> 旧项目 `kb-ai-agent` → 新项目 `smart-kb-agent` 的模块复用判定。
> 审计时间：2026-09-13

## 判定结果

| 旧模块 | 判定 | 依据 |
| --- | --- | --- |
| core/embedding.py | 直接复用 | 三 provider 抽象，无需改 |
| core/vector_store.py | 直接复用 | Chroma + InMemory 抽象 |
| utils/text_splitter.py | 直接复用 | 段落 + 句边界 + overlap |
| utils/document_parser.py | 直接复用 | PDF/MD/TXT（V1 范围） |
| utils/logger.py / exceptions.py | 直接复用 | 无依赖 |
| core/reranker.py | 复用（P1） | 暂 Noop，A/B 实验后再定 |
| services/ingestion.py | 复用 + 微调 | 数据层接 MySQL |
| services/rag.py | 复用 + 微调 | 接入 Router |
| evaluation/runner.py | 复用 + 扩展 | 后续加 A/B 对比 |
| core/tools.py | 改造 | search_kb → knowledge_search |
| core/prompt_templates.py | 改造 | 加 Few-shot + Agent 引用硬约束 |
| services/agent.py | 改造 | 引用硬约束 + 返回 sources |
| services/conversation.py | 改造 | mode 分发 → Router 调度 |
| core/llm_client.py | 改造 | 加 token 控制 |
| models/database.py | 重写 | SQLite → MySQL |
| api/ 路由 | 复用 + 改造 | 加 auth；ask 走 Router |
| main.py / container.py / config.py | 扩展 | 加 MySQL / auth / Router |

## 新增模块
`services/router.py`、`services/chat.py`、`services/auth.py`、`api/routes_auth.py`、MySQL DAO、`users` / `evaluation_runs` 表。