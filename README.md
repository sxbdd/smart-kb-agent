# 企业级 RAG + Agent 智能知识库系统（smart-kb-agent）

面向 **HR / 行政 / 客服** 的企业知识库智能问答助手：上传制度文档，得到一个"答得准、能溯源、不知道就说不知道"的内部 AI 助手。

## 技术栈
FastAPI · RAG · Vector DB（Chroma）· Rerank · Agent（ReAct）· Tool Calling · Evaluation · MySQL · JWT

## 核心架构
```
用户问题 → Router（规则优先 + LLM 分类 + RAG 兜底）
   ├─→ 普通对话 Chat
   ├─→ RAG Workflow（检索 → 生成 → 引用）
   └─→ Agent（ReAct → Tools）
最终回答（RAG / Agent 带引用）
```

## 状态
- M1 骨架：进行中（代码骨架 + 语法检查完成）

## 目录
- `app/`：FastAPI 应用（api / services / core / models / evaluation / utils）
- `docs/`：需求 / 架构 / ADR / 审计报告
- `data/`：运行时数据（文档、向量库）

## 快速启动
见 `.env.example` 配置，然后：
```powershell
start.bat
```