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

## 8. V2 增量（多入口 + 解析扩展 + 多租户）

V2 在保持 V1 分层不变的前提下，向外扩了三类能力：**多入口**、**解析覆盖**、**隔离与治理**。

### 8.1 多入口

```
                    ┌─ HTTP API（FastAPI）      ← 浏览器 / 脚本
知识库能力 ─────────┼─ MCP server（stdio）      ← Claude Desktop / Cursor
                    └─ Agent 工具（knowledge_search）← 内部 ReAct 循环
```

- `mcp_server/`：把同一套 `container.rag` 暴露成 MCP 工具（`knowledge_search` / `ask_knowledge_base`），
  **零业务重复实现** —— 直接复用容器，因此自动继承 `MIN_SCORE`、限流后端等全部配置。
  容器**惰性构建**：`import mcp_server` 不会加载模型或连库。
- 三者共用一份 `.env` 与向量库，行为一致。

### 8.2 解析覆盖（`app/utils/document_parser.py` + `app/utils/ocr.py`）

| 类型 | 处理 |
| --- | --- |
| PDF / DOCX / TXT / MD | V1 已有 |
| **XLSX / XLSM** | openpyxl `read_only`，按 sheet 输出 `【sheet名】` + 行内 `" \| "` 连接 |
| **CSV** | 编码回退链 + 分隔符嗅探（含全角 `；` 兜底） |
| **图片（PNG/JPG/…）** | 走 OCR |
| **扫描件 PDF** | 文本层稀疏（`< OCR_MIN_CHARS`）且 `ENABLE_OCR=true` 时渲染 + OCR |

- OCR 是**插件化**的：`OcrEngine` 协议 + `Fake`（离线测试）/ `RapidOcr`（可选依赖，惰性加载）/ `Noop`；
- **硬保证**：普通文本 PDF 绝不受 OCR 可用性影响；引擎缺失时报清晰的 `AppError` 并给出安装命令。

### 8.3 隔离与治理

| 能力 | 实现 |
| --- | --- |
| 多租户 | `tenant_id` 落在用户/文档/会话/评测 + **向量 chunk 的 metadata**，检索时 `where={"tenant_id": …}` 过滤（最易漏的一环） |
| RBAC | `viewer` / `user` / `admin` 三角色，`require_role()` 依赖统一拦截 |
| 租户准入 | **邀请码**（`invites` 表）：`REQUIRE_INVITE=true` 时注册必须持码，**租户与角色由码决定**，请求里的 `tenant` 被忽略；额度扣减是带条件的单条 `UPDATE`（并发不超发）。bootstrap 例外收窄为「`DEFAULT_TENANT` + 该租户尚无管理员」 |
| 检索阈值 | `MIN_SCORE`（0 = 关闭，保持 V1 行为）；过滤发生在 `VectorStore.query` |
| 限流后端 | `memory`（默认）/ `redis`（跨副本共享，不可用自动回退） |
| 结构演进 | Alembic 迁移（`migrations/`，当前 `0003_invites`），存量库先 `stamp 0001_initial` 再升级；新表同时进 `SCHEMA`（运行时自举）与迁移，故迁移脚本对"表已存在"要**幂等** |
| 流式输出 | `POST /ask/stream`（SSE：`meta → delta* → sources → done`，异常发 `error` 不断流）。两个实现要点：客户端必须**边收边吐**（否则首字延迟 == 总耗时），且同步生成器要用 `iterate_in_threadpool` 迭代（否则阻塞事件循环、整个服务卡住） |

> 三个"看起来能跑、真机才炸"的点（都在真机验收里被抓住，见 `docs/testing.md` §11）：
> ① `httpx.post(..., stream=True)` 是无效用法；
> ② 流式读完才 yield 让首字延迟等于总耗时；
> ③ 在事件循环里同步迭代生成器会卡死服务。
> 共同教训：**假库/桩组件通过 ≠ 真机通过**，网络与并发路径必须单独验收。