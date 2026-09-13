# V2 作战计划（多角色并行）

> 目标：把 V2 backlog 的 10 项全部落地并**可验证**（不是"写完了"，而是"有实测证据"）。
> 原则延续 V1：分层清晰、离线可测、`--strict-markers`、失败先加用例、结论必须有数据。
> Lead 负责集成与提交；各角色只改自己名下的文件，避免并行冲突。

## 1. 范围与分层

| 层 | 项 | 说明 |
| --- | --- | --- |
| A（低风险、可并行） | ① Excel/CSV 解析 · ② OCR（插件化+降级） · ④ MCP server · ⑧ Redis 限流 · ⑨ Alembic 迁移 · ⑦ MIN_SCORE | 各自新增文件或向后兼容的扩展 |
| B（中等风险） | ③ 流式输出 SSE（后端+前端） · ⑩ 语料扩容后 Rerank 重测 | 触及 `llm_client` / `rag` / 前端 |
| C（高风险、必须串行且由 Lead 收口） | ⑤ 多租户 · ⑥ RBAC | 触及 DB schema、所有路由与 DAO |

## 2. 角色与文件所有权

> 铁律：**只改自己名下列出的文件**。需要改共享文件（`config.py` / `container.py` / `factory.py` / `requirements*.txt` / `docs/*`）时，**不要改**，改为在交付说明里写清"需要 Lead 做的接线"。

| 角色 | 职责 | 独占文件 |
| --- | --- | --- |
| **Lead**（我） | 集成、共享文件、文档、提交 | `app/config.py`、`app/container.py`、`app/factory.py`、`app/main.py`、`requirements*.txt`、`.env.example`、`pytest.ini`、`.github/`、`docs/*`、`README.md` |
| **Role-P** 解析工程师 | ① Excel/CSV + ② OCR 插件 | `app/utils/document_parser.py`、`app/utils/ocr.py`(新)、`tests/test_parser_splitter.py`、`tests/test_ocr.py`(新) |
| **Role-M** 集成工程师 | ④ MCP server | `mcp_server/`(新目录，含 `README.md`)、`tests/test_mcp_server.py`(新) |
| **Role-I** 基础设施工程师 | ⑧ Redis 限流 + ⑨ Alembic | `app/api/ratelimit.py`、`alembic.ini`(新)、`migrations/`(新)、`tests/test_ratelimit_backend.py`(新)、`tests/test_migrations.py`(新) |
| **Role-D** 数据/评测工程师 | ⑦ MIN_SCORE + ⑩ 语料扩容与 Rerank 重测 | `app/core/vector_store.py`、`scripts/gen_corpus.py`(新)、`scripts/rerank_ab.py`、`data/kb_ext/`(新)、`tests/test_vector_store_threshold.py`(新) |
| **Role-S** 流式工程师 | ③ SSE 流式输出 | `app/core/llm_client.py`、`app/services/rag.py`、`app/api/routes_stream.py`(新)、`app/frontend/index.html`、`tests/test_streaming.py`(新) |
| **Role-T** 平台工程师 | ⑤ 多租户 + ⑥ RBAC | `app/models/database.py`、`app/models/schemas.py`、`app/api/deps.py`、`app/api/routes_*.py`、`app/services/auth.py`、`app/services/conversation.py`、`app/services/ingestion.py`、`tests/test_tenancy.py`(新) 及受影响的既有接口用例 |

## 3. 接口约定（Lead 已先落地的共享契约）

`app/config.py` 已加入以下配置项（各角色直接读 `settings.<name>`，不要改 `config.py`）：

| 配置 | 默认 | 用途 |
| --- | --- | --- |
| `enable_ocr` / `ocr_provider` / `ocr_lang` / `ocr_min_chars` | `false` / `fake` / `ch` / `20` | OCR 开关与 provider（`rapidocr`\|`fake`\|`none`） |
| `enable_stream` / `stream_timeout_s` | `true` / `180` | SSE 开关与超时 |
| `min_score` | `0.0` | 检索相似度阈值，`0` = 不启用（保持 V1 行为） |
| `rate_limit_backend` / `redis_url` | `memory` / `redis://127.0.0.1:6379/0` | 限流后端与 Redis 地址 |
| `default_tenant` / `allow_self_register` / `bootstrap_admin_username` | `default` / `true` / 空 | 多租户与 RBAC |
| `enable_mcp` / `mcp_server_name` | `true` / `smart-kb-agent` | MCP server |

## 4. 验收方式（每项都要有证据）

| 项 | 证据 |
| --- | --- |
| ① Excel/CSV | 用例（`.xlsx` 多 sheet / 表头 / 空表；`.csv` BOM 与分隔符） |
| ② OCR | 用例：无引擎时**优雅降级**并给出明确错误；有 fake provider 时走通；扫描件 PDF 触发 OCR 分支 |
| ③ SSE | 用例：事件序列 `meta → delta* → sources → done`；前端增量渲染；异常时发 `error` 事件而非断流 |
| ④ MCP | 用例：`initialize`/`tools/list`/`tools/call` 三个 JSON-RPC 交互，工具名与 schema 正确 |
| ⑤⑥ 多租户/RBAC | 用例：跨租户不可见；角色矩阵（viewer/user/admin）逐条断言；既有 152 用例不得回归 |
| ⑦ MIN_SCORE | 用例：阈值为 0 时行为不变；调高后低分片段被过滤；对评测指标的影响用真实评测量化 |
| ⑧ Redis 限流 | 用例：fake/真实 Redis 计入跨实例共享；Redis 不可用时**自动回退 memory** 且不抛错 |
| ⑨ Alembic | 用例：`alembic upgrade head` 后表结构与 `SCHEMA` 一致；`downgrade` 可回滚 |
| ⑩ 语料扩容+重测 | 脚本生成数百 chunk 语料 → 重跑 `rerank_ab.py`，给出"何时该开 Rerank"的数据结论 |

## 5. 串行约束与风险

1. **C 层（多租户/RBAC）最后做**：它会改 DB schema 与所有路由，必须在其他角色都收口后再动，否则所有并行角色的用例都会红。
2. **Role-S 改 `llm_client.py`**：该模块被全体复用（含 8 条既有用例），流式能力必须是**新增方法**，不得改动 `chat()` 的既有行为。
3. **Role-D 改 `vector_store.py`**：`query()` 必须**向后兼容**（`min_score` 默认 `0.0`，行为与 V1 一致）。
4. **并行批次**：批次 1 = P / M / I / D；批次 2 = S；批次 3 = T（Lead 亲自做）。每批结束后 Lead 跑全量回归再放下一批。

## 6. 多租户与 RBAC 设计（Lead 亲自主持，批次 3）

### 6.1 数据模型

| 表 | 变更 |
| --- | --- |
| `users` | 新增 `tenant_id`（默认 `default`）、`role`（`viewer`\|`user`\|`admin`，默认 `user`）；唯一键由 `uk_username` 改为 **`uk_tenant_username(tenant_id, username)`**（不同租户可重名） |
| `documents` | 新增 `tenant_id` + 索引 `idx_documents_tenant` |
| `conversations` | 新增 `tenant_id` + 索引 `idx_conversations_tenant` |
| `messages` | 不变（通过会话归属隔离） |
| `evaluation_runs` | 新增 `tenant_id` + 索引（评测结果也按租户隔离） |

旧库升级：`CREATE TABLE IF NOT EXISTS` **不会**改动已存在的表，因此必须走 Alembic
（批次 2 的 `0001_initial` 负责全新建库，批次 3 追加 `0002_multi_tenant` 负责存量升级）。

### 6.2 角色矩阵

| 动作 | viewer | user | admin |
| --- | --- | --- | --- |
| `POST /ask`、读自己的会话 | ✅ | ✅ | ✅ |
| 改/删**自己的**会话 | ✅ | ✅ | ✅ |
| `POST /upload` | ❌ 403 | ✅ | ✅ |
| `DELETE /documents/{id}` | ❌ 403 | ❌ 403 | ✅ |
| `POST /evaluation/run`、`GET /evaluation/runs` | ❌ 403 | ❌ 403 | ✅ |
| `GET /admin/users`（租户内用户列表） | ❌ 403 | ❌ 403 | ✅ |
| 跨用户会话可见 | ❌ | ❌ | ✅ |

- 新注册用户默认 `role=user`（保持 V1 可用性：注册后即可上传与提问）；
- `BOOTSTRAP_ADMIN_USERNAME` 指定的用户名**首次注册时自动成为该租户 admin**；
- `ALLOW_SELF_REGISTER=false` 时关闭自助注册（403），改由 admin 通过 `POST /admin/users` 创建。

> **读法澄清（实现时发现的歧义）**：表格里 ✅✅✅ 的三行表示**三个角色都可以**，
> 不是"仅 user 及以上"。即 viewer 是"只能读知识库 + 能问答 + 能管自己的会话"，
> 唯一被限制的是**写知识库**（上传）与**管理动作**（删文档 / 跑评测 / 用户管理）。
> 会话归属只做到**租户级**：同租户内用户互相可见，跨用户可见性不在本次范围。

### 6.3 身份与强制点

1. `app/api/deps.py`：`get_current_user()` 由返回 `int` 改为返回 **`Principal(user_id, username, tenant_id, role)`**，
   并新增依赖 `require_role(*roles)`（不足则 `AppError(403)`）。**所有路由同步改为接收 `Principal`。**
2. **DAO 层强制隔离**：所有读写方法都带 `tenant_id` 参数（`list_documents(tenant_id)`、
   `get_document(doc_id, tenant_id)`、`create_conversation(title, tenant_id)`、`get_conversation(conv_id, tenant_id)` …），
   查询条件里必须出现 `tenant_id = %s`，**不允许在服务层"先查再判断"**（容易漏）。
3. **向量库隔离**：chunk 的 `metadata` 增加 `tenant_id`；检索时用 Chroma 的 `where={"tenant_id": ...}` 过滤
   （`InMemoryVectorStore` 同样按 metadata 过滤）。这是多租户 RAG 最容易漏的地方 —— 漏了就会跨租户召回。
4. `IngestionService.ingest()` 增加 `tenant_id` 参数，写入 metadata 与 `documents` 表。

### 6.4 测试策略

- `tests/conftest.py` 的 `FakeDatabase` 同步新签名（含 `tenant_id` 过滤语义），保证 148 条既有用例仍可离线运行；
- 新增 fixture：`admin_auth`（用 `BOOTSTRAP_ADMIN_USERNAME` 注册出来的 admin）、`other_tenant_auth`；
- 新增 `tests/test_tenancy.py`：
  - 跨租户不可见：A 租户上传的文档，在 B 租户的 `/documents` 与检索结果里都**查不到**；
  - 同名用户可在不同租户共存；
  - 角色矩阵逐条断言（viewer 上传 403 / user 删文档 403 / user 跑评测 403 / admin 全通）；
  - `ALLOW_SELF_REGISTER=false` 时注册被拒；
  - `/admin/users` 只返回**本租户**用户。
- 既有接口用例中，涉及"删文档 / 跑评测"的改用 `admin_auth`。

### 6.5 诚实声明（写进文档）

V2 的租户选择采用"注册时传 `tenant` 字段（默认 `DEFAULT_TENANT`）"这种**演示级**做法，
生产应由邀请码 / SSO / 组织关系决定；本设计重点是**隔离强制点**（DAO + 向量库 metadata），
而不是租户准入流程。

## 7. 批次 2：流式输出（SSE）设计

- **LLM 层**：`OpenAICompatClient` 新增 **`chat_stream(messages) -> Iterator[str]`**（`stream: true`，逐行解析 SSE
  的 `data:` 帧，取 `choices[0].delta.content`）；`chat()` 的既有行为**一个字都不改**。
  `FakeLLMClient` 也要有 `chat_stream`（按字符/词切片吐出，供离线测试）。
- **服务层**：`RAGService` 新增 `stream_answer(question, top_k, history) -> (sources, Iterator[str])`
  —— 与 `answer()` 共用检索与 Prompt 组装，只是把生成换成流式。
- **接口层**：新增 `app/api/routes_stream.py` → `POST /ask/stream`（`text/event-stream`），事件序列固定为：

  | 事件 | data | 时机 |
  | --- | --- | --- |
  | `meta` | `{"mode": "rag", "conversation_id": "..."}` | 分发完成后立刻 |
  | `delta` | `{"text": "..."}` | 每个增量片段 |
  | `sources` | `{"sources": [...]}` | 生成结束后 |
  | `done` | `{}` | 收尾 |
  | `error` | `{"detail": "..."}` | 任意阶段异常（**不能直接断流**） |

  生成结束后要像 `/ask` 一样把 user/assistant 两条消息**落库**（含 sources）。
- **前端**：`index.html` 用 `fetch` + `ReadableStream` 消费（EventSource 不支持 POST），
  逐段渲染到气泡里，最后渲染来源区块；失败要有可见提示。保留非流式回退（`ENABLE_STREAM=false` 时用原 `/ask`）。

## 8. 批次 3 补：前端 E2E（Playwright + 系统 Edge，已实测可用）

- **不需要下载浏览器**：用 `p.chromium.launch(channel="msedge")`（本机 Edge `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，已验证可启动）。
- 测试形态：在**线程里**用 `uvicorn` 起一个离线应用（`EMBEDDING_PROVIDER=hash` / `VECTOR_STORE=memory` / `LLM_PROVIDER=fake`，随机端口），
  再用 Playwright 走真实浏览器流程：登录/注册 → 看到三栏布局 → 上传文档 → 提问 → 断言**回答气泡 + 引用来源**出现 → 断言 `mode` 标签。
- 标记：新增 `e2e` marker，`pytest.ini` 登记；**默认跳过**（`RUN_E2E=1` 才跑），避免影响常规离线回归与 CI 时长。
- 若 Playwright 或 Edge 缺失则 `pytest.skip`，保证套件在别的机器上仍绿。

## 9. 批次 3 逐文件改动清单（机械化执行用）

### 9.1 `app/models/database.py` — DAO 全量带租户维度

| 表 | 方法 | 新签名要点 |
| --- | --- | --- |
| users | `create_user` | `(username, password_hash, tenant_id, role)` |
| users | `get_user_by_username` | `(username, tenant_id)` ← 同一租户内唯一 |
| users | `get_user_by_id` | `(user_id)`（主键唯一，返回行内含 tenant_id/role） |
| users | `list_users` | `(tenant_id)`（新增，供 `/admin/users`） |
| documents | `save_document` | `(..., tenant_id)` |
| documents | `list_documents` | `(tenant_id)` |
| documents | `get_document` / `delete_document` | `(doc_id, tenant_id)` |
| conversations | `create_conversation` | `(title, tenant_id)` |
| conversations | `list_conversations` | `(tenant_id)` |
| conversations | `get_conversation` / `delete_conversation` | `(conv_id, tenant_id)` |
| messages | `add_message` | 不变（归属由会话决定） |
| evaluation_runs | `save_evaluation_run` | `(metrics, tenant_id)` |
| evaluation_runs | `list_evaluation_runs` | `(tenant_id)` |

**铁律**：SQL 里必须出现 `tenant_id = %s`；禁止"先查出再在服务层判断"（容易漏）。

### 9.2 其余文件

| 文件 | 改动 |
| --- | --- |
| `app/models/schemas.py` | `AuthResponse` 增 `role`/`tenant_id`；`RegisterRequest` 增可选 `tenant`；新增 `UserInfo` |
| `app/api/deps.py` | `get_current_user()` 返回 `Principal`；新增 `require_role(*roles)` 依赖（403） |
| `app/api/routes_auth.py` | 注册：`allow_self_register` 开关、租户归一、`bootstrap_admin_username` 判定；响应带 role/tenant |
| `app/api/routes_upload.py` / `routes_documents.py` | 传 `principal.tenant_id`；**删除文档要求 admin** |
| `app/api/routes_ask.py` / `routes_conversations.py` | 传 `principal.tenant_id` |
| `app/api/routes_evaluation.py` | **要求 admin**；评测记录按租户存取 |
| `app/api/routes_admin.py`（新） | `GET /admin/users`（本租户）、`POST /admin/users`（admin 建号） |
| `app/services/auth.py` | 注册/登录带 `tenant_id`；哈希逻辑不变 |
| `app/services/conversation.py` | 全链路带 `tenant_id` |
| `app/services/ingestion.py` | `ingest(..., tenant_id)`；chunk metadata 写 `tenant_id` |
| `app/core/vector_store.py` | `query(..., tenant_id=None)`：非 None 时按 metadata 过滤（Chroma 用 `where={"tenant_id": …}`）；`delete_document` 同样带租户 |
| `app/services/rag.py` | `search()` 透传 `tenant_id`（**与 Role-S 的 `min_score` 接线共存**） |
| `app/container.py` / `app/factory.py` | 挂载 `routes_admin`、`routes_stream` |
| `tests/conftest.py` | `FakeDatabase` 同步新签名与租户过滤；新增 `admin_auth` / `other_tenant_auth` fixture |
| `tests/test_tenancy.py`（新） | 见 §6.4 |
| `migrations/versions/0002_multi_tenant.py`（新） | §6.1/§7.2 的全部 ALTER + 外键补齐；`downgrade()` 可回滚 |
| 既有接口用例 | 涉及"删文档 / 跑评测"的改用 `admin_auth` |
| `tests/test_migrations.py` | `get_heads()` 改 `["0002_multi_tenant"]`；`_table_columns()` 需能解析 `ALTER TABLE … ADD COLUMN`，否则"迁移结果 == SCHEMA"这一不变量会被误判为不成立 |

### 9.3 验收（必须实测，不能只看单测）

1. 全量 `pytest` 绿；
2. **真实库迁移**：`alembic stamp 0001_initial` → `upgrade head`，用 `information_schema` 验证
   `tenant_id`/`role`/新索引/新唯一键/外键都在，且既有 4 个用户都落进 `default` 租户；
3. **真实检索隔离**：两个租户各灌一篇文档，确认 A 租户提问**检索不到** B 租户的片段
   —— 这是多租户 RAG 最容易漏的一环，**必须用真实 Chroma 验证，不能只靠 FakeDatabase**；
4. 角色矩阵用真实 HTTP 走一遍（viewer 上传 403 / user 删文档 403 / admin 全通）。
