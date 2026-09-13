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

复制 `.env.example` 为 `.env`，至少填：
- `LLM_API_KEY`（DeepSeek 等 OpenAI 兼容服务的 key）
- `MYSQL_PASSWORD`（本机 MySQL 密码）

## 4. 启动

```powershell
# 一键（自动建 venv、装依赖、启动）
start.bat

# 或手动
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

访问 http://127.0.0.1:8000/ 使用前端；/docs 查看 Swagger。

## 5. 排障

| 现象 | 原因与处理 |
| --- | --- |
| 启动日志 `MySQL 初始化失败 (1045)` | `.env` 里 `MYSQL_PASSWORD` 不对；服务仍可启动，但 DB 功能不可用 |
| 首次启动很慢 | 本地 Embedding 模型首次下载；设置 `HF_ENDPOINT=https://hf-mirror.com` 用国内镜像 |
| 8000 端口被占用 | `netstat -ano | findstr :8000` 找 PID，`taskkill /PID <pid> /F` |
| LLM 报 401/402 | `LLM_API_KEY` 无效或余额不足 |
| 回答"无法回答" | 检索未命中或文档未上传，先检查 `/documents` |