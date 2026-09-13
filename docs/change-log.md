# 变更记录（Change Log）

> 规则：V1 范围内的需求变更记录于此；超出范围的先进 V2 backlog，V1 期间不实现。

## V1 开发记录

| 阶段 | 内容 | 结果 |
| --- | --- | --- |
| M0 | PM 定稿 + 项目经理/架构师方案 | 范围与优先级冻结 |
| M1 | 复用审计 + 骨架（config/MySQL/LLM/Prompt/Tools） | 36 文件语法过、/healthz 通 |
| M2 | 核心 RAG 链路（解析/切分/Embedding/Chroma/Top-K/引用/拒答） | 真实验证通过 |
| M3 | Router + Agent + Tool Calling | 三路验证通过，修复 2 个真 bug |
| P0 | 登录鉴权接入 + 评测 | 鉴权 401/200、评测 5 题 100% |
| P1 | DOCX + 多轮 + 评测可视化 + Rerank A/B | 全部验证通过 |

## 范围控制记录

| 决策 | 依据 |
| --- | --- |
| Rerank 不预设启用 | A/B 实测持平（单文档单 chunk），按数据决定保持关闭 |
| 不做多租户/RBAC | V1 单租户定位，避免范围膨胀 |
| 多轮对话列为 P1 | 核心 RAG 先行，多轮后置 |

## V2 backlog

OCR · Excel · 流式输出 · MCP · 多租户 · RBAC · 更大语料 Rerank 重测