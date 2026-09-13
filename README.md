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

**V1 完成（P0 + P1）**，全部功能经真实环境（真实 Chroma + bge 模型 + DeepSeek）验证。

## 快速开始

```powershell
# 1. 配置（至少填 LLM_API_KEY 与 MYSQL_PASSWORD）
copy .env.example .env

# 2. 一键启动（自动建 venv、装依赖、起服务）
start.bat

# 或手动
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

访问 http://127.0.0.1:8000/ ｜ Swagger：http://127.0.0.1:8000/docs

## 目录结构

```
smart-kb-agent/
├── app/
│   ├── api/          路由层（auth/upload/ask/documents/conversations/evaluation）
│   ├── services/     业务层（router/chat/rag/agent/ingestion/auth/conversation）
│   ├── core/         能力组件（embedding/vector_store/llm_client/reranker/tools/prompt）
│   ├── models/       MySQL DAO + Pydantic 模型
│   ├── evaluation/   评测 runner
│   └── utils/        解析/切分/日志/异常
├── docs/             完整项目文档
├── data/             运行时数据（文档、向量库、评测集）
└── tests/            测试脚本
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/requirements.md](docs/requirements.md) | 需求规格 |
| [docs/architecture.md](docs/architecture.md) | 架构设计 |
| [docs/database-design.md](docs/database-design.md) | 数据库设计 |
| [docs/api.md](docs/api.md) | API 契约 |
| [docs/decisions.md](docs/decisions.md) | 架构决策（ADR） |
| [docs/testing.md](docs/testing.md) | 测试方案与验证记录 |
| [docs/deployment.md](docs/deployment.md) | 部署与排障 |
| [docs/change-log.md](docs/change-log.md) | 变更记录与 V2 backlog |
| [docs/retrospective.md](docs/retrospective.md) | 项目复盘 |
| [docs/audit.md](docs/audit.md) | 复用审计报告 |