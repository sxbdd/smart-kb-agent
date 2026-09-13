"""Agent / 评测链路的租户隔离（V2）。

本文件**刻意不依赖 conftest 的 DB/容器 fixture**：租户绑定是纯函数行为，
用假 rag + 假 llm 就能验证，也就不会因为 Lead 正在并行改的 DAO/conftest 而变红。

核心思路 —— 用"签名"当护栏，而不是只断言传了什么：
- ``TenantStrictRag`` 把 ``tenant_id`` 声明为**必填关键字参数**，任何一处漏传都会直接
  TypeError。这样"漏传租户"是响亮的失败，而不是悄悄退化成全局检索（那样才会真正破防）。
- ``LegacyRag`` 的签名里**没有** ``tenant_id``（就是 V1 的形状）。``tenant_id=None`` 的
  路径必须还能跑通它，否则说明向后兼容被破坏了。
"""
from __future__ import annotations

import json

from app.core.tools import build_tools
from app.evaluation.runner import run_evaluation
from app.services.agent import AgentEngine

QUESTION = "年假有几天"


class ScriptedLLM:
    """按脚本返回响应，并记录每次收到的消息。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, max_tokens=None):
        self.calls.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return "Final Answer: （脚本耗尽）"


class TenantStrictRag:
    """租户**必填**的假 RAG：漏传即 TypeError。"""

    def __init__(self, results=None):
        self.search_tenants: list[str] = []
        self.answer_tenants: list[str] = []
        self._results = results or []

    def search(self, query, top_k=None, *, tenant_id):
        self.search_tenants.append(tenant_id)
        return self._results

    def answer(self, question, top_k=None, history=None, *, tenant_id):
        self.answer_tenants.append(tenant_id)
        return "回答", []


class LegacyRag:
    """V1 形状的假 RAG：接受不了 tenant_id 关键字。"""

    def __init__(self):
        self.search_calls: list[tuple] = []
        self.answer_calls: list[str] = []

    def search(self, query, top_k=None):
        self.search_calls.append((query, top_k))
        return []

    def answer(self, question, top_k=None, history=None):
        self.answer_calls.append(question)
        return "无法回答", []


def _mixed_tool_calls(tools: dict, rag) -> None:
    """把所有工具各调用一次，覆盖"将来新增检索类工具"的情况。"""
    assert "knowledge_search" in tools, "检索工具必须存在"
    tools["knowledge_search"].execute(query=QUESTION)
    tools["calculator"].execute(expression="1+1")


# ---------- build_tools：租户绑定在工具闭包里 ----------

def test_build_tools_binds_tenant_into_every_retrieval_tool():
    rag = TenantStrictRag()
    tools = {t.name: t for t in build_tools(rag, "t1")}
    assert set(tools) == {"knowledge_search", "calculator"}

    _mixed_tool_calls(tools, rag)

    # 只有检索类工具会打 rag；每一次都必须带着 t1
    assert rag.search_tenants == ["t1"], f"检索未带上租户：{rag.search_tenants}"


def test_build_tools_tenant_is_normalized_and_never_empty():
    """空串/纯空白的租户必须被归一成真实租户，不能变成"不过滤"。"""
    rag = TenantStrictRag()
    build_tools(rag, "  t1  ")  # 不影响后续断言，只为确认不抛异常
    tools = {t.name: t for t in build_tools(rag, "   ")}
    tools["knowledge_search"].execute(query=QUESTION)

    assert rag.search_tenants[-1] == "default", "空租户应归一为 default，而不是无过滤"


# ---------- AgentEngine.run：按请求绑定租户 ----------

def test_agent_run_with_tenant_filters_grounding_and_tool_calls():
    rag = TenantStrictRag()          # 引擎持有的真实 rag
    container_rag = LegacyRag()      # 容器级工具用的另一个 rag
    llm = ScriptedLLM([
        'Thought: 查\nAction: knowledge_search\nAction Input: {"query": "年假"}',
        "Final Answer: 5 天",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(container_rag), rag=rag)

    answer, _ = engine.run(QUESTION, tenant_id="t1")

    assert answer == "5 天"
    # 依据检索 + ReAct 里的工具检索，两次都要带 t1
    assert rag.search_tenants == ["t1", "t1"], f"Agent 链路漏了租户：{rag.search_tenants}"
    # run 必须切到租户绑定工具，绝不能再走容器里那份非租户工具
    assert container_rag.search_calls == [], "tenant_id 非 None 时不应再调用容器级工具"


def test_agent_run_without_tenant_keeps_v1_behaviour():
    """tenant_id 为 None：沿用注入工具、不新增租户关键字（LegacyRag 会因多参而 TypeError）。"""
    rag = LegacyRag()
    llm = ScriptedLLM([
        'Thought: 查\nAction: knowledge_search\nAction Input: {"query": "年假"}',
        "Final Answer: 5 天",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(rag), rag=rag)

    answer, _ = engine.run(QUESTION)

    assert answer == "5 天"
    assert rag.search_calls == [(QUESTION, 3), ("年假", 3)], f"调用形状变了：{rag.search_calls}"


def test_agent_run_tenant_is_normalized():
    rag = TenantStrictRag()
    engine = AgentEngine(llm=ScriptedLLM(["Final Answer: 无法回答"]), tools=build_tools(rag), rag=rag)

    engine.run(QUESTION, tenant_id="  t9  ")

    assert rag.search_tenants == ["t9"]


def test_agent_run_tenant_still_works_when_rag_missing():
    """rag 为 None 时没有可绑定的库，至少不能崩（容器始终传 rag，这是兜底路径）。"""
    llm = ScriptedLLM(["Final Answer: 108"])
    engine = AgentEngine(llm=llm, tools=build_tools(LegacyRag()), rag=None)

    answer, sources = engine.run("100 * 1.08", tenant_id="t1")

    assert "108" in answer and sources == []


# ---------- run_evaluation：逐题都带租户 ----------

def _write_set(tmp_path, ids):
    path = tmp_path / "tenant_set.json"
    path.write_text(
        json.dumps(
            [{"id": i, "question": f"问题 {i}", "answerable": False,
              "expected_keywords": [], "expected_source_doc": ""} for i in ids],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path)


def test_run_evaluation_with_tenant_filters_every_question(tmp_path):
    rag = TenantStrictRag()
    path = _write_set(tmp_path, ["q1", "q2"])

    metrics = run_evaluation(rag, path, tenant_id="t1")

    assert metrics["total"] == 2
    assert rag.answer_tenants == ["t1", "t1"], f"评测漏了租户：{rag.answer_tenants}"


def test_run_evaluation_tenant_is_normalized(tmp_path):
    rag = TenantStrictRag()
    path = _write_set(tmp_path, ["q1"])

    run_evaluation(rag, path, tenant_id=" t7 ")

    assert rag.answer_tenants == ["t7"]


def test_run_evaluation_without_tenant_keeps_v1_call_shape(tmp_path):
    rag = LegacyRag()
    path = _write_set(tmp_path, ["q1", "q2"])

    metrics = run_evaluation(rag, path)

    assert metrics["total"] == 2
    assert rag.answer_calls == ["问题 q1", "问题 q2"]


# ---------- 服务层入口与服务层/实现的一致性 ----------

def test_service_layer_alias_points_to_same_implementation():
    from app.evaluation.runner import run_evaluation as impl
    from app.services.evaluation import run_evaluation as service_entry

    assert service_entry is impl, "两个入口必须指向同一实现，避免出现两套指标口径"
