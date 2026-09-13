"""普通对话：不走检索，直接 LLM 回答（支持多轮历史）。"""
from __future__ import annotations

from typing import List, Optional

from app.core.prompt_templates import build_chat_prompt


class ChatService:
    def __init__(self, llm) -> None:
        self.llm = llm

    def answer(self, question: str, history: Optional[List[dict]] = None) -> str:
        prompt = build_chat_prompt(question, history)
        return self.llm.chat([{"role": "user", "content": prompt}])