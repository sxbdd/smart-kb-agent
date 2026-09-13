"""对话调度：Router 分发到 chat / rag / agent，并持久化历史。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.schemas import AskResponse
from app.utils.exceptions import NotFoundError


class ConversationService:
    def __init__(self, db, router, chat, rag, agent=None) -> None:
        self.db = db
        self.router = router
        self.chat = chat
        self.rag = rag
        self.agent = agent

    def get_history(self, conversation_id: str) -> list[dict]:
        conv = self.db.get_conversation(conversation_id)
        return conv["messages"] if conv else []

    def list_conversations(self) -> list[dict]:
        return self.db.list_conversations()

    def _ensure_conversation(self, question: str, conversation_id: Optional[str]) -> str:
        if not conversation_id:
            title = question.strip().replace("\n", " ")[:24]
            return self.db.create_conversation(title)
        if self.db.get_conversation(conversation_id) is None:
            raise NotFoundError("对话不存在")
        return conversation_id

    def ask(self, question: str, conversation_id: Optional[str] = None, top_k: Optional[int] = None) -> AskResponse:
        conversation_id = self._ensure_conversation(question, conversation_id)
        history = self.get_history(conversation_id)

        mode = self.router.route(question)
        if mode == "chat":
            answer = self.chat.answer(question, history=history)
            sources = []
        elif mode == "agent" and self.agent is not None:
            answer, sources = self.agent.run(question, history=history)
        else:
            answer, sources = self.rag.answer(question, top_k=top_k, history=history)

        self.db.add_message(conversation_id, "user", question)
        self.db.add_message(conversation_id, "assistant", answer, [s.model_dump() for s in sources])

        return AskResponse(
            answer=answer,
            sources=sources,
            conversation_id=conversation_id,
            mode=mode,
            timestamp=datetime.now(timezone.utc),
        )