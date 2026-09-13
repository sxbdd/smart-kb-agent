"""Agent 引擎：ReAct 循环 + 知识库引用硬约束（支持多轮历史）。"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from app.core.prompt_templates import build_agent_prompt
from app.core.tools import build_tools
from app.models.schemas import Source
from app.services.router import is_calculation
from app.services.tenancy import normalize_tenant


class AgentEngine:
    def __init__(self, llm, tools: List, rag=None, max_iterations: int = 5) -> None:
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.rag = rag
        self.max_iterations = max_iterations

    def run(
        self,
        question: str,
        history: Optional[List[dict]] = None,
        tenant_id: Optional[str] = None,
    ) -> tuple[str, List[Source]]:
        """执行一次问答；``tenant_id`` 非 None 时，整条链路只检索该租户的库。

        租户绑定放在 run 里而不是 __init__：容器只在启动时建一次 AgentEngine，
        租户却是**每个请求**才知道的。所以这里按请求重建租户绑定的工具。
        为 None 时**一字不变**地沿用容器注入的 tools（V1 行为）。
        """
        tenant = None if tenant_id is None else normalize_tenant(tenant_id)
        tools = self.tools
        if tenant is not None and self.rag is not None:
            # 不做跨请求缓存：build_tools 很轻（只是建几个闭包），
            # 而缓存键一旦写错就是租户串号，代价远大于这点开销。
            tools = {t.name: t for t in build_tools(self.rag, tenant)}
        # rag 为 None 却要求租户过滤时无处可绑（引擎不知道注入工具用的是哪个 rag），
        # 只能退回注入工具；容器始终传 rag，生产路径不会走到这里。

        first_user = question
        if history:
            recent = history[-6:]
            history_str = "\n".join([f"{h['role']}: {h['content']}" for h in recent])
            first_user = f"对话历史：\n{history_str}\n\n当前问题：{question}"

        # Agent prompt 作为首条 system 消息只追加一次；此前每轮迭代都重新追加，
        # token 随轮次线性增长（见 docs/review-v1-audit.md §2.19）。
        messages: List[dict] = [{"role": "system", "content": self._agent_prompt()}]

        # 纯计算类问题不需要知识库依据，跳过检索：省一次向量查询，也避免把无关
        # 片段塞进上下文（此前无条件检索）。
        if is_calculation(question):
            grounding, sources = "", []
        else:
            grounding, sources = self._grounding(question, tenant)
            if grounding:
                messages.append({"role": "system", "content": grounding})

        messages.append({"role": "user", "content": first_user})

        for _ in range(self.max_iterations):
            response = self.llm.chat(messages)
            thought, action, action_input = self._parse(response)

            if not action or action == "Final Answer":
                text = action_input if (isinstance(action_input, str) and action_input.strip()) else response
                return (text if isinstance(text, str) else str(text)), sources

            if action in tools:
                if isinstance(action_input, dict):
                    try:
                        observation = tools[action].execute(**action_input)
                    except Exception as exc:
                        observation = f"工具执行失败: {exc}"
                else:
                    observation = f"工具 {action} 需要 JSON 格式参数"
            else:
                observation = f"未知工具: {action}，请使用可用工具或直接给出 Final Answer"

            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": f"Observation: {observation}"})

        return "抱歉，处理这个问题需要的步骤超出了我的能力范围。", sources

    def _grounding(self, question: str, tenant_id: Optional[str] = None) -> tuple[str, List[Source]]:
        if self.rag is None:
            return "（未配置知识库检索工具）", []
        # 依据检索必须和工具检索走同一个租户：否则 prompt 里的"依据"和工具返回的片段
        # 可能来自不同租户，既串数据又让模型自相矛盾。
        if tenant_id is None:
            results = self.rag.search(question, top_k=3)
        else:
            results = self.rag.search(question, top_k=3, tenant_id=tenant_id)
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