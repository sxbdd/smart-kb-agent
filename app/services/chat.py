"""普通对话：不走检索，直接 LLM 回答。"""
from __future__ import annotations

from app.core.prompt_templates import build_chat_prompt


class ChatService:
    def __init__(self, llm) -> None:
        self.llm = llm

    def answer(self, question: str) -> str:
        prompt = build_chat_prompt(question)
        return self.llm.chat([{"role": "user", "content": prompt}])