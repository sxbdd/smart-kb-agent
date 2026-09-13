"""首次运行初始化 .env。

- `.env` 不存在时从 `.env.example` 复制一份
- `JWT_SECRET` 为空时写入一个强随机值（服务在 JWT_SECRET 缺失时会拒绝启动）

用法：
    python scripts/init_env.py
"""
from __future__ import annotations

import re
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

PLACEHOLDERS = {"", "change-me", "changeme", "secret", "your-secret-key"}


def main() -> int:
    if not EXAMPLE.exists():
        print(f"[错误] 找不到环境变量模板：{EXAMPLE}", file=sys.stderr)
        return 1

    if not ENV.exists():
        shutil.copyfile(EXAMPLE, ENV)
        print(f"[1/2] 已从 .env.example 生成 {ENV}")
    else:
        print(f"[1/2] {ENV} 已存在，保持不动")

    text = ENV.read_text(encoding="utf-8")
    match = re.search(r"(?m)^JWT_SECRET=(.*)$", text)
    current = match.group(1).strip() if match else ""

    if current.lower() in PLACEHOLDERS:
        new_secret = secrets.token_hex(32)
        if match:
            text = re.sub(r"(?m)^JWT_SECRET=.*$", f"JWT_SECRET={new_secret}", text, count=1)
        else:
            text += f"\nJWT_SECRET={new_secret}\n"
        ENV.write_text(text, encoding="utf-8")
        print("[2/2] 已生成随机 JWT_SECRET 并写入 .env")
    else:
        print("[2/2] JWT_SECRET 已配置，跳过")

    print("\n下一步：在 .env 中填入 LLM_API_KEY 与 MYSQL_PASSWORD，然后重新运行 start.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
