"""对话调度：Router 分发到 chat / rag / agent，并持久化历史。

V2 租户维度（见 docs/v2-plan.md §6.3）：**服务层不自己判断归属**，而是把
`tenant_id` 一路透传给 DAO —— 会话说到底属于哪个租户由 SQL 的 `tenant_id = %s`
条件决定，"先查再判断"的写法最容易漏，这里一律不用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.schemas import AskResponse
from app.utils.exceptions import NotFoundError

#: 与 tenancy.normalize_tenant 的默认值保持一致；放在这里只为给新参数一个明确的默认值
DEFAULT_TENANT = "default"


class ConversationService:
    def __init__(self, db, router, chat, rag, agent=None) -> None:
        self.db = db
        self.router = router
        self.chat = chat
        self.rag = rag
        self.agent = agent

    def get_history(self, conversation_id: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
        """取会话消息；`tenant_id` 交给 DAO 过滤，别的租户的会话表现为"不存在"。"""
        conv = self.db.get_conversation(conversation_id, tenant_id)
        return conv["messages"] if conv else []

    def list_conversations(self, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
        return self.db.list_conversations(tenant_id)

    def _ensure_conversation(self, question: str, conversation_id: Optional[str],
                             tenant_id: str = DEFAULT_TENANT) -> str:
        """没有会话就建一个（标题取问题前 24 字），有就校验它属于本租户。

        新增的 `tenant_id` 是**带默认值的末尾参数**：`app/api/routes_stream.py`
        用 `run_in_threadpool(conversation._ensure_conversation, question, conversation_id)`
        直接按位置调用，前两个参数的位置一旦改动就会静默传错 —— 所以只允许往后加。
        """
        if not conversation_id:
            title = question.strip().replace("\n", " ")[:24]
            return self.db.create_conversation(title, tenant_id)
        if self.db.get_conversation(conversation_id, tenant_id) is None:
            raise NotFoundError("对话不存在")
        return conversation_id

    def ask(self, question: str, conversation_id: Optional[str] = None, top_k: Optional[int] = None,
            tenant_id: str = DEFAULT_TENANT) -> AskResponse:
        """一次问答：建/校验会话 → 取历史 → 分发 → 落两条消息。

        三条链路都带 `tenant_id`：
        - `rag.answer(..., tenant_id=...)` → 向量检索只在本租户的 chunk 里召回；
        - `agent.run(..., tenant_id=...)` → 由 AgentEngine 继续往下透传；
        - 会话读写走 DAO → 同租户内可见、跨租户表现为不存在。

        `chat` 分支不检索，天然与租户无关；但它的历史同样来自本租户的会话。
        """
        conversation_id = self._ensure_conversation(question, conversation_id, tenant_id)
        history = self.get_history(conversation_id, tenant_id)

        mode = self.router.route(question)
        if mode == "chat":
            answer = self.chat.answer(question, history=history)
            sources = []
        elif mode == "agent" and self.agent is not None:
            answer, sources = self.agent.run(question, history, tenant_id=tenant_id)
        else:
            answer, sources = self.rag.answer(question, top_k=top_k, history=history, tenant_id=tenant_id)

        # 消息本身不带 tenant_id：归属由会话决定（见 v2-plan §9.1 的 add_message "不变"）
        self.db.add_message(conversation_id, "user", question)
        self.db.add_message(conversation_id, "assistant", answer, [s.model_dump() for s in sources])

        return AskResponse(
            answer=answer,
            sources=sources,
            conversation_id=conversation_id,
            mode=mode,
            timestamp=datetime.now(timezone.utc),
        )
