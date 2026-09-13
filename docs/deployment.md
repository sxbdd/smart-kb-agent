# 部署与排障（Deployment）

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| Python | 3.10+ |
| MySQL | 8.x（默认 127.0.0.1:3306） |
| 内存 | ≥ 4GB（本地 Embedding 模型约 100MB） |
| GPU | 不需要（CPU 可跑） |

## 2. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## 3. 配置 .env

```powershell
# 自动生成 .env 并写入随机 JWT_SECRET
.venv\Scripts\python scripts\init_env.py
```

然后至少填：
- `LLM_API_KEY`（DeepSeek 等 OpenAI 兼容服务的 key）
- `MYSQL_PASSWORD`（本机 MySQL 密码）

> ⚠️ `JWT_SECRET` **必填**。为空或仍为 `change-me` 等占位值时，服务会**拒绝启动**
> （避免静默降级成"任何人可伪造 token"）。`init_env.py` 会自动生成 64 位随机值。

## 4. 启动

```powershell
# 一键（自动建 venv、装依赖、启动）
start.bat

# 或手动
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

访问 http://127.0.0.1:8000/ 使用前端；/docs 查看 Swagger。

> **自动打开浏览器的时机**：`start.bat` 不再"先开浏览器再起服务"，而是后台启动
> `scripts/open_when_ready.py` 轮询 `/healthz`，**服务真正就绪后才打开**。
> 原因是应用启动时要 import `app.main` 并加载 bge 模型（数秒到十几秒），
> 此期间端口还没监听，抢跑就会看到 `ERR_CONNECTION_REFUSED`。
> 等待日志写在 `data/startup.log`；等待超时（默认 600s）会弹提示框。
> 手动验证等待器：`.venv\Scripts\python scripts\open_when_ready.py --dry-run`。

## 5. 初始化知识库

```powershell
# 先看会删什么
.venv\Scripts\python scripts\rebuild_kb.py --dry-run
# 执行（自动备份到 data/backups/）
.venv\Scripts\python scripts\rebuild_kb.py
```

语料来源是纳入版本管理的 `data/kb/`。**不要**用测试脚本往真实知识库里灌数据 ——
历史上正是这样让 47 个 chunk 里 39 个变成了项目自身文档。

## 6. 排障

| 现象 | 原因与处理 |
| --- | --- |
| 启动报 `JWT_SECRET 未配置或仍为占位值` | 运行 `python scripts/init_env.py` 生成随机密钥 |
| 启动日志 `MySQL 初始化失败 (1045)` | `.env` 里 `MYSQL_PASSWORD` 不对；服务仍可启动，但 DB 功能不可用 |
| 首次启动很慢 | 本地 Embedding 模型首次下载；设置 `HF_ENDPOINT=https://hf-mirror.com` 用国内镜像 |
| 后续启动仍然慢 | 每次启动往返 HuggingFace 做版本校验。模型已缓存时本项目会自动置 `HF_HUB_OFFLINE=1`（见 `app/core/hf_cache.py`）；如需强制在线校验可设 `HF_OFFLINE_AUTO=false` |
| 8000 端口被占用 | `netstat -ano | findstr :8000` 找 PID，`taskkill /PID <pid> /F` |
| LLM 报 401/402 | `LLM_API_KEY` 无效或余额不足 |
| 回答"无法回答" | 检索未命中或文档未上传，先检查 `/documents` |
| 登录突然返回 429 | 触发了 `/auth/*` 限流（默认每 IP 每分钟 10 次），调 `AUTH_RATE_LIMIT_PER_MINUTE` |
| 上传返回 413 | 超过 `MAX_UPLOAD_MB`（默认 20MB） |
| 浏览器报 `ERR_CONNECTION_REFUSED` | 浏览器在服务就绪前就打开了。**现已修复**：`start.bat` 会等 `/healthz` 就绪再开（见 §4）。若仍遇到，看 `data/startup.log` 确认等待器是否在跑；手动打开时先等启动窗口出现 `Application startup complete` |
| 日志里 `GET /favicon.ico 404` | 前端已用 `<link rel="icon" href="data:,">` 阻止浏览器请求；若仍有 404 属浏览器直连测试，可忽略 |
| 报 `LLM 调用失败：LLM 返回空内容` | **推理类模型（如 deepseek-v4-flash）会先输出 `reasoning_content`**，`LLM_MAX_TOKENS` 给小了会被思考吃光、`content` 为空且 `finish_reason=length`。客户端已会自动加倍预算重试，但仍建议 `LLM_MAX_TOKENS>=4096` |
| 评测整体失败 | 旧版本单题 LLM 抖动会中断整轮评测；现已逐题捕获并计入 `error_count`，看返回里的 `error_count` 与逐题 `error` 字段定位 |
| `pip-audit` 报 `UnicodeDecodeError` | requirements 文件里出现了非 ASCII 注释（该工具按本地代码页解码无 BOM 文件）。**保持 requirements 系列纯 ASCII** |

## 7. 已知限制

| 限制 | 说明 |
| --- | --- |
| 限流默认仍是**进程内**实现 | 默认 `RATE_LIMIT_BACKEND=memory`，多副本各算各的。**V2 已支持 `redis` 后端**：设 `RATE_LIMIT_BACKEND=redis` + `REDIS_URL`，Redis 不可用时自动回退进程内并告警，服务不会因 Redis 挂了而启动失败 |
| 存量库升级必须先 stamp | 旧库由 `CREATE TABLE IF NOT EXISTS` 建，可能缺 `fk_messages_conversation`，直接 `alembic upgrade head` 会失败。存量库请先 `alembic stamp 0001_initial` 再执行 `0002_multi_tenant`；全新建库直接 `upgrade head` |
| 多租户隔离为**演示级准入** | V2 已实现隔离强制点（DAO 全部带 `tenant_id` + 向量库按 metadata 过滤 + RBAC 三角色），但"用户属于哪个租户"由**注册时传 `tenant` 字段**决定（默认 `DEFAULT_TENANT`）。生产应由邀请码 / SSO / 组织关系决定（ADR-009 的定位已被 V2 取代） |
| 升级后 Chroma 老数据**检索不到** | V1 时期灌入的 chunk 没有 `tenant_id` 字段，而 Chroma 的 `where` 对"缺字段"的记录天然不匹配（且 `query` 不支持 `$exists`，实测报错）。这是 **fail-closed**（宁可查不到也不跨租户泄漏），但**升级后必须用 `rebuild_kb.py` 重灌一次索引**，否则带租户检索会返回空 |
| 并发能力有限 | 实测 4 并发后吞吐见顶（约 144 QPS），延迟随并发线性上涨；瓶颈是 CPU 上的查询向量化 |
| `chromadb` 有 5 个未修复漏洞 | `pip-audit` 报出且上游无修复版本；CI 的生产依赖集审计仅提示不阻断 |
| Rerank 默认关闭 | 实测 Recall@3 两组均 100%，首命中 +2.6pp 但延迟 133× → 维持关闭（ADR-011）；需要时设 `ENABLE_RERANK=true` |
| OCR 需要可选依赖 | `pymupdf`（PDF 转图片）+ `rapidocr-onnxruntime`（本地 OCR），未安装时**不影响文本 PDF**，只在遇到扫描件/图片时报清晰错误 |

## 8. 运维脚本速查

| 脚本 | 用途 | 是否调用 LLM |
| --- | --- | --- |
| `scripts/init_env.py` | 生成 `.env` 并写入随机 `JWT_SECRET` | 否 |
| `scripts/rebuild_kb.py` | 清空并重建知识库语料（自动备份） | 否 |
| `scripts/open_when_ready.py` | 等 `/healthz` 就绪后再开浏览器 | 否 |
| `scripts/run_evaluation.py` | 跑评测（`--repeat` 看稳定性、`--details` 看逐题） | **是** |
| `scripts/rerank_ab.py` | Rerank A/B：排序质量 + 延迟 | 否 |
| `scripts/benchmark.py` | 检索/并发性能基准（`--with-llm` 才测问答） | 否（默认） |
| `scripts/gen_corpus.py` | 生成压测/干扰语料（`--docs N --include-base`，固定种子可复现） | 否 |
| `scripts/verify_tenancy.py` | **V2 多租户真机验收**：临时库迁移往返 / 真实库升级校验 / 真实 Chroma 隔离 / 真实 HTTP 角色矩阵 | 否 |

## 9. 数据库迁移（Alembic）

```powershell
# 全新库：直接建到最新
.venv\Scripts\python -m alembic upgrade head

# 存量库（V1 升级上来）：先标记基线，再升级
.venv\Scripts\python -m alembic stamp 0001_initial
.venv\Scripts\python -m alembic upgrade head

# 查看/回滚
.venv\Scripts\python -m alembic current
.venv\Scripts\python -m alembic downgrade -1

# 离线查看将要执行的 SQL（不连库）
.venv\Scripts\python -m alembic upgrade head --sql
```

| 版本 | 内容 |
| --- | --- |
| `0001_initial` | 5 张表，与 `app/models/database.py` 的 `SCHEMA` 完全一致（含外键与索引） |
| `0002_multi_tenant` | 多租户与角色：新增 `tenant_id` / `role` 列与索引，`uk_username` → `uk_tenant_username`，补 `fk_messages_conversation` |

> 注意：`alembic.ini` **必须保持 ASCII-only** —— alembic 用本地代码页（zh-CN 下 cp936）读 ini，
> 中文注释会直接抛 `UnicodeDecodeError`（与 requirements 文件同一个坑）。

### 9.1 存量库升级后的两件必做事（少一件服务就不可用）

`0002_multi_tenant` 只改表结构，**不会**替你处理下面两件事：

**① 提升一个管理员。** 迁移把 `role` 的默认值定为 `user`，所以升级完**没有任何 admin**，
而删文档 / 跑评测 / 用户管理都要求 admin：

```sql
-- 二选一：把已有账号提成 admin
UPDATE users SET role = 'admin' WHERE username = '<你的账号>' AND tenant_id = 'default';
-- 或者：在 .env 里设 BOOTSTRAP_ADMIN_USERNAME=<用户名>，然后用该用户名注册一个新账号
```

**② 重灌一次向量库。** V1 时期灌进 Chroma 的 chunk **没有 `tenant_id` metadata**，
而 `ChromaVectorStore.query(..., tenant_id=...)` 会加 `where={"tenant_id": ...}` ——
缺字段的老数据**永远匹配不上**（`where` 不支持 `$exists`，Chroma 实测直接报错，
所以"缺字段视为 default"这条兼容只能在内存实现里做）。不重灌的表现是**检索结果为空**：

```powershell
.venv\Scripts\python scripts\rebuild_kb.py --yes    # 会先备份到 data/backups/
```

### 9.2 升级后自检（真机验收）

```powershell
.venv\Scripts\python scripts\verify_tenancy.py --part all
```

四个部分各自可单独跑，全部成功才返回 0：

| 部分 | 验证内容 |
| --- | --- |
| `temp-db` | 在临时库跑 `0001 → 0002 → base` 往返，用 `information_schema` 校验列 / 索引 / 唯一键 / 外键 |
| `upgrade` | **真实库**：先逻辑备份 → `stamp` → `upgrade` → 校验结构、存量数据零丢失、重复 upgrade 幂等 |
| `chroma` | 真实 Chroma 双租户隔离（含"缺 `tenant_id` 老数据不可见"这条已知偏差的固化断言） |
| `api` | 临时库 + 真实 Chroma 上跑真实 HTTP 角色矩阵与跨租户隔离 |

> 本机实测（2026-09-14）：四部分共 **65 项断言全部通过**，升级前后行数不变
> （users 4 / documents 6 / conversations 6 / messages 14 / evaluation_runs 6）。