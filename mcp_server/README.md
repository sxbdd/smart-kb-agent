# MCP server（`mcp_server/`）

把本项目的知识库能力通过 **MCP（Model Context Protocol）** 暴露给支持 MCP 的客户端
（Claude Desktop、Cursor、Continue 等），让它们直接调用本项目的检索与问答能力。

- **传输**：stdio（标准输入输出），一条消息一行（`\n` 分隔），UTF-8 编码
- **协议**：JSON-RPC 2.0，协议版本 `2024-11-05`
- **工具**：`knowledge_search`（检索）、`ask_knowledge_base`（问答 + 引用）
- **零依赖新增**：只用标准库 + 项目既有代码，不引入额外 pip 包

---

## 1. 启动方式

MCP 客户端会自己拉起进程，人工排查时也可以直接跑：

```powershell
# 在项目根目录 D:\Projects\smart-kb-agent
.venv\Scripts\python -m mcp_server            # 进入 stdio 主循环（等待客户端发消息）
.venv\Scripts\python -m mcp_server --list-tools   # 打印工具清单（JSON）后退出
```

只有 `--list-tools` 会往 stdout 打业务数据；正常运行期间 **stdout 是协议专用通道**，
日志一律走 stderr，所以可以直接观察终端而不会污染报文。

---

## 2. 挂到 MCP 客户端

### Claude Desktop（Windows）

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "smart-kb-agent": {
      "command": "D:\\Projects\\smart-kb-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "D:\\Projects\\smart-kb-agent",
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

### Cursor（项目级）

在项目里新建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "smart-kb-agent": {
      "command": "D:\\Projects\\smart-kb-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "D:\\Projects\\smart-kb-agent"
    }
  }
}
```

### macOS / Linux

```json
{
  "mcpServers": {
    "smart-kb-agent": {
      "command": "/path/to/smart-kb-agent/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/smart-kb-agent"
    }
  }
}
```

要点：

- `cwd` 必须指向项目根目录，`-m mcp_server` 才找得到包，`.env` 也才读得到（见 §5）。
- `command` 建议写 **虚拟解释器绝对路径**（`.venv` 里的），避免客户端环境里没有依赖。
- server 自身会把 stdout 强制为 **UTF-8 + LF**（Windows 上管道默认是 GBK，中文会乱码），
  `PYTHONIOENCODING=utf-8` 只是额外保险，可有可无。
- 改完配置要**重启客户端**；Claude Desktop 可在设置里看到 server 的运行状态与工具数。

---

## 3. 工具签名

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `knowledge_search` | `query: string`（必填）<br>`top_k?: integer`（1–20，可选） | 向量检索，返回片段 + 来源文档名 + 相似度。**不调用 LLM** |
| `ask_knowledge_base` | `question: string`（必填） | 检索 → 重排 → LLM 生成，返回回答正文 + 引用来源 |

两个工具的 `inputSchema`（客户端据此校验与渲染）：

```json
{
  "name": "knowledge_search",
  "description": "在企业知识库中做向量检索，返回相关片段（含来源文档名与相似度）。只检索、不生成回答。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string", "minLength": 1, "description": "检索关键词或自然语言问题" },
      "top_k": { "type": "integer", "minimum": 1, "maximum": 20, "description": "返回片段数，默认取主服务 TOP_K 配置，范围 1-20" }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "ask_knowledge_base",
  "description": "基于企业知识库检索并由 LLM 生成回答，返回回答正文与引用来源。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "question": { "type": "string", "minLength": 1, "description": "要提问的问题（自然语言）" }
    },
    "required": ["question"],
    "additionalProperties": false
  }
}
```

### 调用与返回示例

**请求**（`knowledge_search`）：

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "knowledge_search", "arguments": {"query": "出差住宿标准", "top_k": 3}}}
```

**响应**：

```json
{"jsonrpc": "2.0", "id": 2, "result": {
  "content": [{"type": "text", "text": "共检索到 1 个相关片段：\n\n[1] 来源：员工考勤制度.txt（相似度 0.6123）\n3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。"}],
  "isError": false}}
```

**请求**（`ask_knowledge_base`）：

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "ask_knowledge_base", "arguments": {"question": "出差住宿标准是多少？"}}}
```

**响应**（`text` 字段里的换行在 JSON 中是 `\n`）：

```json
{"jsonrpc": "2.0", "id": 3, "result": {
  "content": [{"type": "text", "text": "一线城市每晚不超过 600 元，其他城市不超过 450 元。\n\n引用来源：\n[1] 员工考勤制度.txt（相似度 0.6123）：3. 出差住宿标准：一线城市每晚不超过 600 元……"}],
  "isError": false}}
```

知识库为空 / 没有命中时，`knowledge_search` 返回可读提示（
「未在知识库中检索到与“xxx”相关的片段……」）且 `isError` 仍为 `false`。

---

## 4. 协议细节与错误约定

| 方法 | 行为 |
| --- | --- |
| `initialize` | 返回 `{"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": <settings.mcp_server_name>, "version": "2.0.0"}}` |
| `notifications/initialized` | **通知，不返回任何响应**（客户端靠这一点继续握手，回了反而崩） |
| `ping` | 返回 `{}` |
| `tools/list` | 返回两个工具的 `name` / `description` / `inputSchema` |
| `tools/call` | 执行工具，返回 `{"content": [{"type": "text", "text": ...}], "isError": false}` |

错误码：

| 码 | 触发条件 |
| --- | --- |
| `-32700` | 收到的行不是合法 JSON（只丢弃该行，进程继续） |
| `-32600` | 请求不是 JSON 对象 / 缺 `method` / `jsonrpc` 不是 `"2.0"` |
| `-32601` | 未知方法 |
| `-32602` | 参数非法（缺 `query`/`question`、类型错、`top_k` 越界、未知工具名……） |
| `-32603` | 工具执行内部异常。响应顶层同时带 `"isError": true`，让只看 `isError` 的客户端不会误判成功 |

**任何情况下进程都不会崩**：坏报文、参数错、向量库/LLM 报错都只影响当前这一条消息，
主循环继续读下一行。

---

## 5. 与主服务的关系

MCP server 是主服务（FastAPI，`app/`）的**另一个入口**，不是第二套实现：

- **共用配置**：和主服务读同一份 `.env`（`app/config.py` 的 `Settings`）。
  相关配置项：`MCP_SERVER_NAME`（默认 `smart-kb-agent`，即 `serverInfo.name`）与
  `ENABLE_MCP`（默认 `true`；设为 `false` 时 `python -m mcp_server` 会记录一条错误并以
  退出码 1 结束，不再进入 stdio 循环）。
- **共用向量库**：检索走的还是 `RAGService` + 配置里的向量库，所以
  **主服务上传的文档，MCP 客户端立刻就能检索到**（`VECTOR_STORE=chroma` 时共用
  `CHROMA_PERSIST_DIR`；`memory` 时则是各自进程内的独立存储，两者互相看不到）。
- **共用 LLM**：`ask_knowledge_base` 用的是同一套 LLM 配置，消耗同一份额度。
- **容器惰性构建**：`import mcp_server` 只加载协议层。直到第一次 `tools/call` 才会
  `import app.container` 并组装容器，因此：
  - 冷启动的 `initialize` / `tools/list` 是**毫秒级**的，客户端握手不会超时；
  - 首次检索会付出加载 bge 模型（+ 必要时连 MySQL / 打开 Chroma）的代价，
    这一步通常几秒，属预期现象。

---

## 6. 测试

```powershell
.venv\Scripts\python -m pytest tests\test_mcp_server.py -q     # 协议层用例
.venv\Scripts\python -m compileall -q mcp_server                # 语法检查
```

用例全部**进程内**断言（`handle_message()` 是纯函数），不起子进程；容器用
`HashEmbedding` + `InMemoryVectorStore` + `FakeLLMClient` 组装，不调用真实 LLM、
不连 MySQL、不访问外网。
