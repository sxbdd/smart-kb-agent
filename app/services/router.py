"""意图路由：规则优先 + LLM 分类（可选） + RAG 兜底。"""
from __future__ import annotations

import re
from typing import Optional

_CALC_PATTERN = re.compile(r"^[\d\s+\-*/%().^]+$")
_CHAT_PATTERNS = [
    re.compile(r"^(你好|您好|hi|hello|嗨|早上好|下午好|晚上好|谢谢|感谢|再见|拜拜)[!！。~～\s]*$", re.I),
]
_KB_HINTS = (
    "报销", "考勤", "请假", "年假", "制度", "规定", "流程", "标准",
    "工资", "公积金", "社保", "政策", "手册", "员工", "公司", "出差", "住宿",
)


class Router:
    """按规则分类用户意图；规则判不出时可选 LLM 分类，最终兜底 RAG。"""

    def __init__(self, llm=None, enable_llm: bool = False) -> None:
        self.llm = llm
        self.enable_llm = enable_llm

    def route(self, question: str) -> str:
        q = (question or "").strip()
        if not q:
            return "rag"

        # 规则优先：纯计算表达式 → agent
        if _CALC_PATTERN.match(q):
            return "agent"
        # 规则优先：明显闲聊 → chat
        if any(p.match(q) for p in _CHAT_PATTERNS):
            return "chat"
        # 规则优先：明显知识库问题 → rag
        if any(h in q for h in _KB_HINTS):
            return "rag"

        # 可选 LLM 分类
        if self.enable_llm and self.llm is not None:
            try:
                return self._llm_classify(q)
            except Exception:
                pass

        # 兜底：默认走 RAG（最稳、可追溯）
        return "rag"

    def _llm_classify(self, q: str) -> str:
        prompt = (
            "判断下面用户问题属于哪一类，只回答一个词：chat（闲聊）、rag（知识库问答）、agent（需要工具/多步）。\n"
            f"问题：{q}"
        )
        ans = self.llm.chat([{"role": "user", "content": prompt}], max_tokens=10).strip().lower()
        if ans in ("chat", "rag", "agent"):
            return ans
        return "rag"