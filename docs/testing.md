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

`114 个测试函数 → 144 个用例`（参数化展开后）：离线 **140 passed** + 真实环境 **4 skipped**。

| 层 | 文件 | 用例数 | 依赖 | 目标 | 反馈速度 |
| --- | --- | --- | --- | --- | --- |
| L1 冒烟 | `test_smoke.py` | 4 | 无 | 应用可创建、健康检查、静态资源、路由齐备 | <1s |
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
.venv\Scripts\python -m pytest                 # 140 passed, 4 skipped

# ---- 只装离线测试所需的最小依赖（不需要 torch / chromadb，约 20MB）----
.venv\Scripts\python -m pip install -r requirements-ci.txt

# ---- 按层/按主题选择 ----
.venv\Scripts\python -m pytest tests\test_router.py -v          # 单文件
.venv\Scripts\python -m pytest -k "evaluation or refusal"       # 关键字
.venv\Scripts\python -m pytest -m "not integration"             # 显式排除真实环境
.venv\Scripts\python -m pytest --collect-only -q                # 只看用例数

# ---- 真实环境端到端（默认跳过；会真实调用 LLM 并写入 MySQL）----
$env:RUN_INTEGRATION=1
.venv\Scripts\python -m pytest tests\test_integration_real.py -v -s
$env:RUN_INTEGRATION=$null
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

### L1 单元 `test_parser_splitter.py`（13）
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

### L3 接口 `test_upload.py`（9）
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

### L3 接口 `test_evaluation.py`（11）
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

### 6.6 Rerank A/B 复测（ADR-011 待更新）

ADR-011 的结论（"Noop 与 bge-reranker-base 持平 → V1 不启用"）建立在"**单文档单 chunk，重排无发挥空间**"的前提上。当前语料已变化，前提需要重新验证：

```powershell
# A 组
$env:ENABLE_RERANK="false"; <重启服务>; <运行评测>;  记录四项指标
# B 组
$env:ENABLE_RERANK="true";  <重启服务>; <运行评测>;  记录四项指标
```

**语料已扩到 6 个文档（含 2 个近邻干扰），Rerank 现在有 6 个候选可比** —— 比原先"单文档单 chunk 无发挥空间"有意义得多，
但每文档仍只有 1 个 chunk，**建议把语料写到千字级（拆成多 chunk）后再下结论**。做完后按 ADR 格式更新 `docs/decisions.md`。

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
.venv\Scripts\python -m pytest                            # 140 passed, 4 skipped
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

| 缺口 | 影响 | 备注 |
| --- | --- | --- |
| **检索无相似度阈值** | 全部片段不相关时仍返回 top-k，拒答完全依赖 Prompt 守规矩 | 已用 `test_knowledge_search_has_no_score_threshold` 固化行为；是否引入 `MIN_SCORE` 待评估 |
| **语料仍偏小** | 6 个文档、每文档 1 chunk；指标只能说明"在此语料下的表现" | 测试集已扩到 47 题并加干扰文档；再加语料建议写到千字级以拆出多 chunk |
| **真实 LLM 非确定性** | 评测结果有波动，无多次重跑与置信区间 | 同一配置跑 2~3 次再判读 |
| **无前端 E2E** | 只有静态断言（页面含某字符串），无浏览器级交互测试 | 未引入 Playwright；当前靠 §8 手工步骤 |
| **无性能/并发测试** | 不知道大 `top_k`、多并发下的延迟与内存表现 | 无压测脚本 |
| **无依赖漏洞扫描** | 第三方库风险未跟踪 | 可加 `pip-audit` 到 CI |
| **限流是进程内实现** | 多副本部署时各副本独立计数，限流失效 | 需换 Redis 等共享存储 |
| **单租户** | 无权限隔离，所有用户共享同一知识库 | ADR-009 有意为之 |
| **无 DB 迁移工具** | 建表用 `CREATE TABLE IF NOT EXISTS`，加字段需手工 | 可引入 Alembic |

## 10. 维护规范

1. **优先写离线用例**：能用回退组件验证的一律不标 `integration`；
2. **需要真实 MySQL / 模型 / LLM 的用例**必须打 `@pytest.mark.integration` + `skipif RUN_INTEGRATION != 1`（`--strict-markers` 会拦住拼错的标记名）；
3. **改 `data/kb/` 语料必须同步 `data/evaluation/` 测试集**（文件名、关键词、`gold_evidence`），
   并重建索引（`scripts/rebuild_kb.py`）—— §5 里 8 条一致性用例会守住这条；
4. **新增配置项要配边界用例**（例如再引入上限类配置，就补"越界拒绝 + 边界值可用"两条）；
5. **修 bug 先加失败用例**：先在 `tests/` 里复现，再改实现，用例留在套件里当回归防线（本套件里标记为"回归"的用例即此来源）；
6. **不要用测试往真实知识库灌数据**：上传类用例一律走 `tmp_path`。

## 11. 附：历史验证记录与已修问题

### 真实验证记录（V1 历史）

| 项 | 结果 |
| --- | --- |
| RAG 问答 | 出差住宿标准 → 带引用正确回答 |
| 拒答 | 知识库无答案 → "根据当前知识库，我无法回答这个问题" |
| Router | chat / rag / agent 三路分类正确 |
| Agent 计算器 | 100 * 1.08 = 108 |
| 鉴权 | 无 token 401 / 带 token 200 |
| 评测（5 题） | 关键词 100% · 来源 100% · 拒答 100% · 综合 100%（3 条历史记录见 `evaluation_runs`） |

> ⚠️ 上述评测数字产生时，知识库被测试脚本上传的 `testing.md` / `decisions.md` 污染
> （47 个 chunk 里 39 个与业务无关）。语料已于 2026-09-14 重建为干净标准语料
> （`data/kb/`，共 2 个 chunk），**旧指标已失效，需按 §6 重新跑出基线**。

### 测试发现并修复的真实问题

1. **ReAct 解析**：真实 LLM 有时把答案直接写在 `Action: Final Answer` 后（无 `Action Input` 行）→ 重写 `_parse` 提取逻辑；
2. **LLM 空返回**：真实 API 偶发返回空内容 → LLM 客户端加空返回自动重试（最多 3 次）；
3. **测试与容器耦合**：`settings` import 时固化 + 脚本式测试上传后不清库 → 改为实例化读环境 + `FakeDatabase` + `tmp_path` 隔离；
4. **评测默认路径指向不存在的文件** → 不传 `test_set_path` 必 500 → 修正常量并加回归用例；
5. **陈旧测试脚本**：M2/M3 的脚本早于鉴权层，未带 token → 必挂 → 整套重构为 pytest 用例。
