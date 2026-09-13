# API 设计说明书（API Contract）

> 风格：RESTful　｜　鉴权：除 `/healthz` `/auth/*` `/` 外均需 `Authorization: Bearer <token>`

## 1. 通用约定

- 成功返回业务数据；错误返回 `{"detail": "..."}`，状态码 4xx/5xx；
- 鉴权失败 `401`；资源不存在 `404`；参数错误 `400`；不支持的文件类型 `415`；
  文件过大 `413`；请求过于频繁 `429`；未预期异常 `500`（对外只回通用信息，详情见服务日志）。

### 请求边界（可配置，防成本/内存放大）

| 配置项 | 默认 | 约束 |
| --- | --- | --- |
| `MAX_QUESTION_CHARS` | 2000 | `question` 长度上限，超出返回 422 |
| `MAX_TOP_K` | 20 | `top_k` 取值范围 `1..MAX_TOP_K`，超出返回 422 |
| `MAX_UPLOAD_MB` | 20 | 上传文件大小上限，超出返回 413 |
| `AUTH_RATE_LIMIT_PER_MINUTE` | 10 | `/auth/*` 每 IP 每分钟请求上限，超出返回 429 |

## 2. 认证

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | /auth/register | 注册（username ≥3，password ≥6），返回 JWT | 否（限流） |
| POST | /auth/login | 登录，返回 JWT | 否（限流） |

响应：`{"token": "...", "username": "..."}`

> `/auth/*` 按客户端 IP 做滑动窗口限流（默认每分钟 10 次）。PBKDF2 迭代 12 万次，
> 不限流时少量请求即可放大为 CPU 消耗。

## 3. 文档管理

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | /upload | 上传 PDF/MD/TXT/DOCX，解析→切分→向量化→入库 | 是 |
| GET | /documents | 文档列表 | 是 |
| DELETE | /documents/{document_id} | 删除文档（元数据 + 向量） | 是 |

## 4. 智能问答

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | /ask | 提问；Router 自动分发 | 是 |

请求体：
```json
{"question": "出差住宿标准是多少？", "conversation_id": null, "top_k": 3}
```
响应：
```json
{
  "answer": "...[来源: 员工考勤制度.txt]",
  "sources": [{"document_id":"...","document_name":"员工考勤制度.txt","chunk_id":"...","content":"...","score":0.83}],
  "conversation_id": "...",
  "mode": "rag",
  "timestamp": "2026-09-13T..."
}
```
- `mode`：实际走的分支 —— `chat` / `rag` / `agent`。

## 5. 对话管理

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | /conversations | 会话列表 | 是 |
| GET | /conversations/{id} | 会话历史 | 是 |
| PATCH | /conversations/{id} | 重命名 | 是 |
| DELETE | /conversations/{id} | 删除 | 是 |

## 6. 评测

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | /evaluation/run | 运行评测，保存历史 | 是 |
| GET | /evaluation/runs | 评测历史（倒序） | 是 |

请求体：`{"test_set_path": null, "top_k": 3}`

- `test_set_path` **可省略**，默认使用 `data/evaluation/test_set_smart.json`；
- 响应含逐题 `details`，但**落库只存汇总指标**（避免数据膨胀）。
- 响应指标：`total / answerable_count / non_answerable_count / keyword_accuracy / source_accuracy / refusal_accuracy / overall_accuracy / details / run_id`

## 7. 其他

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /healthz | 健康检查 |
| GET | / | 前端单页 |
| GET | /marked.min.js | Markdown 渲染库（本地） |
| GET | /purify.min.js | DOMPurify（本地，渲染前消毒防 XSS） |
| GET | /docs | Swagger UI |