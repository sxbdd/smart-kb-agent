"""Agent：ReAct 循环、工具调用、引用硬约束、解析容错（含历史真机 bug 回归）。"""
from __future__ import annotations

from app.services.agent import AgentEngine
from app.core.tools import build_tools


class ScriptedLLM:
    """按脚本返回响应，并记录每次调用收到的完整消息列表。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, max_tokens=None):
        self.calls.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return "Final Answer: （脚本耗尽）"


class CountingRag:
    """只统计检索次数，用来验证"纯计算题不检索"。"""

    def __init__(self, results=None):
        self.searches = 0
        self._results = results or []

    def search(self, query, top_k=None):
        self.searches += 1
        return self._results


def test_react_loop_calls_calculator_tool():
    llm = ScriptedLLM([
        'Thought: 需要算一下\nAction: calculator\nAction Input: {"expression": "100 * 1.08"}',
        "Final Answer: 结果是 108",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(CountingRag()), rag=None, max_iterations=5)

    answer, _ = engine.run("100 * 1.08")
    assert "108" in answer
    assert len(llm.calls) == 2
    # 第二轮消息里应带回工具执行结果作为 Observation
    joined = "\n".join(m["content"] for m in llm.calls[1])
    assert "Observation" in joined and "108" in joined


def test_calculation_question_skips_knowledge_search():
    rag = CountingRag()
    engine = AgentEngine(llm=ScriptedLLM(["Final Answer: 108"]), tools=build_tools(rag), rag=rag)

    engine.run("100 * 1.08")
    assert rag.searches == 0, "纯计算题不应触发知识库检索"


def test_knowledge_question_does_search_and_injects_grounding():
    rag = CountingRag()
    engine = AgentEngine(llm=ScriptedLLM(["Final Answer: 无法回答"]), tools=build_tools(rag), rag=rag)

    engine.run("年假有几天")
    assert rag.searches == 1
    systems = [m for m in engine.llm.calls[0] if m["role"] == "system"]
    assert any("知识库检索结果" in m["content"] for m in systems)


def test_agent_prompt_sent_once_and_first():
    """回归：agent 提示词只出现一次，且位于首位（原先每轮作为尾随 system 追加）。"""
    rag = CountingRag()
    llm = ScriptedLLM([
        'Thought: 查\nAction: knowledge_search\nAction Input: {"query": "年假"}',
        "Final Answer: 5 天",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(rag), rag=rag)
    engine.run("年假有几天")

    for call in llm.calls:
        systems = [m for m in call if m["role"] == "system"]
        assert len(systems) == 2, f"system 消息数应为 2（提示词+依据），实际 {len(systems)}"
        assert call[0]["role"] == "system"
        assert "能调用工具的知识库助手" in call[0]["content"]


def test_unknown_tool_does_not_crash():
    llm = ScriptedLLM([
        'Action: 不存在的工具\nAction Input: {"x": 1}',
        "Final Answer: 换个方式回答",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(CountingRag()), rag=None)
    answer, _ = engine.run("100+1")
    assert "换个方式回答" in answer
    assert "未知工具" in "\n".join(m["content"] for m in llm.calls[1])


def test_tool_exception_is_reported_as_observation():
    llm = ScriptedLLM([
        'Action: calculator\nAction Input: {"expression": "not a math expr"}',
        "Final Answer: 计算失败",
    ])
    engine = AgentEngine(llm=llm, tools=build_tools(CountingRag()), rag=None)
    engine.run("1+1")
    assert "工具执行失败" in "\n".join(m["content"] for m in llm.calls[1])


def test_max_iterations_returns_graceful_message():
    llm = ScriptedLLM(['Action: calculator\nAction Input: {"expression": "1+1"}'] * 10)
    engine = AgentEngine(llm=llm, tools=build_tools(CountingRag()), rag=None, max_iterations=2)
    answer, _ = engine.run("1+1")
    assert "超出了我的能力范围" in answer
    assert len(llm.calls) == 2


# ---------- _parse 回归：真机 LLM 的坏格式 ----------

def test_parse_final_answer_without_action_input():
    """历史真机 bug：模型把答案直接写在 `Action: Final Answer` 之后，没有 Action Input 行。"""
    response = "Thought: 已知\nAction: Final Answer\n入职满 1 年 5 天年假。"
    thought, action, action_input = AgentEngine._parse(response)
    assert action == "Final Answer"
    assert action_input == "入职满 1 年 5 天年假。"


def test_parse_final_answer_with_chinese_colon_and_multiline():
    response = "Final Answer：第一行\n第二行"
    _, action, action_input = AgentEngine._parse(response)
    assert action == "Final Answer"
    assert "第一行" in action_input and "第二行" in action_input


def test_parse_action_input_is_json_decoded():
    response = 'Thought: 查\nAction: knowledge_search\nAction Input: {"query": "年假"}'
    thought, action, action_input = AgentEngine._parse(response)
    assert thought == "查"
    assert action == "knowledge_search"
    assert action_input == {"query": "年假"}


def test_parse_non_json_action_input_stays_string():
    response = "Action: knowledge_search\nAction Input: 年假有多少天"
    _, action, action_input = AgentEngine._parse(response)
    assert action == "knowledge_search"
    assert action_input == "年假有多少天"


def test_parse_plain_answer_has_no_action():
    _, action, _ = AgentEngine._parse("这是一段没有格式的回答")
    assert action == ""
