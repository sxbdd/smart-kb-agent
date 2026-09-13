# 代码审计与改进建议（V1 Review）

> 审计对象：smart-kb-agent V1（P0 + P1）
> 审计方式：全部结论均来自**实际执行**（shell 可用，非静态阅读）
> 环境：Python 3.12.10 / MySQL 8 已连通 / Chroma 已建索引 / DeepSeek 已配置

## 0. 结论速览

**功能已完成，可信度尚未完成。**

代码层面 P0 + P1 功能闭环完整、分层清晰、ADR 与文档体系远超一般个人项目；真正的短板不在功能，而在三处：**测试套件部分失效**、**演示数据被测试污染导致核心卖点证据链不成立**、**边界与安全校验普遍缺失**。

| 维度 | 评分 | 依据 |
| --- | --- | --- |
| 功能完整度 | ★★★★☆ | 三路分发 / 引用 / 拒答 / 评测 / 多轮 / DOCX 全部可运行 |
| 架构与分层 | ★★★★★ | 接口抽象 + 依赖容器 + ADR 记录，可替换性真实存在 |
| 工程规范 | ★★★★☆ | 统一异常、日志、类型注解、文档齐全 |
| 可验证性 | ★★☆☆☆ | 测试脚本部分失效、pytest 未安装、无 CI、语料被污染 |
| 健壮性/安全 | ★★☆☆☆ | 输入无上限、上传无大小限制、登录无限流 |
| 可运维性 | ★★★☆☆ | 无健康检查细节、无指标、无结构化日志 |

## 1. 环境与运行基线（实测）

| 项 | 结果 |
| --- | --- |
| Python | 3.12.10（`.venv`） |
| 代码量 | **37 个 .py / 2012 行** |
| `compileall app tests` | **exit 0**（无语法错误） |
| 应用冷启动 | **成功**，导入 13.8s（瓶颈是模型加载） |
| 路由注册 | 13 个 route 条目 = 4 内置 + 3 顶层 + 6 个 `_IncludedRouter` 内含 **12 条 API**，全部正确 |
| `tests/smoke_test.py` | **exit 0**（healthz 200，Router 4/4 分类正确） |
| `tests/test_rag_e2e.py` | **exit 0**（1 chunk 入库、检索命中、回答带 1 个来源 score=0.267） |
| MySQL | 已连通：users=4 / documents=7 / conversations=6 / messages=16 / evaluation_runs=3 |
| 评测历史 | 3 条，**全部满分**（total=5, keyword/source/refusal/overall 均 1.0） |

## 2. 缺陷清单（全部已实测复现）

### P0 — 会直接坏掉

#### 2.1 评测默认测试集路径不存在 → 不传参必 500
- **位置**：`app/evaluation/runner.py:8`
- **事实**：`DEFAULT_TEST_SET` 指向 `data/evaluation/test_set.json`，磁盘上**不存在**（实际只有 `test_set_smart.json`）
- **复现**：`run_evaluation(None)` → `FileNotFoundError: ...data\evaluation\test_set.json`
- **影响**：`POST /evaluation/run` 不带 body（或前端/外部调用方不传 `test_set_path`）直接 500
- **加重**：`docs/api.md:65` 写的默认值恰恰是 `test_set_smart.json` → **文档与代码不一致**，掩盖了这个 bug
- **修复**：把常量改为 `test_set_smart.json`

#### 2.2 M2/M3 测试脚本在加入鉴权后已失效
- **位置 + 复现**：
  | 脚本 | 失败点 | 实测 |
  | --- | --- | --- |
  | `tests/test_real_rag.py:32` | `POST /upload` 未带 Authorization | **401** `{"detail":"未登录"}`，`AssertionError` |
  | `tests/test_m3_agent.py:18` | `POST /ask` 未带 Authorization | **401**，`mode: None`，`AssertionError: None` |
- **根因**：这两个脚本写于 M2/M3，早于 P0 引入的 `Depends(get_current_user)`，之后未同步更新
- **影响**：`docs/testing.md` 的"真实验证记录"**已不可复现**；测试套件处于"部分红"状态，且没有任何机制会告诉你它红了
- **修复**：补 `Authorization` 头（照 `test_p0_auth_eval.py` 的写法）

#### 2.3 pytest 实际未安装
- **事实**：`requirements-dev.txt` 声明 `-r requirements.txt` + `pytest>=8.0`，但 `.venv` 中 `python -m pytest` → **`No module named pytest`**
- **影响**：6 个测试脚本只能 `python tests/xxx.py` 手跑；无法用 `pytest -k` 选择、无 fixture、无参数化
- **连带**：项目**没有** `pytest.ini` / `conftest.py` / `pyproject.toml`
- **更深的风险**：`smoke_test.py:7-9`、`test_rag_e2e.py:8-10` 靠 `os.environ` 切回退组件，而 `config.py:94` 的 `settings` 是 **import 时固化的模块级单例**。一旦用 pytest 一次收集多个文件，**谁先 import 谁决定全局配置** → 真实测试会被 fake 组件污染。没有 conftest 就无法隔离
- **修复**：装 pytest + 加 `conftest.py` 把回退组件改用 monkeypatch/fixture 注入

### P1 — 正确性与数据

#### 2.4 删除会话留下孤儿消息
- **位置**：`app/models/database.py:212` `delete_conversation()` 只 `DELETE FROM conversations`
- **事实**：`messages` 表在 `SCHEMA` 中**没有外键、没有级联**；实测 **孤儿消息 = 2 条**
- **影响**：数据无限膨胀；`messages` 与 `conversations` 长期不一致
- **修复**：删除会话时同事务删消息，或给 `messages.conversation_id` 加外键 `ON DELETE CASCADE`

#### 2.5 语料被污染，向量索引严重失衡（最影响作品可信度）
- **Chroma `kb_documents` 实测构成**：
  ```
  total chunks: 47
     39  testing.md          ← 83%
      6  decisions.md
      1  员工考勤制度.txt
      1  员工福利制度.docx
  ```
- **MySQL `documents` 7 行**：3× `testing.md` + 2× `decisions.md` + 1 txt + 1 docx
- **事实**：真实 HR 制度内容**只有 2 个 chunk**；83% 是**项目自己的文档**，且 `testing.md` 被重复上传 3 次
- **影响链**：
  1. 检索结果被项目文档主导，评测数字（"5 题 100%"）**不再具有说服力**
  2. 评测跑的是 `/ask`→`rag.answer`，索引里塞满 `testing.md` 仍拿 100%，说明测试集**区分度不足**（题目过于"干净"，答案在唯一命中的 chunk 里）
  3. `ADR-011` 的结论建立在"**单文档单 chunk，重排无发挥空间**"的前提上 —— 现在 47 chunk / 4 文档，**该前提已不成立**，"V1 保持不启用 Rerank"不该继续沿用
- **成因**：`test_p1.py:29` 上传 docx、`test_real_rag.py:30` 上传 txt，**测试上传后从不清库**
- **修复**：清空 Chroma + MySQL 的 `documents`/`messages`，仅保留一套干净制度语料，重跑评测取真实数字；测试改为用后即删

#### 2.6 `.env.example` 最后一行格式错误
- **位置**：`.env.example:36`
- **内容**：`AGENT_MAX_ITERATIONS=5RERANK_MODEL=BAAI/bge-reranker-base` —— **缺一个换行**
- **后果**：`RERANK_MODEL` 从未出现在模板中；`_int("AGENT_MAX_ITERATIONS")` 解析 `"5RERANK..."` 失败 → 回落默认 5（恰好无害，属侥幸）
- **修复**：补换行

#### 2.7 Router 的「LLM 分类」是死代码
- **位置**：`app/container.py:49` 硬编码 `Router(llm=llm, enable_llm=False)`
- **事实**：`config.py` 中**没有**任何对应开关；`docs/architecture.md:51` 却把"LLM 分类（可选开关）"写成已实现能力
- **附带**：`app/services/router.py:43` 使用 `except Exception: pass` 静默吞掉分类异常（静态审计命中）
- **修复**：要么加 `ROUTER_ENABLE_LLM` 配置项真正接通，要么在文档里标注为"预留未启用"

### P2 — 健壮性与安全

#### 2.8 输入无任何上限（实测）
- **复现**：
  ```
  AskRequest(question="x"*500_000)  → 被接受，len=500000
  AskRequest(question="hi", top_k=1_000_000) → 被接受
  ```
- **位置**：`app/models/schemas.py:43,45` —— `question` 只有 `min_length=1`，`top_k` 是裸 `Optional[int]`
- **影响**：单次请求可塞入 50 万字 prompt（直接推高 token 成本、可能触发 LLM 报错）；`top_k=10^6` 会让 Chroma 全量返回并构造巨型上下文 → **成本/内存/DoS 三重风险**
- **修复**：`question` 加 `max_length`（如 2000）；`top_k` 加 `ge=1, le=20`

#### 2.9 上传无文件大小限制
- **位置**：`app/api/routes_upload.py:33` `dest.write_bytes(await file.read())`
- **事实**：整个文件读进内存，无大小校验、无流式写入
- **影响**：单个大文件即可打满内存
- **修复**：分块读取 + 上限校验（如 20MB），超限返回 413

#### 2.10 登录/注册无速率限制
- **事实**：`routes_auth.py` 无任何限流；PBKDF2 迭代 12 万次
- **影响**：密码可暴力枚举；且**每次尝试都要 12 万次哈希** → 攻击者可用少量请求打满 CPU（放大式 DoS）
- **修复**：按 IP/用户名限流 + 失败退避

#### 2.11 `JWT_SECRET` 代码默认值不安全
- **位置**：`app/config.py:74` 默认 `"change-me"`
- **现状**：实测 `.env` 中已配置强随机值（安全），但**代码默认仍是弱值**
- **风险**：换机器 / 丢失 `.env` / CI 环境 → 静默降级为**任何人可伪造 token**
- **修复**：`JWT_SECRET` 缺失或等于 `change-me` 时**直接拒绝启动**（fail-fast），不要静默兜底

#### 2.12 前端 XSS 面
- **位置**：`app/frontend/index.html:302` `marked.parse(text||"",{breaks:true,gfm:true})` 结果直接 `innerHTML`
- **事实**：用户输入走了 `escapeHtml()`（`:215`），但**助手回答没有做任何 sanitize**
- **影响**：若文档内容含 HTML（如 `<img src=x onerror=...>`）且被 LLM 回显，即可在浏览器执行
- **修复**：引入 DOMPurify 对 `marked.parse` 结果消毒；或配置 marked + 白名单

#### 2.13 其他
- `MYSQL_PASSWORD=root`（弱口令，本地开发可接受但要登记）
- 无 CORS 中间件（实测 `user_middleware` 为空）—— 当前同源无碍，前后端分离时会直接 403
- 单租户：所有路由的 `user_id` **只鉴权、不做数据隔离**（ADR-009 有意为之，但文档未明确写"所有用户共享同一知识库"）

### P3 — 工程与体验

| # | 问题 | 位置 | 说明 |
| --- | --- | --- | --- |
| 2.14 | **冷启动每次都访问 HuggingFace** | 启动日志 | 几十个 HEAD/GET 请求，导入耗时 **13.8s**；`start.bat` 的"已缓存离线秒开"分支实际未生效。设 `HF_HUB_OFFLINE=1` 即可 |
| 2.15 | 未使用的 import | `main.py:10` `settings`；`vector_store.py:6` `field`；`router.py:5` `Optional` | 静态审计命中（`from __future__ import annotations` 是误报，不算） |
| 2.16 | 未使用的依赖 | `requirements.txt:12` `markdown>=3.6` | `document_parser` 把 `.md` 当纯文本读，从未 import markdown |
| 2.17 | 死代码：取消上传没做出来 | `index.html:102` `.fp-actions` 样式 + `:354` `cancelUpload()` | 无任何按钮引用 `cancelUpload()`，用户选了文件后无法取消 |
| 2.18 | 文档字段描述漏 docx | `schemas.py:21` | `file_type` 描述写 `pdf / md / txt`，实际支持 docx |
| 2.19 | Agent 效率问题 | `agent.py:27` / `:31` | 进循环前**无条件**做一次 `knowledge_search`（纯计算题 `100*1.08` 也会向量检索 + 传 1M top_k 语义）；且每轮迭代都把 agent prompt 作为 system 重复追加 → token 随轮次线性增长 |
| 2.20 | `/documents` 无服务端分页 | `routes_documents.py:22` | 全量返回，前端做客户端分页；语料变大后响应体会很大 |
| 2.21 | 异常处理粒度 | `main.py:39` | 只处理了 `AppError`；其余异常（如上面 2.1 的 `FileNotFoundError`）会以裸 500 + 通用信息返回，排障依赖日志 |

## 3. 项目所处阶段

| 视角 | 阶段判断 |
| --- | --- |
| **功能** | ✅ **V1 完成**（P0+P1 全部闭环，可运行、可演示） |
| **代码质量** | ✅ **中上**（分层、抽象、异常、注解、ADR 齐备） |
| **验证资产** | ⚠️ **未完成**（测试部分失效、pytest 未装、无 CI、语料污染导致证据链失效） |
| **可交付性** | ⚠️ **demo 态**：能跑，但"5 题 100%"这个核心卖点目前**不可复现** |
| **生产就绪** | ❌ 差得远（无多租户/权限、无限流、无大小限制、无可观测性、无迁移管理） |

**一句话定位**：这是一个「**代码已经通关、但证据没清理干净**」的作品集项目。距离"能在面试里站住脚"只差把测试和语料修好；距离"生产"还差一整个非功能层。

## 4. 改进路线（按性价比排序）

### 第一梯队：1 小时内恢复可信度（强烈建议先做）
1. 修 `runner.py:8` 的默认测试集路径（1 行）
2. 给 `test_real_rag.py` / `test_m3_agent.py` 补 Bearer 头
3. 安装 pytest + 新增 `conftest.py`，用 fixture 注入回退组件，消除配置污染
4. **清空 Chroma + MySQL 的 `documents`/`messages`**，只灌入一套干净制度语料
5. 重跑评测 → 拿到**真实、可复现**的指标数字（并写进 `docs/testing.md`）

### 第二梯队：1~2 天补齐边界
6. `AskRequest` 加 `question` 上限与 `top_k` 范围约束
7. 上传加分块读取 + 大小上限（413）
8. 登录/注册限流
9. `JWT_SECRET` 缺失或为默认值时 fail-fast
10. 前端引入 DOMPurify

### 第三梯队：作品集加分项（对求职最有说服力）
11. **把 6 个脚本重构成 pytest 测试套件 + GitHub Actions CI**（这一步的边际收益最高：它把"我写了测试"变成"我的测试在 CI 上绿着"）
12. 在 **47 chunk / 4 文档**语料上重做 Rerank A/B，更新 ADR-011（前提已变）
13. 评测集从 5 题扩到 30~50 题，增加分类维度（事实型/多跳/越界/多轮指代），并**加入干扰文档**提升区分度
14. 补 token/成本统计（每次 `/ask` 的 prompt/completion token 与费用）

### 第四梯队：体验与运维
15. `HF_HUB_OFFLINE=1` 修冷启动（13.8s → 预计 <3s）
16. `/documents` 服务端分页
17. 流式输出（SSE，V2 backlog 已列）
18. 结构化日志 + 关键指标（检索耗时、命中率、LLM 重试次数）
19. 清理未使用 import / 依赖 / 死代码（2.15~2.18）

## 5. 值得保留的优点（不要在重构中丢掉）

- **回退组件设计真实可用**：`hash` / `memory` / `fake` 让全链路无外网可跑，`test_rag_e2e.py` 直接组装服务绕开 API 与 DB —— 这是很专业的可测性设计
- **`calculator` 用 AST 白名单**（`tools.py:31-46`）而非 `eval`，安全意识正确
- **`_parse` 处理了真实 LLM 的坏格式**（`agent.py:84-88`：答案直接写在 `Action: Final Answer` 后、无 `Action Input` 行）—— 这是只有真机跑过才会发现的修复
- **LLM 空返回自动重试 3 次 + 退避**（`llm_client.py:37-60`）
- **PBKDF2-SHA256 12 万次 + `secrets.compare_digest` 常量时间比较**（`auth.py:12-27`）
- **上传路径穿越已防**（`routes_upload.py:28` `Path(file.filename).name`）
- **全部 SQL 参数化**，无字符串拼接（`database.py` 通篇 `%s`）
- **文本切分实测健康**：128 字 → 1 chunk；1800 字 → 11 chunk（192~198 字），无过短尾块
- **文档体系**（10 篇 + ADR + 复盘）与 **`docs/` 单一事实来源**约定（如建表 SQL 只在 `SCHEMA`）

## 6. 附：本次审计执行过的验证

| 验证项 | 命令/方式 | 结果 |
| --- | --- | --- |
| 全量语法 | `python -m compileall -q app tests` | exit 0 |
| 静态审计（未用 import / 裸 except / TODO / 长函数） | 临时 AST 脚本 | 见 2.7 / 2.15 |
| 离线链路 | `python tests/smoke_test.py`、`test_rag_e2e.py` | 均 exit 0 |
| 冷启动 | `from app.main import app` | 成功，13.8s |
| 路由注册 | 枚举 `app.routes` + 各 router | 12 条 API 全部正确 |
| 输入边界 | 构造 50 万字 question / top_k=10^6 | 均被接受（缺陷） |
| 切分算法 | `split_text` 两种长度 | 行为正确 |
| 评测默认路径 | `run_evaluation(None)` | `FileNotFoundError`（缺陷） |
| 陈旧测试 | 运行 `test_real_rag.py` / `test_m3_agent.py` | 401 + 断言失败（缺陷） |
| 数据状态 | MySQL 各表计数 + Chroma 元数据枚举 | 见 2.4 / 2.5 |
| 孤儿消息 | `LEFT JOIN ... WHERE c.id IS NULL` | 2 条（缺陷） |

> 审计期间创建的临时探针文件均已删除，`git status` 干净。

## 7. 更正记录（整改期间自我复核）

审计报告本身有两处结论经复核后发现不准确，在此更正——保留原文以便对照。

### 更正 1：§2.14「start.bat 的离线分支实际未生效」——判断错误

- **原文**：认为 `start.bat` 里"已缓存则离线秒开"的分支没生效。
- **事实**：该分支**判断条件是正确的** —— `%USERPROFILE%\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5`
  确实存在。我观测到网络请求，是因为审计时**绕过 start.bat 直接执行 `python -c`**，环境变量自然没设置。
- **仍然成立的实质问题**：离线只对走 start.bat 的启动方式生效，直接 `uvicorn`/`python` 启动、
  pytest、Docker 等路径仍会往返 HuggingFace。因此整改为**应用层自动离线**
  （`app/core/hf_cache.py`：模型已缓存则置 `HF_HUB_OFFLINE=1`），与启动方式无关。
  已实测生效：日志出现 `[hf-cache] 检测到本地已缓存 BAAI/bge-small-zh-v1.5，切换为离线加载`，且不再有 HF 请求。

### 更正 2：§2.19「Agent 提示词 token 随轮次线性增长」——表述不准确

- **原文**：称 agent prompt 每轮重复追加导致 token 线性增长。
- **事实**：原实现是 `self.llm.chat(messages + [{"role":"system", "content": self._agent_prompt()}])`，
  即**每次调用附带一份**（常量开销），并非累积。真正的增长来自 ReAct 轮次本身（必要开销）。
- **实际改进**：① 提示词从"尾随追加"改为**首位单次注入**（system 在最前更利于指令遵循）；
  ② 纯计算题不再做一次无用的 `knowledge_search`；③ 省掉每次调用的重复拼接。
  影响是常数级而非线性，原文夸大了严重程度。

### 附：新增发现（审计时未列出）

- **检索无相似度阈值**：`vector_store.query` 只做 top-k，不做分数截断。单文档知识库里
  任何查询都会命中该文档，"拒答"完全依赖 Prompt 约束。已用
  `tests/test_tools.py::test_knowledge_search_has_no_score_threshold` 显式记录该行为，
  是否引入 `MIN_SCORE` 留待扩充语料后评估（见 `docs/change-log.md` V2 backlog）。
