"""Agent 引擎：ReAct 循环 + 知识库引用硬约束（支持多轮历史）。"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from app.core.prompt_templates import build_agent_prompt
from app.models.schemas import Source


class AgentEngine:
    def __init__(self, llm, tools: List, rag=None, max_iterations: int = 5) -> None:
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.rag = rag
        self.max_iterations = max_iterations

    def run(self, question: str, history: Optional[List[dict]] = None) -> tuple[str, List[Source]]:
        first_user = question
        if history:
            recent = history[-6:]
            history_str = "\n".join([f"{h['role']}: {h['content']}" for h in recent])
            first_user = f"对话历史：\n{history_str}\n\n当前问题：{question}"

        messages = [{"role": "user", "content": first_user}]
        grounding, sources = self._grounding(question)
        messages.append({"role": "system", "content": grounding})

        for _ in range(self.max_iterations):
            response = self.llm.chat(messages + [{"role": "system", "content": self._agent_prompt()}])
            thought, action, action_input = self._parse(response)

            if not action or action == "Final Answer":
                text = action_input if (isinstance(action_input, str) and action_input.strip()) else response
                return (text if isinstance(text, str) else str(text)), sources

            if action in self.tools:
                if isinstance(action_input, dict):
                    try:
                        observation = self.tools[action].execute(**action_input)
                    except Exception as exc:
                        observation = f"工具执行失败: {exc}"
                else:
                    observation = f"工具 {action} 需要 JSON 格式参数"
            else:
                observation = f"未知工具: {action}，请使用可用工具或直接给出 Final Answer"

            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": f"Observation: {observation}"})

        return "抱歉，处理这个问题需要的步骤超出了我的能力范围。", sources

    def _grounding(self, question: str) -> tuple[str, List[Source]]:
        if self.rag is None:
            return "（未配置知识库检索工具）", []
        results = self.rag.search(question, top_k=3)
        if not results:
            return "知识库检索结果：未检索到相关内容。", []
        kb = "\n".join([f"- {r.metadata.get('document_name', '')}: {r.document[:200]}" for r in results])
        sources = [
            Source(
                document_id=r.metadata.get("document_id", ""),
                document_name=r.metadata.get("document_name", ""),
                chunk_id=r.id,
                content=r.document,
                score=round(r.score, 4),
            )
            for r in results
        ]
        grounding = (
            "知识库检索结果如下：\n" + kb + "\n\n"
            "回答规则：当问题涉及知识/事实时，必须以上述检索结果为准；"
            "若检索结果与问题无关或没有相关内容，请明确说明“根据当前知识库，我无法回答这个问题”，不要使用你自己的知识。"
        )
        return grounding, sources

    def _agent_prompt(self) -> str:
        tool_desc = "\n".join([f"- {name}: {t.description}" for name, t in self.tools.items()])
        return build_agent_prompt(tool_desc)

    @staticmethod
    def _parse(response: str) -> tuple[str, str, str | dict]:
        fa = re.search(r"Final Answer[:：]?\s*(.+)", response, re.IGNORECASE | re.DOTALL)
        if fa:
            final = fa.group(1).strip()
            final = re.sub(r"^Action\s+Input[:：]?\s*", "", final).strip()
            return "", "Final Answer", final

        thought = ""
        action = ""
        action_input: str | dict = ""
        m = re.search(r"Thought:\s*(.+)", response, re.IGNORECASE)
        if m:
            thought = m.group(1).strip()
        m = re.search(r"Action:\s*(.+)", response, re.IGNORECASE)
        if m:
            action = m.group(1).strip()
        m = re.search(r"Action Input:\s*(.+)", response, re.IGNORECASE | re.DOTALL)
        if m:
            action_input = m.group(1).strip()

        if action and action != "Final Answer" and isinstance(action_input, str):
            try:
                action_input = json.loads(action_input)
            except json.JSONDecodeError:
                pass
        return thought, action, action_input