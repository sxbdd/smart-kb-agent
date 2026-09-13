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

## 7. 已知限制

| 限制 | 说明 |
| --- | --- |
| 限流是**进程内**实现 | 多副本部署时各副本独立计数，需要换成 Redis 等共享存储 |
| 单租户 | 所有登录用户共享同一知识库；路由里的 `user_id` 只用于鉴权，不做数据隔离（ADR-009） |
| 无迁移工具 | 建表用 `CREATE TABLE IF NOT EXISTS`，加字段需手工处理或引入 Alembic |