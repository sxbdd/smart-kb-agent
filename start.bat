@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 ( echo 创建虚拟环境失败 & pause & exit /b 1 )
    .venv\Scripts\python -m pip install --upgrade pip
    echo [2/3] 安装 torch CPU（首次较慢）...
    .venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
)

echo [3/3] 检查并安装依赖...
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo 依赖安装失败 & pause & exit /b 1 )

if not exist .env (
    copy .env.example .env >nul
    echo [提示] 已生成 .env，请填入 LLM_API_KEY 与 MYSQL_PASSWORD 后重新运行本脚本。
    pause
    exit /b 0
)

:: BGE 模型缓存检测：已缓存 -> 完全离线秒开；未缓存 -> 走国内镜像下载
if exist "%USERPROFILE%\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5" (
    set HF_HUB_OFFLINE=1
    set TRANSFORMERS_OFFLINE=1
    echo [提示] 已检测到本地 BGE 模型，离线秒开（不访问 HuggingFace）
) else (
    set HF_ENDPOINT=https://hf-mirror.com
    echo [提示] 首次运行：将从国内镜像下载 BGE 模型（约 100MB，仅此一次）
)

echo 启动服务：http://127.0.0.1:8000/
start "" http://127.0.0.1:8000/
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload