# 架构设计说明书（Architecture）

> 项目：smart-kb-agent　｜　原则：分层、可测试、可解释，避免过度工程化。

## 1. 架构目标

用最小复杂度真实解决"企业知识库问答"问题：检索准确、答案可追溯、无依据不编造、效果可量化。

## 2. 技术选型

| 层 | 选型 | 理由 |
| --- | --- | --- |
| Web 框架 | FastAPI | 自动 OpenAPI/Swagger，类型校验 |
| LLM | OpenAI 兼容接口（默认 DeepSeek） | 一套代码切换服务商 |
| Embedding | sentence-transformers（bge-small-zh-v1.5） | 本地、免费、中文效果好 |
| 向量库 | Chroma | 轻量、可持久化、接口可替换 |
| 关系库 | MySQL | 事务、稳定、主流 |
| 鉴权 | JWT（PyJWT）+ PBKDF2 | 无状态、标准做法 |
| 文档解析 | pypdf / python-docx / markdown | 覆盖 PDF/DOCX/MD/TXT |

## 3. 分层结构

```
main（入口）→ factory（应用工厂）→ api（路由层）→ services（业务层）→ core（能力组件）+ models（数据）+ utils（工具）
```

- `main.py`：仅 `app = create_app()`，供 `uvicorn app.main:app` 使用（**不承载逻辑**）
- `factory.py`：`create_app(cfg)` 组装 FastAPI 实例与异常处理；单独成文件是为了消除
  `import app.main` 的容器构造副作用（测试 import 它不会加载模型/连库）
- `api/`：路由（auth / upload / ask / documents / conversations / evaluation）+ 鉴权依赖 deps + 限流 ratelimit
- `services/`：router（意图路由）、chat、rag、agent、ingestion、auth、conversation
- `core/`：embedding、vector_store、llm_client、reranker、tools、prompt_templates、hf_cache
- `models/`：database（MySQL DAO）、schemas（Pydantic）
- `evaluation/`：评测 runner
- `utils/`：document_parser、text_splitter、logger、exceptions
- `scripts/`：init_env（初始化 .env 与 JWT_SECRET）、rebuild_kb（重建知识库）

> **配置读取时机**：`Settings` 的字段全部用 `default_factory`，在**实例化时**读环境变量，
> 而不是 import 时固化。这让测试可以 `dataclasses.replace(Settings(), ...)` 构造任意配置，
> 不再受 import 顺序影响（历史坑见 docs/review-v1-audit.md §2.3）。
> `JWT_SECRET` 缺失或为占位值时构造直接失败（fail-fast）。

## 4. 核心数据流

```
用户问题
   ↓
Router（规则优先 + LLM 分类 + RAG 兜底）
   ├─→ 普通对话 Chat   → 直接 LLM 回答（无引用）
   ├─→ RAG Workflow    → 检索 Top-K → 组装上下文 → LLM 生成 → 回答 + 引用
   └─→ Agent           → ReAct 循环 → Tools（knowledge_search / calculator）→ 回答 + 引用
   ↓
最终回答（RAG / Agent 带引用）
```

## 5. 关键机制

### 5.1 Router（意图路由）
- **规则优先**：纯计算表达式 → Agent；明显闲聊 → Chat；知识库关键词 → RAG；
- **LLM 分类**（`ROUTER_ENABLE_LLM` 开关，默认关）：规则判不出时才调用，减少 LLM 调用；
- **兜底**：无法判断 → RAG（最稳、可追溯）。

### 5.2 RAG Workflow
Query 向量化 → Chroma 检索 Top-K →（可选 Rerank）→ 组装 Prompt（系统指令 + 片段 + 历史 + 问题）→ LLM 生成 → 提取引用来源。

> 检索只做 top-k，**没有相似度阈值**：即使全部片段都不相关也会返回结果，
> "拒答"完全依赖 Prompt 约束。这是有意的取舍（保留召回），代价是依赖模型守规矩。

### 5.3 Agent（ReAct + 工具）
- **引用硬约束**：涉及知识库事实必须先经 knowledge_search 取得依据；无依据不得编造引用，应明确拒答；
- ReAct 循环：Thought → Action → Action Input → Observation，最多 5 轮；
- 工具安全：calculator 用 AST 白名单（禁 eval），防代码注入。

### 5.4 防幻觉与拒答
Prompt 强制"只根据提供的文档回答，无相关内容则拒答"，并用越界题评测验证。

## 6. 部署拓扑

```
浏览器 → FastAPI(:8000) → MySQL(:3306)
                        └→ Chroma（本地持久化）
                        └→ LLM API（DeepSeek）
```

## 7. 生产风险与应对

| 风险 | 应对 |
| --- | --- |
| LLM API 不稳定 | 空返回自动重试（最多 3 次），异常统一处理 |
| 检索不准 | 提供评测体系，用数据驱动调 chunk / top_k / rerank |
| 大模型幻觉 | Prompt 硬约束 + 引用溯源 + 拒答评测 |
| 密钥泄露 | 密钥仅存 .env（gitignore），不入库；JWT_SECRET 缺失即拒绝启动 |
| 无外网环境 | hash / memory / fake 回退组件，链路可离线跑通 |
| 请求放大（超长 prompt / 超大 top_k / 大文件） | `MAX_QUESTION_CHARS` / `MAX_TOP_K` / `MAX_UPLOAD_MB` 边界 |
| 暴力破解与 CPU 放大 | `/auth/*` 按 IP 滑动窗口限流（进程内，多副本需换共享存储） |
| XSS | 前端 `marked.parse` 结果经本地 DOMPurify 消毒后再入 DOM |
| 冷启动依赖外网 | `app/core/hf_cache.py`：模型已缓存则自动离线加载 |