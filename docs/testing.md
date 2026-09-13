# 测试方案（Testing）

> 原则：结论必须来自可复现的脚本与真实数据；**不预设目标值**（ADR-006）。
> 本文是测试的单一事实来源：测试目标、分层策略、运行手册、隔离机制、用例清单、评测方案、验收标准、已知缺口与维护规范。

## 1. 测试目标与不变量

系统的四条核心不变量，测试全部围绕它们展开：

| # | 不变量 | 若被破坏的后果 |
| --- | --- | --- |
| I1 | **答案可追溯**：RAG / Agent 的回答必须带引用来源 | 卖点消失，退化成普通聊天机器人 |
| I2 | **不知道就说不知道**：知识库无依据时明确拒答，不编造 | 幻觉直接暴露给用户 |
| I3 | **三路分发正确**：chat / rag / agent 各走各路 | 计算题被当成知识问答、闲聊被强行检索 |
| I4 | **边界与鉴权有效**：无 token 401、超限 422/413/429、不越权 | 成本放大、暴力破解、数据泄漏 |

另有一条工程不变量：**测试必须可离线复现**（不依赖 MySQL / Chroma / 模型 / 外网），否则 CI 与协作都不可行。

## 2. 分层策略

`122 个测试函数 → 152 个用例`（参数化展开后）：离线 **148 passed** + 真实环境 **4 skipped**。

| 层 | 文件 | 用例数 | 依赖 | 目标 | 反馈速度 |
| --- | --- | --- | --- | --- | --- |
| L1 冒烟 | `test_smoke.py` | 4 | 无 | 应用可创建、健康检查、静态资源、路由齐备 | <1s |
| L1 单元 | `test_llm_client.py` | 8 | 无（桩 HTTP） | 空返回、**推理模型 token 截断**、重试与预算加倍 | <1s |
| L1 单元 | `test_router.py` | 19 | 无 | 意图规则矩阵、LLM 分类开关与降级 | <1s |
| L1 单元 | `test_tools.py` | 18 | 内存向量库 | 计算器 AST 沙箱、工具注册表、检索行为 | <1s |
| L1 单元 | `test_parser_splitter.py` | 20 | 无 | 解析（txt/md/docx/**pdf**/编码/损坏文件）与切分边界 | <1s |
| L2 服务 | `test_rag_offline.py` | 5 | 内存向量库 + fake LLM | 入库 → 检索 → 生成 → 引用 → 删除 全链路（I1） | <1s |
| L2 服务 | `test_agent.py` | 12 | 脚本化 LLM | ReAct 循环、工具调用、解析容错、提示词注入 | <1s |
| L3 接口 | `test_api_auth.py` | 15 | Fake DB | 注册/登录/401/限流（I4） | <1s |
| L3 接口 | `test_api_ask.py` | 14 | Fake DB + 内存向量库 | 三路分发（I3）、引用落库（I1）、输入边界、会话管理 | <1s |
| L3 接口 | `test_upload.py` | 10 | Fake DB + 内存向量库 | 上传入库、类型校验、大小上限、路径穿越、失败回滚、损坏 PDF | <1s |
| L3 接口 | `test_evaluation.py` | 19 | Fake DB + stub RAG | 指标计算正确性、默认测试集、历史落库（不含 details）、**测试集自身一致性** | <1s |
| L1 脚本 | `test_open_when_ready.py` | 4 | 本地回环 HTTP | 启动等待器：健康才返回、未就绪不误判、超时正确（`start.bat` 抢跑回归） | <1s |
| L4 端到端 | `test_integration_real.py` | 4 | **真实 MySQL + Chroma + bge + DeepSeek** | I1/I2 真机验证、Agent 工具调用、评测指标 | 分钟级、有费用 |

全量离线套件实测约 **8 秒**（含启动等待器的超时用例，它们本身要真等）。

## 3. 运行手册

```powershell
# ---- 离线（默认）----
.venv\Scripts\python -m pytest -o addopts=""    # 419 passed, 10 skipped
# 注意：pytest.ini 的 addopts 带了 -q，再叠加命令行的 -q 会变成 -qq，
# 那就**看不到汇总行**了。想看"N passed"两种办法：
#   .venv\Scripts\python -m pytest -o addopts=""      （清掉 ini 里的 addopts）
#   .venv\Scripts\python -m pytest -p no:warnings     （与 -q 叠加也仍会打印汇总）

# ---- 只装离线测试所需的最小依赖（不需要 torch / chromadb，约 20MB）----
.venv\Scripts\python -m pip install -r requirements-ci.txt

# ---- 按层/按主题选择 ----
.venv\Scripts\python -m pytest tests\test_router.py -v          # 单文件
.venv\Scripts\python -m pytest -k "evaluation or refusal"       # 关键字
.venv\Scripts\python -m pytest -m "not integration"             # 显式排除真实环境
.venv\Scripts\python -m pytest --collect-only -q                # 只看用例数

# ---- 前端 E2E（真实浏览器：Playwright + 系统 Edge；默认跳过）----
$env:RUN_E2E=1
.venv\Scripts\python -m pytest -o addopts="" tests\test_frontend_e2e.py -v
$env:RUN_E2E=$null

# ---- 真实环境端到端（默认跳过；会真实调用 LLM 并写入 MySQL）----
$env:RUN_INTEGRATION=1
.venv\Scripts\python -m pytest tests\test_integration_real.py -v -s
$env:RUN_INTEGRATION=$null

# ---- 真机验收：多租户（65 项断言，零费用、不调用 LLM）----
.venv\Scripts\python scripts\verify_tenancy.py --part all

# ---- 真机验收：真实流式 SSE（**会真实调用 LLM，消耗 token**）----
.venv\Scripts\python scripts\verify_streaming.py
.venv\Scripts\python scripts\verify_streaming.py --skip-http   # 只测 LLM 客户端层

# ---- 评测基线（调用 LLM，有费用；结果写入 evaluation_runs）----
.venv\Scripts\python scripts\run_evaluation.py --repeat 2 --details

# ---- Rerank A/B：排序质量 + 延迟（零费用，不调用 LLM）----
.venv\Scripts\python scripts\rerank_ab.py --candidates 5 --final 3

# ---- 检索/并发性能基准（零费用；加 --with-llm 才测端到端问答）----
.venv\Scripts\python scripts\benchmark.py --queries 39 --concurrency 1,4,8

# ---- 运维：清理 Chroma 孤立 HNSW 段目录（反复重建索引后会堆积；需停服执行）----
.venv\Scripts\python scripts\cleanup_chroma.py                 # 先看（dry-run）
.venv\Scripts\python scripts\cleanup_chroma.py --apply --keep-count 13
```

CI：`.github/workflows/ci.yml`，push / PR 时在 **Python 3.10 与 3.12** 上跑 `compileall` + 离线套件。

> **本机快捷方式（仅本开发机，不随仓库迁移）**：跨项目 `pytest` 启动器
> （`D:\Dev\Tools\pytest`，登记在 `D:\Dev\dev-readme.txt` §4），优先 pwsh 7、回退 5.1，
> 自动挑本项目的 `.venv` 解释器：
>
> ```powershell
> pytest -q                      # 等价于 .venv\Scripts\python -m pytest -q
> pytest -ProjectDir D:\Projects\smart-kb-agent -k evaluation
> pytest -Show                   # 查看宿主 / 解释器 / pytest 版本
> ```
>
> 其他机器与 CI 请仍用 `.venv\Scripts\python -m pytest` 写法。

## 4. 隔离机制（测试可信的前提）

| 机制 | 实现 | 解决的坑 |
| --- | --- | --- |
| 回退组件 | `conftest.py` 在 import 任何 `app.*` **之前**设置 `EMBEDDING_PROVIDER=hash` / `VECTOR_STORE=memory` / `LLM_PROVIDER=fake` | 无需模型、外网、向量库 |
| 配置实例化时读取 | `Settings` 字段用 `default_factory`，测试可 `dataclasses.replace(Settings(), ...)` 造任意配置 | **历史坑**：`settings` 曾是 import 时固化的单例，pytest 一次收集多文件时"谁先 import 谁说了算"（`docs/review-v1-audit.md` §2.3） |
| 假数据库 | `conftest.FakeDatabase` 通过 `monkeypatch.setattr("app.container.Database", ...)` 替换，签名与真实 DAO 对齐 | 鉴权 / 会话 / 评测全部离线可测 |
| 临时目录 | `settings` fixture 把 `data_dir` / `documents_dir` / `chroma_persist_dir` 指向 `tmp_path` | 测试不再污染仓库 `data/`（历史坑：脚本上传后从不清库） |
| 无 import 副作用 | `create_app` 位于 `app/factory.py`；`app/main.py` 只放 `app = create_app()` | `import app.main` 不再连带加载模型与连库 |
| 标记隔离 | `pytest.ini` 开 `--strict-markers`；真实用例打 `@pytest.mark.integration` + `skipif RUN_INTEGRATION != 1` | 破坏性/付费用例永不误跑 |

## 5. 用例清单

### L1 冒烟 `test_smoke.py`（4）
| 用例 | 守护 |
| --- | --- |
| `test_healthz` | `/healthz` 契约 `{"status":"ok"}` |
| `test_frontend_and_static_assets` | `/`、`/marked.min.js`、`/purify.min.js` 均可访问（DOMPurify 缺失会导致渲染降级） |
| `test_openapi_lists_all_business_routes` | 10 条业务路由齐备（契约回归） |
| `test_frontend_ships_both_libraries_in_html` | 前端确实加载两个库且调用 `DOMPurify.sanitize`（XSS 防线） |

### L1 单元 `test_router.py`（19）
| 用例 | 守护 |
| --- | --- |
| `test_rule_based_routing`（10 参数） | 规则矩阵：问候→chat、纯算式→agent、知识关键词→rag、不确定→兜底 rag、空串→rag |
| `test_is_calculation`（5 参数） | 算式识别边界（`100元怎么算` 不算算式） |
| `test_llm_classify_disabled_by_default` | 默认**不**调用 LLM 分类（用会抛异常的桩证明） |
| `test_llm_classify_enabled_uses_llm` | `ROUTER_ENABLE_LLM` 真正接通（原为死代码） |
| `test_llm_classify_failure_falls_back_to_rag` | LLM 异常时降级 RAG，不影响可用性 |
| `test_llm_classify_unexpected_answer_falls_back_to_rag` | 模型返回非法分类时兜底 |

### L1 单元 `test_tools.py`（18）
| 用例 | 守护 |
| --- | --- |
| `test_calculator_allows_arithmetic`（6 参数） | 四则/幂/负号正确 |
| `test_calculator_rejects_everything_else`（8 参数） | `__import__` / `open` / `__subclasses__` / lambda / 推导式 / `exec` 全部拒绝（AST 白名单，非 eval） |
| `test_tools_registry_exposes_exactly_two_tools` | 工具注册表就是 `knowledge_search` + `calculator` |
| `test_knowledge_search_on_empty_store` | 空库返回 `（未检索到相关内容）` |
| `test_knowledge_search_returns_indexed_chunk_with_filename` | 命中时带文件名与原文（引用溯源基础） |
| `test_knowledge_search_has_no_score_threshold` | **记录已知行为**：检索只做 top-k、无相似度截断，拒答依赖 Prompt（见 §9） |

### L1 单元 `test_parser_splitter.py`（20）
| 用例 | 守护 |
| --- | --- |
| `test_short_text_is_one_chunk` | 短文不切碎 |
| `test_long_text_splits_without_runt_tail` | 1800 字 → 多块且**无 <50 字的碎尾** |
| `test_paragraphs_are_merged_up_to_chunk_size` | 段落聚合不超 `chunk_size` |
| `test_invalid_parameters_rejected`（4 参数） | `chunk_size<=0` / `overlap>=chunk_size` / 负 overlap 全部报错 |
| `test_crlf_normalized` | CRLF 归一化 |
| `test_parse_txt_utf8` / `test_parse_txt_gb18030` | 编码回退链（utf-8-sig → utf-8 → gb18030） |
| `test_parse_markdown_treated_as_text` | `.md` 按纯文本读（不引入 markdown 依赖） |
| `test_parse_docx_including_tables` | DOCX 段落 + **表格行拼成 `A \| B`** |
| `test_unsupported_extension_rejected` | 不支持的扩展名 → 415 |
| `test_parse_pdf_extracts_text` | **PDF 文本可被抽取**（用例内手工构造最小 PDF，零新增依赖） |
| `test_parse_pdf_multiple_pages_all_extracted` | 多页 PDF：两页文本都在，且**页序不乱** |
| `test_parse_pdf_without_text_returns_empty` | 空白 PDF → 解析结果为空（后续入库会因此被拒） |
| `test_parse_pdf_escaped_parentheses` | PDF 字符串转义（`(a)`）不被破坏 |
| `test_parse_corrupt_pdf_raises_app_error` | **损坏 PDF → `AppError(400)`**，不是无信息的 500 |
| `test_parse_plain_text_named_pdf_raises_app_error` | 后缀是 `.pdf` 但内容是文本 → 同样 400 |
| `test_parse_corrupt_docx_raises_app_error` | 损坏 DOCX → 同样 400（与 PDF 对称） |

### L2 服务 `test_rag_offline.py`（5）
| 用例 | 守护 |
| --- | --- |
| `test_ingest_creates_chunks_and_metadata` | 入库返回 `success` + chunk 数 + 原始文件名 |
| `test_search_returns_relevant_chunk` | 检索命中且命中内容含关键词（I1 前提） |
| `test_answer_returns_citation_sources` | 回答非空且**必带来源**、score 在 0~1、含原文（I1） |
| `test_ingest_rejects_empty_document` | 空文档被拒并给出明确原因 |
| `test_delete_removes_from_vector_store` | 删除后检索不到（元数据与向量一致） |

### L2 服务 `test_agent.py`（12）
| 用例 | 守护 |
| --- | --- |
| `test_react_loop_calls_calculator_tool` | ReAct 第二轮确实带回 `Observation` 与工具结果 |
| `test_calculation_question_skips_knowledge_search` | 纯计算题**不**触发检索（省一次向量查询） |
| `test_knowledge_question_does_search_and_injects_grounding` | 知识题必检索且注入 grounding |
| `test_agent_prompt_sent_once_and_first` | 提示词只出现一次且居首（system 数量恒为 2） |
| `test_unknown_tool_does_not_crash` | 未知工具降级为 Observation |
| `test_tool_exception_is_reported_as_observation` | 工具内部异常不炸链路 |
| `test_max_iterations_returns_graceful_message` | 超轮次给友好兜底而非报错 |
| `test_parse_final_answer_without_action_input` | **真机回归**：答案直接写在 `Action: Final Answer` 后、无 `Action Input` 行 |
| `test_parse_final_answer_with_chinese_colon_and_multiline` | 中文冒号 + 多行答案 |
| `test_parse_action_input_is_json_decoded` | `Action Input` 正确 JSON 化 |
| `test_parse_non_json_action_input_stays_string` | 非 JSON 参数保持字符串 |
| `test_parse_plain_answer_has_no_action` | 无格式回答不误判出 action |

### L3 接口 `test_api_auth.py`（15）
| 用例 | 守护 |
| --- | --- |
| `test_register_returns_token` | 注册返回 token + username |
| `test_duplicate_register_conflicts` | 重名 → 409 |
| `test_login_success` / `test_login_wrong_password_401` / `test_login_unknown_user_401` | 登录三条路径 |
| `test_protected_routes_require_token`（3 参数） | `/documents`、`/conversations`、`/evaluation/runs` 无 token → 401（I4） |
| `test_ask_requires_token` / `test_evaluation_run_requires_token` | POST 类业务接口同样要鉴权 |
| `test_garbage_token_rejected` / `test_malformed_authorization_header_rejected` | 非法/畸形 token → 401 |
| `test_valid_token_grants_access` | 带 token → 200 |
| `test_register_validation_enforced` | 用户名/密码长度下限 → 422 |
| `test_auth_rate_limit_returns_429` | 连发 4 次：前 2 次 401、后 2 次 **429**（PBKDF2 放大攻击防线） |

### L3 接口 `test_api_ask.py`（14）
| 用例 | 守护 |
| --- | --- |
| `test_chat_mode` | 问候 → `mode=chat`、无来源（I3） |
| `test_rag_mode_returns_sources` | 知识题 → `mode=rag`、**必带来源与原文**（I1/I3） |
| `test_agent_mode_for_calculation` | 算式 → `mode=agent`（I3） |
| `test_oversized_question_rejected` | 超 `MAX_QUESTION_CHARS` → 422 |
| `test_empty_question_rejected` | 空问题 → 422 |
| `test_top_k_out_of_range_rejected` | `top_k` 越界（过大/为 0）→ 422 |
| `test_max_top_k_is_accepted` | 边界值可用（防"改紧后误伤"） |
| `test_multi_turn_keeps_same_conversation` | 多轮共用 `conversation_id`，历史 role 序列正确 |
| `test_assistant_message_persists_sources` | 助手消息的引用**落库**（刷新页面仍能溯源） |
| `test_ask_with_unknown_conversation_404` | 伪造 `conversation_id` → 404 |
| `test_conversation_list_shows_message_count` | 会话列表消息数为 2 |
| `test_rename_conversation` | PATCH 改名生效 |
| `test_delete_conversation_also_removes_messages` | **孤儿消息回归**：删除会话后 `count_orphan_messages()==0` |
| `test_delete_unknown_conversation_404` | 删不存在的会话 → 404 |

### L3 接口 `test_upload.py`（10）
| 用例 | 守护 |
| --- | --- |
| `test_upload_txt_creates_index` | 上传 txt → 入库并出现在 `/documents` |
| `test_upload_docx` | DOCX 走通解析链路 |
| `test_upload_unsupported_type_415` | `.xlsx` → 415 |
| `test_upload_empty_file_400` | 空文件 → 400 |
| `test_upload_too_large_413` | 超 `MAX_UPLOAD_MB` → 413，且**不留残文件、不写元数据** |
| `test_upload_rejects_empty_parsed_content` | 纯空白内容 → 400 且回滚 |
| `test_filename_path_traversal_is_neutralised` | `../../evil.txt` 被剥成 `evil.txt`，不写到目录外 |
| `test_delete_document` / `test_delete_unknown_document_404` | 删除文档与 404 |
| `test_upload_corrupt_pdf_400_not_500` | 损坏 PDF 走完整接口返回 **400**（不是 500）；同一用例顺带验证正常 PDF 能入库 |

### L3 接口 `test_evaluation.py`（19）
| 用例 | 守护 |
| --- | --- |
| `test_default_test_set_exists` | 默认测试集存在且名为 `test_set_smart.json`（**原 bug：指向不存在的 test_set.json**） |
| `test_default_test_set_schema` | 字段齐备且含越界题（拒答评测前提） |
| `test_keyword_and_source_hit` | 全命中 → 三项指标 1.0 |
| `test_keyword_miss_marks_incorrect` | 关键词漏一个即判错（all 语义） |
| `test_wrong_source_marks_incorrect` | 关键词对但来源错 → overall 0.0 |
| `test_refusal_metric` | 越界题 + 拒答话术 → `refusal_accuracy=1.0` |
| `test_non_refusal_on_unanswerable_fails` | 越界题却编答案 → `refusal_accuracy=0.0`（I2） |
| `test_details_excluded_from_saved_summary` | 接口返回 details，但**落库只存汇总**（防膨胀） |
| `test_run_evaluation_without_body_uses_default_set` | 不传 `test_set_path` 也能跑（**回归原 500**） |
| `test_evaluation_history_records_runs` | 历史记录可查 |
| `test_evaluation_end_to_end_hits_keyword_and_source` | 上传→提问→评测 打通（I1） |
| `test_test_set_is_expanded_enough` | 样本量必须落在 **30~50 题** |
| `test_ids_unique_and_categories_known` | id 唯一、分类合法且至少含 fact 与 refusal |
| `test_questions_are_unique` | 无重复问题 |
| `test_answerable_items_point_to_real_corpus_documents` | 出处必须是 `data/kb/` 里真实存在的文件名 |
| `test_answerable_items_have_keywords_present_in_gold_evidence` | 关键词必须真的出现在 `gold_evidence` 里 |
| `test_refusal_items_declare_no_source_or_keywords` | 拒答题不得声明来源/关键词 |
| `test_every_corpus_document_is_cited_by_some_question` | 每个语料文档都要有题引用（否则等于白灌） |
| `test_gold_evidence_really_exists_in_its_corpus_file` | **`gold_evidence` 必须能在对应语料文件里逐句找到** —— 防出题时凭空编造事实 |

### L1 单元 `test_llm_client.py`（8）
| 用例 | 守护 |
| --- | --- |
| `test_truncated_then_ok_retries_with_doubled_budget` | **被 `finish_reason=length` 截断时自动加倍预算重试**（推理模型的核心坑） |
| `test_doubling_is_capped_at_ceiling` | 预算封顶 `MAX_TOKEN_CEILING`，不会无限膨胀 |
| `test_truncation_error_message_is_actionable` | 失败信息含 max_tokens / 截断，可定位到配置 |
| `test_empty_without_length_does_not_inflate_budget` | 真正的空返回**不**无脑翻倍 |
| `test_api_error_retries_then_raises_with_status` | 非 200 重试后带状态码抛错 |
| `test_malformed_payload_is_retried_not_crashed` | 返回体结构异常不崩，重试可恢复 |
| `test_default_budget_is_generous_enough_for_reasoning_models` | 默认预算 ≥ 4096 |
| `test_fake_client_is_deterministic` | 离线 FakeLLM 行为稳定 |

### L1 脚本 `test_open_when_ready.py`（4）
| 用例 | 守护 |
| --- | --- |
| `test_returns_true_when_healthy` | `/healthz` 200 → 立刻返回 True |
| `test_returns_false_on_timeout_when_not_ready` | 一直未就绪 → 真的等到超时才返回 False（不是立刻放弃） |
| `test_returns_false_when_nothing_listens` | **端口无人监听（抢跑场景）→ 返回 False 且不抛异常** |
| `test_waits_for_late_readiness` | 一开始未就绪、稍后就绪 → 必须等到 True（证明是轮询） |

### L4 端到端 `test_integration_real.py`（4，默认跳过）
| 用例 | 需要 |
| --- | --- |
| `test_real_rag_answer_with_citation` | 真机 RAG：回答含 600/450 且来源为《员工考勤制度.txt》 |
| `test_real_refusal_on_unknown_question` | 真机拒答：越界题出现拒答话术（I2） |
| `test_real_agent_tool_calling` | `100 * 1.08` → `mode=agent` 且答案含 108 |
| `test_real_evaluation_metrics` | 真机评测四指标齐备 |

用例自带清理：`uploaded` fixture 在用例结束后 `DELETE /documents/{id}`。

## 5.1 V2 新增用例（按能力，共 291 条）

V1 那 168 条继续保留；V2 按新能力补了下面这些。**每条都对应一个可能漏掉的强制点**，
不是凑数用例（括号内为实测用例数）：

| 能力 | 文件（用例数） | 守住什么 |
| --- | --- | --- |
| 邀请码准入 | `test_invites.py`（30） | 无码/错码/过期/用尽全部 403；**租户与角色由码决定、body 的 tenant 与 role 都被忽略**；一次性码不超发；重名 409 不吃额度；**bootstrap 例外被收窄（不能拿它越权抢别的租户）**；平台租户才能跨租户签码；`require_invite=false` 的旧行为回归 |
| 多租户 + RBAC | `test_tenancy.py`（24） | 三层强制点各一条：路由/角色矩阵、DAO 跨租户查不到、**向量库跨租户检索不到**；`ALLOW_SELF_REGISTER=false`；`/admin/users` 只看本租户 |
| 多租户（DAO 之外） | `test_admin_routes.py`（33） | 管理面端点的角色、跨租户建号 403（不静默改写）、409 透传、密码不回显 |
| 租户 → 向量库 | `test_vector_store_tenancy.py`（39） | 三种实现（内存/Chroma/协议）行为逐条一致；`tenant_id=None` 不过滤（V1 兼容）；跨租户删不掉；**老数据缺 `tenant_id` 的行为差异被显式固化** |
| 租户 → Agent/评测 | `test_agent_tenancy.py`（10） | 租户必须穿过工具链到达 `rag.search`：用"把 `tenant_id` 声明为必填关键字"的假 rag 做签名护栏（漏传即报错，而不是悄悄退化成全局检索） |
| 检索阈值 | `test_vector_store_threshold.py`（22） | `min_score<=0` 与 V1 逐条一致；过滤发生在截断前；只减不增、顺序不变 |
| 流式 SSE | `test_streaming.py`（25） | LLM 层 10 条（含 SSE 噪声帧、`[DONE]` 截断、推理模型吃预算后翻倍、已产出后中断必须报错）+ **增量产出回归**；接口层事件序列/落库/断连兜底/404 开关 |
| DB 迁移 | `test_migrations.py`（10） | 离线 SQL 与 `SCHEMA` **逐列一致**（解析器支持 `ALTER TABLE ADD COLUMN`）；revision 链；downgrade 先子后父；真库往返（integration，默认跳过） |
| MCP server | `test_mcp_server.py`（47） | JSON-RPC 协议层（错误码 -32600/-32601/-32602/-32603）、两个工具的契约、stdio 往返、**惰性容器**、坏帧不断流 |
| Excel/CSV + OCR | `test_ocr.py`（32）、`test_parser_splitter.py`（36） | 新格式解析、OCR 引擎插件化与优雅降级、`ENABLE_OCR` 开关、经 `/upload` 的真实调用路径 |
| Redis 限流 | `test_ratelimit_backend.py`（16） | 两后端语义一致、超限不计数（DECR 回滚）、Redis 不可用时回退进程内 |
| 前端 E2E | `test_frontend_e2e.py`（3） | 真实浏览器（Playwright + 系统 Edge）：登录页→注册→三栏→上传→提问→引用来源→会话列表；计算题显示 `agent` 标签。**默认跳过**（`RUN_E2E=1`） |

### 5.2 两种"真机验收"与单测的分工

单测跑的是 `FakeDatabase` + `InMemoryVectorStore`。**假库的隔离语义是"照着真库写的"，
所以假库通过 ≠ 真库通过** —— 真机问题必须由下面两个脚本兜住（都是可重复执行的验收，不是一次性调试脚本）：

| 脚本 | 覆盖 | 实测结论 |
| --- | --- | --- |
| `scripts/verify_tenancy.py` | 临时库迁移往返 / 真实库升级零丢数据且幂等 / **真实 Chroma 双租户隔离** / 真实 MySQL+Chroma 上的 HTTP 角色矩阵 / **邀请码准入（含"body 的 tenant 被忽略"与"bootstrap 不能越权"）** | 5 部分 **81 项断言全通过** |
| `scripts/verify_streaming.py` | 真实 DeepSeek：客户端层流式非空/无 U+FFFD/首字延迟；HTTP 层事件序列、增量数、TTFT、**流式期间 `/healthz` 未被阻塞**；与非流式一致性；关闭开关后回退 | 17 项断言通过，见 §11 的 P0 记录 |

> 这两个脚本正是"真机才暴露"的价值所在：`verify_streaming.py` 第一次跑就抓出了
> **真实流式 100% 失败**的 P0（24 条离线用例全绿），详见 §11。

## 6. 评测方案

评测是"系统答得准不准"的唯一量化证据。**结论必须由你亲自跑出来**，本文只给方法与判读规则。

### 6.1 测试集与语料

| 项 | 值 |
| --- | --- |
| 默认路径 | `data/evaluation/test_set_smart.json`（代码常量 `DEFAULT_TEST_SET`） |
| 当前规模 | **47 题**：可答 39 + 越界拒答 8 |
| 分类 | `fact` 31 / `paraphrase` 5 / `multi_fact` 3 / `refusal` 8 |
| 字段 | `id` / `category` / `question` / `answerable` / `expected_keywords[]` / `expected_source_doc` / `reference` / `gold_evidence` |
| 语料 | `data/kb/` 共 **6 个文档**（`rebuild_kb.py` 灌入，索引 6 chunk） |

语料构成（**故意包含近邻干扰文档**，让检索必须在 6 个文档里认准出处）：

| 文档 | 角色 | 被引用的题目数 |
| --- | --- | --- |
| `员工考勤制度.txt` | 核心制度（年假 / 住宿 / 交通 / 餐补 / 工时） | 19 |
| `员工福利制度.docx` | 补充福利（团建 / 体检 / 生日） | 4 |
| `员工培训管理制度.txt` | 独立制度（入职培训 / 考核 / 费用 / 外部培训） | 4 |
| `办公用品与设备管理办法.txt` | 独立制度（领用 / 盘点 / 审批 / 固定资产） | 4 |
| `差旅与报销指引.txt` | **近邻干扰**：谈出差、住宿、交通费，但关键数值一律"以《员工考勤制度》为准" | 4（自己的报销时限/审批/票据题） |
| `新员工入职指引.txt` | **近邻干扰**：谈年假、考勤，同样让位给《员工考勤制度》 | 4（自己的试用期/材料/转正题） |

**扩充或修改题目时必须遵守的约束（已全部固化为测试，见 §5）**：

1. `expected_keywords` 必须是**答案中真实会出现**的子串（全部命中才算 hit，别塞同义表述）；
2. `expected_source_doc` 必须与 `data/kb/` 里的**文件名完全一致**，否则来源准确率必然掉；
3. `gold_evidence` 必须能在对应语料文件里**逐句找到**（不允许凭空编造事实）；
4. 新增语料文档后，至少有一条题引用它，否则它进不了检索路径。

> 加语料后记得重建索引，否则新文档不在库里：
> `.venv\Scripts\python scripts\rebuild_kb.py`

### 6.2 指标定义（源码 `app/evaluation/runner.py`）

| 指标 | 分母 | 分子 |
| --- | --- | --- |
| `keyword_accuracy` | 可答题中**声明了** `expected_keywords` 的 | `all(kw in answer)` 为真的比例 |
| `source_accuracy` | 可答题中**声明了** `expected_source_doc` 的 | 任一条 `sources[].document_name` 包含期望文档名的比例 |
| `refusal_accuracy` | **不可答**题 | 回答含拒答话术的比例 |
| `overall_accuracy` | **全部**题 | 单题 `correct` 的比例 |

- 可答题 `correct = keyword_hit 且 source_hit`（两者都声明时需同时成立）；
- 不可答题 `correct = refusal_hit`；
- 拒答话术表（任一命中即算拒答）：`无法回答` / `没有相关` / `未检索到` / `知识库中没有` / `知识库不包含` / `无法提供` / `不包含相关信息` / `不存在`；
- 接口返回逐题 `details`，但**落库只存汇总**（`evaluation_runs.metrics`）。

### 6.3 运行方式（三选一）

```powershell
# A. 前端：右栏「运行评测」按钮
# B. 接口：先登录拿 token，再调用（test_set_path 可省略）
curl -X POST http://127.0.0.1:8000/evaluation/run -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d "{}"

# C. 命令行（直接走真实容器，不经过 HTTP）
.venv\Scripts\python -c "from app.container import build_container; from app.evaluation.runner import run_evaluation; c=build_container(); m=run_evaluation(c.rag, top_k=3); print({k:v for k,v in m.items() if k!='details'})"
```

### 6.4 跑之前必须做的两件事

```powershell
# 1) 确认知识库是干净的标准语料（避免测试残留污染指标）
.venv\Scripts\python scripts\rebuild_kb.py --dry-run
.venv\Scripts\python scripts\rebuild_kb.py     # 需要时重建（自动备份到 data/backups/）

# 2) 确认测试集与语料一致（文件名必须对得上）
.venv\Scripts\python -m pytest tests\test_evaluation.py -q
```

### 6.5 结果判读

- **不预设目标值**。正确做法：先跑出**基线**，之后每次改动（换 chunk / 换 top_k / 加 rerank / 改 Prompt）与之对比；
- **真实 LLM 有非确定性**：同一测试集两次结果可能不同。要下结论就固定 `top_k`，同一配置**跑 2~3 次**再看是否稳定；
- **样本量已从 5 题扩到 47 题**（39 可答 + 8 拒答），并配了 4 个近邻干扰文档 → 指标首次具备可讨论性。
  但语料仍只有 6 个文档、每文档 1 个 chunk，**仍偏小**：结论宜表述为"在此语料与配置下的表现"，而非"系统普遍达到 X%"；
- **区分度提升点已到位**：检索必须在 6 个文档中认准出处（`差旅与报销指引`、`新员工入职指引` 在年假/住宿话题上是强干扰），
  所以 `source_accuracy` 现在能真正反映检索质量，而不是"只有一个文档必然命中"；
- 记录方式：每次跑完把 `total / keyword_accuracy / source_accuracy / refusal_accuracy / overall_accuracy` 与当时的配置一起记下来（`/evaluation/runs` 或 `evaluation_runs` 表已有历史）。

### 6.6 Rerank A/B（已实测，结论见 ADR-011）

评测指标在两组上都已**饱和为 100%**，因此 A/B 不能只看评测 —— 要用**有区分度的排序指标 + 延迟成本**：

```powershell
# 排序质量与延迟对比（不调用 LLM，零费用）
.venv\Scripts\python scripts\rerank_ab.py --candidates 5 --final 3 --json data/rerank_ab_prod.json

# 若还想确认"开启重排不会让评测掉分"（会调用 47 次 LLM）
$env:ENABLE_RERANK="true"; .venv\Scripts\python scripts\run_evaluation.py
```

实测结果（2026-09-14）：Noop 与 bge-reranker-base 的 **Recall@3 均为 100%**，
重排只把首命中从 92.3% 提到 94.9%（MRR +0.017），代价是检索延迟 **9.3ms → 1236ms（133×）**。
→ **维持 `ENABLE_RERANK=false`**；完整判据与适用边界见 `docs/decisions.md` ADR-011。

> 复测建议：重排的价值通常在**候选多、噪声大**时体现。语料扩到数百 chunk 后，
> "候选集里是否还包含正确文档"会重新成为瓶颈，届时用同一脚本复测即可。

## 7. 验收标准

对照 `docs/requirements.md` §6 的 6 条验收项，映射到自动化用例与手工步骤：

| # | 验收项 | 自动化用例 | 真实环境 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 上传文档 → 提问 → 带引用回答 | `test_rag_offline.py::test_answer_returns_citation_sources`、`test_api_ask.py::test_rag_mode_returns_sources`、`test_upload.py::test_upload_txt_creates_index` | `test_real_rag_answer_with_citation` | `sources` 非空且文档名正确 |
| 2 | 知识库无答案 → 拒答 | `test_evaluation.py::test_refusal_metric`、`test_non_refusal_on_unanswerable_fails` | `test_real_refusal_on_unknown_question` | 回答含拒答话术 |
| 3 | Router 三路分发正确 | `test_router.py::test_rule_based_routing`、`test_api_ask.py` 三个 mode 用例 | — | mode 值与预期一致 |
| 4 | Agent ReAct + 工具调用 | `test_agent.py`（前 7 个用例） | `test_real_agent_tool_calling` | 出现 Observation 且结果含 108 |
| 5 | 登录鉴权（无 token 401） | `test_api_auth.py`（15 个用例） | — | 401/200 符合预期 |
| 6 | 评测输出真实指标 | `test_evaluation.py`（19 个用例） | `test_real_evaluation_metrics` | 四项指标齐备且可复现 |

**发布前检查单**（全绿才算可交付）：

```powershell
.venv\Scripts\python -m compileall -q app tests scripts   # 语法
.venv\Scripts\python -m pytest                            # 148 passed, 4 skipped
.venv\Scripts\python scripts\rebuild_kb.py --dry-run      # 语料干净
$env:RUN_INTEGRATION=1; .venv\Scripts\python -m pytest tests\test_integration_real.py -v
<运行评测并记录基线指标>
```

## 8. 真实环境手工验收步骤

自动化覆盖不到的部分（前端交互、真实检索观感）按此走一遍：

1. `start.bat` 启动 → 打开 `http://127.0.0.1:8000/`；
2. 注册新用户 → 确认自动登录、顶栏显示用户名；
3. 右栏上传 `data/kb/员工考勤制度.txt` → 确认预览前 600 字、上传后出现在文档列表；
4. 提问「出差住宿标准是多少？」→ 确认回答含 **600 / 450**，下方「引用来源」可展开看到原文片段；
5. 提问「公司年会抽奖的一等奖奖品是什么？」→ 确认**明确拒答**，不编造；
6. 输入 `100 * 1.08` → 确认 mode 标签为 **agent** 且答案含 108；
7. 追问「那市内交通费呢？」→ 确认沿用同一会话（左栏标题不变、历史累积）；
8. 点「运行评测」→ 记录四项指标；
9. 测试边界：粘贴 3000 字长文提问 → 应 422；上传 >20MB 文件 → 应 413；
10. 删除会话与文档 → 刷新后确认消失。

## 9. 已知缺口与未覆盖

> V2 收口后重写：下表只保留**当前仍然存在**的缺口。V2 已经补掉的（MIN_SCORE 评估、语料扩容、
> 前端 E2E、Redis 限流、多租户/RBAC、Alembic 迁移）不再列在这里，见 §11 的验证记录。

| 缺口 | 影响 | 备注 |
| --- | --- | --- |
| **会话归属只到租户级** | 同租户内的用户互相可见对方的会话 | 本次范围外；要做需给 `conversations` 加 `user_id` 并在 DAO 加条件 |
| **邀请码并发未压测** | 一次性码靠带条件的 `UPDATE` 保证不超发，但没做真并发压测 | 语义有单测覆盖；压测待补 |
| **OCR 只验证了链路，未评估准确率** | 扫描件能解析、能入库，但识别质量没有指标 | 需要一份带标注的扫描件样本集 |
| **真实 LLM 流式的"首字"仍偏晚** | `deepseek-v4-flash` 是推理模型，`reasoning_content` 占了大部分时间；实测首字 ≈ 总耗时的 97% | 已修掉"读完才吐"的实现问题（§11）；要更早出字只能把思考过程也透出，属产品决策 |
| **Rerank 在 20 候选下才有收益，延迟不可接受** | 190 chunk 下候选 5 时 R@1 零增益，候选 20 才 +2.6pp 但要 6.2s/次 | 已实测，维持 `ENABLE_RERANK=false`（ADR-011） |
| **`chromadb` 有 5 个未修复漏洞** | 生产依赖集存在已知风险，上游无修复版本 | CI 已加 `pip-audit`（生产集仅提示）；详见 §11 |
| **并发能力有限** | 4 并发后吞吐见顶（~144 QPS），延迟线性上涨 | 已实测（§11）；瓶颈是 CPU 上的查询向量化，非 Chroma |
| **MCP server 未接真实客户端做长期联调** | 协议层有 47 条离线用例（含 stdio 往返），但没和 Claude Desktop / 其它宿主长期跑 | 传输层只覆盖到"进程内 stdio 往返" |
| **限流的多副本一致性未压测** | `RATE_LIMIT_BACKEND=redis` 已实现且有回退，但没做多进程共享计数的压测 | 单进程语义有 16 条用例覆盖 |

## 10. 维护规范

1. **优先写离线用例**：能用回退组件验证的一律不标 `integration`；
2. **需要真实 MySQL / 模型 / LLM 的用例**必须打 `@pytest.mark.integration` + `skipif RUN_INTEGRATION != 1`（`--strict-markers` 会拦住拼错的标记名）；
3. **改 `data/kb/` 语料必须同步 `data/evaluation/` 测试集**（文件名、关键词、`gold_evidence`），
   并重建索引（`scripts/rebuild_kb.py`）—— §5 里 8 条一致性用例会守住这条；
4. **新增配置项要配边界用例**（例如再引入上限类配置，就补"越界拒绝 + 边界值可用"两条）；
5. **修 bug 先加失败用例**：先在 `tests/` 里复现，再改实现，用例留在套件里当回归防线（本套件里标记为"回归"的用例即此来源）；
6. **不要用测试往真实知识库灌数据**：上传类用例一律走 `tmp_path`；
7. **requirements 系列文件保持纯 ASCII**：`pip-audit` 的解析器对无 BOM 文件按本地代码页（zh-CN 下是 cp936）解码，
   中文注释会直接抛 `UnicodeDecodeError` 导致依赖审计失败（见 `requirements.txt` 末尾注释）。

## 11. 附：验证记录与已修问题

### V2 收口记录（2026-09-14）

**全量回归**：`449 passed / 10 skipped / 0 failed`（24 个模块 / 459 用例；`-o addopts=""` 才看得到汇总行，原因见 §3）。

**真机验收（两个脚本，零假设）**

| 脚本 | 结果 |
| --- | --- |
| `scripts/verify_tenancy.py --part all` | 五部分 **81 项断言全通过**：临时库 `0001→head→base` 往返（18）、真实库 `stamp→upgrade` 且行数零丢失 + 幂等（20）、真实 Chroma 双租户隔离（9）、真实 HTTP 角色矩阵 + 跨租户检索隔离（18）、**邀请码准入（16，含"bootstrap 不能越权抢别的租户"）** |
| `scripts/verify_streaming.py` | 四阶段 **17 项断言全通过**（真实 DeepSeek + 真实 MySQL + 真实 bge + 真实 Chroma） |

**前端 E2E**：`RUN_E2E=1` → **3 passed**（真实 Edge）。

**存量库升级 + 索引重灌**：`stamp 0001_initial → upgrade 0002_multi_tenant`，
`information_schema` 校验列/索引/唯一键/外键齐全，行数不变
（users 4 / documents 6 / conversations 6 / messages 14 / evaluation_runs 6）；
V1 评测基线复跑 **47 题 × 2 次、四项指标 100%、极差 0.0pp**（迁移无回归）。

**Chroma 目录清理**：`scripts/cleanup_chroma.py` 删掉 **10 个孤立 HNSW 段目录（约 2.1 MB）**，
保留 1 个活跃段，删后 `count()` 复核仍为 **13**（证明删的是无人引用的目录）。

#### 真机流式抓出的三个 P0（全部已修 + 已加回归护栏）

V2 的流式曾经有 **24 条离线用例全绿**，但 `scripts/verify_streaming.py` 第一次跑就全线失败。
根因是**离线桩把无效用法固化成了"事实"** —— 这是"必须有真机验收"的最好例证。

| # | 问题 | 证据 | 修复 |
| --- | --- | --- | --- |
| P0-1 | `chat_stream` 用 `self.httpx.post(..., stream=True)`，而**顶层 `httpx.post()` 没有 `stream` 参数** → 真实流式 100% 失败（`TypeError`） | 真机首跑即 `LLMError: LLM 调用失败：post() got an unexpected keyword argument 'stream'` | 改用 `httpx.stream()` 上下文管理器；**测试桩同步改为模拟 `httpx.stream`**（原桩断言 `stream=True` 正是把 bug 锁死的元凶） |
| P0-2 | 先把整条流读完再一次性 `yield` → **首字延迟恒等于总耗时**，流式在体验上等于没有 | 实测 httpx 层首字 **15.531s** / 结束 **15.547s**（只差 16ms） | 改为收到一帧就 `yield` 一帧（仍保留"已产出后中断必须报错、绝不重试"的不变量）；新增回归用例 `test_chat_stream_yields_incrementally_instead_of_after_full_read` 断言"取到第一片时上游尚未读完" |
| P0-3 | `routes_stream._stream_body` 在**事件循环里同步迭代**同步生成器 → 生成期间整个服务被阻塞 | 修复后用并发探测验证：流式期间 `/healthz` 最慢 **0.016s**（判据 < 1s） | 用 `starlette.concurrency.iterate_in_threadpool` 迭代；验收脚本内置该探测，回归即失败 |

修复后的实测指标（真实 DeepSeek）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 客户端首字 / 总耗时 | 1.593s / 1.593s（**100%**） | 1.672s / 1.797s |
| HTTP 首字 / 总耗时 | 15.531s / 15.547s（99.9%） | 5.937s / 6.125s（96.9%） |
| 流式期间 `/healthz` 最慢 | （未探测；实现上会阻塞） | **0.016s** |
| 增量片段数 | 73（一次性到达） | 81（渐进到达） |

> **诚实说明**：HTTP 层首字仍占总耗时 96.9%，**不是实现问题了**，而是
> `deepseek-v4-flash` 是推理模型 —— `reasoning_content` 占了大部分时间，而应用只展示
> `content`。所以本次修复解决的是"吐得晚"（实现缺陷），没有也解决不了"想得久"（模型特性）。
> 要再往前一步只能把思考过程也透出（产品决策，当前不做）。

#### 邀请码准入（V2.1）发现并修掉的两个问题

| # | 问题 | 怎么发现的 | 修复 |
| --- | --- | --- | --- |
| 1 | **bootstrap 例外越权**：`require_invite=True` 时，任何人用 `BOOTSTRAP_ADMIN_USERNAME` + 自选 `tenant` 就能免码注册成那个租户的 admin；若目标租户已存在且无同名账号，等于跨租户越权读数据 | 邀请码功能的评审者实测复现：`{"username":"root","tenant":"victim-corp"}` → 200 / `role=admin` | bootstrap 例外收窄为 **`DEFAULT_TENANT` + 该租户尚无管理员**；真机验收里加了一条"必须 403"的断言，单测里加了两条回归 |
| 2 | **`0003_invites` 升级撞 `1050 Table 'invites' already exists`**：本项目的建表有**两个入口** —— 运行时 `db.init()`（执行 `SCHEMA`，全是 `CREATE TABLE IF NOT EXISTS`）与 Alembic 迁移。只要应用启动过一次，`invites` 就已经被建出来了 | 真机验收 `--part upgrade` 直接抛 `(1050, "Table 'invites' already exists")` | 0003 改为**幂等**：连库执行时先查 `information_schema`，已存在就跳过（与 0002 补外键同一套路）；离线 `--sql` 仍渲染完整 DDL，保住"迁移结果 == SCHEMA"的断言 |

> 顺带修掉验收脚本自身的两个缺陷：**硬编码版本号**（`0002_multi_tenant` → 改为从
> `ScriptDirectory` 取 head，否则每加一版迁移脚本就失效）与一处**写反的角色期望**
> （跨租户删除的方向搞反，脚本自己报错才发现）。验收脚本也是代码，也会错。

### 真实验证记录 · V1 基线（2026-09-14，现行语料与测试集）

语料 **6 文档 / 13 chunk**；测试集 **47 题**（可答 39 + 拒答 8）；`top_k=5`；
同一配置**连跑 2 次**（`python scripts/run_evaluation.py --repeat 2`）：

| 指标 | 第 1 次 | 第 2 次 | 极差 |
| --- | --- | --- | --- |
| `keyword_accuracy` | 100.0% | 100.0% | 0.0pp |
| `source_accuracy` | 100.0% | 100.0% | 0.0pp |
| `refusal_accuracy` | 100.0% | 100.0% | 0.0pp |
| `overall_accuracy` | 100.0% | 100.0% | 0.0pp |

- 39 道可答题：`keyword_hit=True` **且** `source_hit=True`（关键词全中、且引用了正确文档）；
- 8 道越界题：全部正确拒答；
- **`error_count = 0`**（无 LLM 调用失败）；
- 落库记录见 `evaluation_runs`（3 条 `total=47`，含 rerank 开启的那次）。

### Rerank A/B（同日，`scripts/rerank_ab.py`，候选 5 → 最终 3）

| 指标 | Noop（现状） | bge-reranker-base | 变化 |
| --- | --- | --- | --- |
| Recall@1 | 92.3% | 94.9% | +2.6pp |
| Recall@3（最终集合） | **100%** | **100%** | 0 |
| MRR | 0.9573 | 0.9744 | +0.0171 |
| 检索延迟（均值） | **9.3 ms** | **1236 ms** | **132.9×** |

→ 维持 `ENABLE_RERANK=false`。判据与适用边界见 **ADR-011**；原始数据 `data/rerank_ab_prod.json`。

### 检索性能（同日，`scripts/benchmark.py`，CPU 单进程）

| 档位 | 延迟 mean | p95 | 吞吐 |
| --- | --- | --- | --- |
| 串行（39 次） | 8.3 ms | 9.9 ms | 120.7 QPS |
| 并发 1 | 10.0 ms | 12.3 ms | 100.1 QPS |
| 并发 4 | 27.4 ms | 31.0 ms | **143.9 QPS** |
| 并发 8 | 56.2 ms | 76.2 ms | 138.0 QPS |

→ **4 并发后吞吐见顶、延迟线性上涨**；瓶颈是查询向量化（bge，CPU 密集），不是 Chroma。
端到端问答延迟另受 LLM 影响（单题实测约 1.7~7s，随答案长度波动）。

### 依赖漏洞扫描（`pip-audit`，2026-09-14）

| 范围 | 结果 |
| --- | --- |
| 测试/CI 依赖集 `requirements-ci.txt` | 无已知漏洞（CI 中为**阻断**任务）。注：本机经代理解析超时，该结论由已安装环境审计推出 |
| 生产依赖集 `requirements.txt` | **9 个已知漏洞 / 2 个包**：`chromadb 1.5.9` 5 个（**均无修复版本**，上游问题）、`setuptools` 3 个（已升级本机到 84.0.0）。CI 中为**提示**任务 |
| 另 | `torch 2.14.0+cpu` 因版本号不在 PyPI 而无法审计（跳过） |

### V1 历史记录（语料污染时期，已失效，仅留档）

| 项 | 结果 |
| --- | --- |
| 评测（5 题） | 关键词 100% · 来源 100% · 拒答 100% · 综合 100% |

> ⚠️ 这批数字产生时知识库被测试脚本上传的 `testing.md` / `decisions.md` 污染
> （47 chunk 中 39 个与业务无关），且只有 5 题、2 个文档，**不具代表性**。
> 已被上面的 47 题基线取代。

### 测试发现并修复的真实问题

1. **ReAct 解析**：真实 LLM 有时把答案直接写在 `Action: Final Answer` 后（无 `Action Input` 行）→ 重写 `_parse` 提取逻辑；
2. **LLM 空返回**：真实 API 偶发返回空内容 → LLM 客户端加空返回自动重试（最多 3 次）；
3. **测试与容器耦合**：`settings` import 时固化 + 脚本式测试上传后不清库 → 改为实例化读环境 + `FakeDatabase` + `tmp_path` 隔离；
4. **评测默认路径指向不存在的文件** → 不传 `test_set_path` 必 500 → 修正常量并加回归用例；
5. **陈旧测试脚本**：M2/M3 的脚本早于鉴权层，未带 token → 必挂 → 整套重构为 pytest 用例。
