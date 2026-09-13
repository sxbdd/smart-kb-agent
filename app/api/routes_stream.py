"""流式问答接口：`POST /ask/stream`（SSE）。

事件序列固定（每条事件 = `event: <name>\\ndata: <json>\\n\\n`）：

| 事件 | data | 时机 |
| --- | --- | --- |
| `meta` | `{"mode": "rag"/"chat"/"agent", "conversation_id": "..."}` | 分发完成后立刻 |
| `delta` | `{"text": "..."}` | 每个增量片段 |
| `sources` | `{"sources": [...]}` | 生成结束后 |
| `done` | `{}` | 收尾 |
| `error` | `{"detail": "..."}` | 任意阶段异常（**不断流**，先发 error 再正常结束） |

设计要点：

- 鉴权与非流式 `/ask` 完全一致（`Depends(get_current_user)`）。
- 生成结束后像 `/ask` 一样落库 user / assistant 两条消息（assistant 带 sources）。
- 所有同步服务调用（DB、向量检索、Router 分发）都放进线程池，避免阻塞事件循环
  —— 流式接口被阻塞时，SSE 的心跳与其它请求都会一起卡住。
- `settings.enable_stream == False` 时返回 **404**（接口视作不存在，前端据此回退 `/ask`）。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Iterator, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.prompt_templates import build_chat_prompt
from app.models.schemas import AskRequest, Source
from app.services.tenancy import Principal
from app.utils.exceptions import NotFoundError

logger = logging.getLogger("stream")

router = APIRouter(tags=["流式问答"])

#: SSE 响应头（额外加 X-Accel-Buffering，避免 Nginx 反代把流缓冲成一次性响应）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_event(name: str, data: dict) -> str:
    """组装一条 SSE 事件。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _ensure_conversation(conversation, question: str, conversation_id: Optional[str],
                               tenant_id: str = "default") -> str:
    """复用非流式的会话创建 / 校验（ConversationService 里的同一套规则）。

    `tenant_id` 必须一路带到 DAO：否则 `/ask/stream` 会成为绕过租户隔离的后门
    （能读到别人租户的会话历史）。
    """
    return await run_in_threadpool(
        conversation._ensure_conversation, question, conversation_id, tenant_id
    )


@router.post(
    "/ask/stream",
    summary="知识库问答（流式 SSE）",
    description=(
        "以 Server-Sent Events 逐步返回回答：meta → delta* → sources → done；"
        "任意阶段出错则发 error 事件后正常结束（不会直接断流）。"
    ),
    response_class=StreamingResponse,
)
async def ask_stream(
    request: Request,
    body: AskRequest,
    principal: Principal = Depends(get_current_user),
) -> StreamingResponse:
    container = request.app.state.container
    cfg = container.settings
    if not cfg.enable_stream:
        # 与"能力未开启"保持一致：接口不存在（前端 fallback 回非流式 /ask）
        raise NotFoundError("流式输出未开启（ENABLE_STREAM=false）")
    return StreamingResponse(
        _stream_body(container, body, principal.tenant_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/config",
    summary="前端运行时配置",
    description="给前端读取能力开关（目前只有流式输出），避免把配置硬编码进 HTML。",
)
async def runtime_config(
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> dict:
    return {"enable_stream": bool(request.app.state.container.settings.enable_stream)}


async def _stream_body(container, body: AskRequest, tenant_id: str = "default") -> AsyncIterator[str]:
    """`/ask/stream` 的响应体生成器：负责分发、流式产出、落库、异常上报。

    落库只有一处（`_persist`），且用 `saved` / `errored` 两个标志保证
    **每个问题最多落库一次**：既不重复落库，也不会因客户端中途断开而丢掉半截回答。
    """
    conversation = container.conversation
    question = body.question
    answer_parts: list[str] = []
    sources: list[Source] = []
    conversation_id: Optional[str] = None
    settled = False   # 是否已正常收尾（sources + done 已发出）
    saved = False     # 本次问答是否已落库
    errored = False   # 是否已经发过 error 事件
    mode = "rag"

    async def _persist() -> None:
        nonlocal saved
        if saved or conversation_id is None:
            return
        saved = True
        await run_in_threadpool(conversation.db.add_message, conversation_id, "user", question)
        await run_in_threadpool(
            conversation.db.add_message, conversation_id, "assistant",
            "".join(answer_parts), [s.model_dump() for s in sources],
        )

    try:
        try:
            conversation_id = await _ensure_conversation(conversation, question, body.conversation_id, tenant_id)
            history = await run_in_threadpool(conversation.get_history, conversation_id, tenant_id)
            mode = await run_in_threadpool(container.router.route, question)

            # 1) meta：分发完成后立刻下发
            yield sse_event("meta", {"mode": mode, "conversation_id": conversation_id})

            deltas, sources = await _build_stream(container, mode, question, body, history, tenant_id)

            # 2) delta：逐个增量
            for piece in deltas:
                if not piece:
                    continue
                answer_parts.append(piece)
                yield sse_event("delta", {"text": piece})

            # 3) sources：生成结束后
            yield sse_event("sources", {"sources": [s.model_dump() for s in sources]})

            # 4) 落库（与非流式一致：user + assistant，assistant 带 sources）
            await _persist()

            # 5) done
            settled = True
            yield sse_event("done", {})
        except Exception as exc:  # noqa: BLE001 —— 任何阶段异常都必须转成 error 事件，不能断流
            errored = True
            logger.exception("流式问答失败：%s", exc)
            detail = getattr(exc, "detail", None) or str(exc) or "服务器内部错误"
            yield sse_event("error", {"detail": str(detail)})
    finally:
        # 客户端提前断开（GeneratorExit）时也要保证已产出的内容落库；此处不再 yield
        if not settled and not errored:
            try:
                await _persist()
            except Exception:  # noqa: BLE001 —— 兜底落库失败不能影响响应收尾
                logger.exception("流式问答兜底落库失败：%s", question[:50])


async def _build_stream(container, mode: str, question: str, body: AskRequest,
                        history: list[dict], tenant_id: str = "default") -> tuple[Iterator[str], list[Source]]:
    """按 Router 分发结果组装"增量来源"。

    - `rag`：`RAGService.stream_answer()` —— 真实逐字流式，附带检索来源；
    - `chat`：普通对话流式（无来源）；
    - `agent`：Agent 目前是多步推理，不逐字产出，整段作为**单个** delta 下发（来源照常）。

    三条分支都必须把 `tenant_id` 带到底层检索，否则流式接口会跨租户召回。
    """
    if mode == "chat":
        # ChatService 只有非流式 answer()，这里用同一套 Prompt 模板本地组装，行为完全对齐
        # （不动 app/services/chat.py，避免影响其它角色的文件）
        deltas = await run_in_threadpool(
            lambda: container.llm.chat_stream([{"role": "user", "content": build_chat_prompt(
                question, history, container.settings.max_history_messages)}])
        )
        return deltas, []

    if mode == "agent" and container.agent is not None:
        answer, sources = await run_in_threadpool(
            container.agent.run, question, history, tenant_id=tenant_id
        )
        return iter([answer]), list(sources)

    sources, deltas = await run_in_threadpool(
        container.rag.stream_answer, question, body.top_k, history, tenant_id=tenant_id
    )
    return deltas, list(sources)
