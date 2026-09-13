# 企业级 RAG + Agent 智能知识库系统（smart-kb-agent）

面向 **HR / 行政 / 客服** 的企业知识库智能问答助手：上传制度文档，得到一个"答得准、能溯源、不知道就说不知道"的内部 AI 助手。

## 核心架构

```
用户问题 → Router（规则优先 + LLM 分类 + RAG 兜底）
   ├─→ 普通对话 Chat
   ├─→ RAG Workflow（检索 → 生成 → 引用）
   └─→ Agent（ReAct → Tools）
最终回答（RAG / Agent 带引用）
```

## 技术栈

FastAPI · RAG · Chroma · sentence-transformers(bge-small-zh) · Agent(ReAct) · Tool Calling · Evaluation · MySQL · JWT

## 功能特性

- **文档管理**：PDF / Markdown / TXT / DOCX 上传，自动解析、切分、向量化入库
- **智能问答**：Router 自动分发 Chat / RAG / Agent，回答带引用来源
- **诚实拒答**：知识库无依据时明确拒答，不编造
- **Agent**：ReAct 循环 + 工具调用（knowledge_search / calculator）
- **多轮对话**：chat / rag / agent 统一接入历史上下文
- **评测**：关键词命中率 / 来源准确率 / 拒答正确率 / 综合准确率 + 历史可视化
- **认证**：JWT + PBKDF2，业务接口 Bearer 鉴权

## 状态

**V1 完成（P0 + P1），并已完成 V1.1 审计整改与收尾验证。**

- 修复 **20 项缺陷**（含跑基线时发现的 P0：`LLM_MAX_TOKENS=1024` 被推理模型思考吃光导致全线失败）；
- 测试从 6 个脚本升级为 **13 个模块 / 152 个用例 + CI**（Python 3.10/3.12）+ `pip-audit` 依赖扫描；
- 语料从被测试污染重建为 **6 个文档（含 2 个近邻干扰）/ 13 chunk**，评测集 **5 → 47 题**；
- **评测基线**（连跑 2 次）：关键词 / 来源 / 拒答 / 综合 **均 100%，极差 0.0pp**；
- **Rerank A/B**：Recall@3 两组均 100%，首命中 +2.6pp 但延迟 133× → **维持关闭**（ADR-011 已更新为实测结论）；
- 检索性能：串行 8.3ms / 120.7 QPS，4 并发后吞吐见顶。

详见 [docs/review-v1-audit.md](docs/review-v1-audit.md)、[docs/change-log.md](docs/change-log.md)、
[docs/testing.md](docs/testing.md) §11（基线原始数据与复现命令）。

## 快速开始

```powershell
# 1. 初始化配置（自动生成 .env 并写入随机 JWT_SECRET）
.venv\Scripts\python scripts\init_env.py
#    然后编辑 .env，至少填 LLM_API_KEY 与 MYSQL_PASSWORD

# 2. 一键启动（自动建 venv、装依赖、起服务）
start.bat

# 或手动
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

访问 http://127.0.0.1:8000/ ｜ Swagger：http://127.0.0.1:8000/docs

## 常用脚本

```powershell
# 离线测试（约 3 秒，不需要 MySQL / 模型 / 外网）
.venv\Scripts\python -m pytest

# 重建知识库（清空污染语料，从 data/kb/ 灌入标准语料；自动备份）
.venv\Scripts\python scripts\rebuild_kb.py --dry-run
.venv\Scripts\python scripts\rebuild_kb.py

# 真实环境端到端（会真实调用 LLM API 并写入 MySQL）
$env:RUN_INTEGRATION=1; .venv\Scripts\python -m pytest tests\test_integration_real.py -v -s
```

## 目录结构

```
smart-kb-agent/
├── app/
│   ├── factory.py    应用工厂（create_app，测试 import 它不产生副作用）
│   ├── main.py       入口：uvicorn app.main:app
│   ├── api/          路由层（auth/upload/ask/documents/conversations/evaluation）+ 限流
│   ├── services/     业务层（router/chat/rag/agent/ingestion/auth/conversation）
│   ├── core/         能力组件（embedding/vector_store/llm_client/reranker/tools/prompt/hf_cache）
│   ├── models/       MySQL DAO + Pydantic 模型
│   ├── evaluation/   评测 runner
│   └── utils/        解析（PDF/Word/Excel/CSV/OCR）/切分/日志/异常
├── mcp_server/       MCP server（把知识库暴露给 MCP 客户端，见其 README）
├── migrations/       Alembic 数据库迁移
├── scripts/          init_env · rebuild_kb · open_when_ready · run_evaluation · rerank_ab · benchmark · gen_corpus
├── docs/             完整项目文档
├── data/
│   ├── kb/           标准知识库语料（纳入版本管理，保证评测可复现）
│   ├── evaluation/   评测测试集
│   └── documents/    运行时上传落盘目录（gitignore）
└── tests/            pytest 测试套件（conftest 提供全离线隔离）
```

## MCP 接入（可选）

除 HTTP API 外，项目还可作为 **MCP server** 使用，让 Claude Desktop / Cursor 这类客户端直接调用知识库：

```jsonc
{
  "mcpServers": {
    "smart-kb-agent": {
      "command": "D:\\Projects\\smart-kb-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "D:\\Projects\\smart-kb-agent"
    }
  }
}
```

暴露两个工具：`knowledge_search`（检索片段）与 `ask_knowledge_base`（问答 + 引用）。
与主服务共用同一份 `.env` 与向量库；`ENABLE_MCP=false` 可关闭。详见 [mcp_server/README.md](mcp_server/README.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/requirements.md](docs/requirements.md) | 需求规格 |
| [docs/architecture.md](docs/architecture.md) | 架构设计 |
| [docs/database-design.md](docs/database-design.md) | 数据库设计（含 V2 多租户变更与迁移策略） |
| [docs/api.md](docs/api.md) | API 契约 |
| [docs/decisions.md](docs/decisions.md) | 架构决策（ADR） |
| [docs/testing.md](docs/testing.md) | 测试方案与验证记录 |
| [docs/deployment.md](docs/deployment.md) | 部署与排障（含 Alembic 迁移手册） |
| [docs/change-log.md](docs/change-log.md) | 变更记录与 backlog |
| [docs/v2-plan.md](docs/v2-plan.md) | V2 作战计划（角色分工 / 设计 / 验收标准） |
| [docs/retrospective.md](docs/retrospective.md) | 项目复盘 |
| [docs/audit.md](docs/audit.md) | 复用审计报告 |
| [docs/review-v1-audit.md](docs/review-v1-audit.md) | V1 代码审计与改进建议 |