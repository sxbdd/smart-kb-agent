"""`python -m mcp_server` 入口：等价于 `mcp_server.server:main`。"""
from __future__ import annotations

import sys

from mcp_server.server import main

if __name__ == "__main__":
    sys.exit(main())
