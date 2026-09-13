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

**V1 完成（P0 + P1）+ V1.1 审计整改**。
V1.1 做了一轮以实际执行为依据的审计与整改：修掉 16 项缺陷、把测试从脚本升级为
**144 个 pytest 用例 + CI**、重建了被测试污染的语料库，并把评测语料扩到
**6 个文档（含 2 个近邻干扰）**、测试集扩到 **47 题**。详见
[docs/review-v1-audit.md](docs/review-v1-audit.md) 与 [docs/change-log.md](docs/change-log.md)。

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
│   └── utils/        解析/切分/日志/异常
├── scripts/          init_env.py（初始化 .env）、rebuild_kb.py（重建知识库）
├── docs/             完整项目文档
├── data/
│   ├── kb/           标准知识库语料（纳入版本管理，保证评测可复现）
│   ├── evaluation/   评测测试集
│   └── documents/    运行时上传落盘目录（gitignore）
└── tests/            pytest 测试套件（conftest 提供全离线隔离）
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
| [docs/review-v1-audit.md](docs/review-v1-audit.md) | V1 代码审计与改进建议 |