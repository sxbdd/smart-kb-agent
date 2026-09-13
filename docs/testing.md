# 测试方案（Testing）

> 原则：结论必须来自可复现的脚本与真实数据。

## 1. 测试分层

| 层 | 手段 | 目标 |
| --- | --- | --- |
| 单元 / 接口 | pytest + FastAPI TestClient | 契约、鉴权、解析、切分 |
| 端到端 | 真实向量库 + 真实 LLM | 上传→检索→生成→引用 |
| 评测 | 测试集 + 指标 | 关键词/来源/拒答/综合 |
| Agent | 真实 LLM | ReAct 循环 + 工具调用 + 引用约束 |

## 2. 回退组件（离线可测）

`EMBEDDING_PROVIDER=hash` · `VECTOR_STORE=memory` · `LLM_PROVIDER=fake`
→ 无外网、无模型也能跑通全链路，用于 CI 与本地快速验证。

## 3. 测试脚本

| 脚本 | 内容 |
| --- | --- |
| tests/smoke_test.py | 应用可创建、/healthz、Router 规则分类 |
| tests/test_rag_e2e.py | 回退组件下的 RAG 链路 |
| tests/test_real_rag.py | 真实 Chroma + bge + DeepSeek 的 RAG + 拒答 |
| tests/test_m3_agent.py | Chat / Agent 计算器 / Agent 检索 |
| tests/test_p0_auth_eval.py | 注册登录、401 鉴权、评测 |
| tests/test_p1.py | DOCX 解析、多轮对话、评测历史、前端 |

## 4. 真实验证记录

| 项 | 结果 |
| --- | --- |
| RAG 问答 | 出差住宿标准 → 带引用正确回答 ✅ |
| 拒答 | 知识库无答案 → "根据当前知识库，我无法回答这个问题" ✅ |
| Router | chat / rag / agent 三路分类正确 ✅ |
| Agent 计算器 | 100 * 1.08 → 108 ✅ |
| Agent 检索 | 返回带来源的回答 ✅ |
| 鉴权 | 无 token 401 / 带 token 200 ✅ |
| 评测（5 题） | 关键词 100% · 来源 100% · 拒答 100% · 综合 100% ✅ |
| DOCX | 上传 .docx 解析入库成功 ✅ |

## 5. 发现并修复的真实问题

1. **ReAct 解析**：真实 LLM 有时把答案直接写在 `Action: Final Answer` 后（无 `Action Input` 行）→ 修复 `_parse` 的提取逻辑；
2. **LLM 空返回**：真实 API 偶发返回空内容 → 增加自动重试（最多 3 次）。