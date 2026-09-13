"""问答接口：三路分发、引用来源、多轮、输入边界、会话管理。"""
from __future__ import annotations

import io

POLICY = (
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
)


def _upload_policy(client, auth, name: str = "员工考勤制度.txt"):
    resp = client.post(
        "/upload",
        files={"file": (name, io.BytesIO(POLICY.encode("utf-8")), "text/plain")},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 三路分发 ----------

def test_chat_mode(client, auth):
    resp = client.post("/ask", json={"question": "你好"}, headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "chat"
    assert body["sources"] == []
    assert body["conversation_id"]


def test_rag_mode_returns_sources(client, auth):
    _upload_policy(client, auth)
    resp = client.post("/ask", json={"question": "出差住宿标准是多少？", "top_k": 3}, headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "rag"
    assert body["sources"], "RAG 回答必须带引用来源"
    assert body["sources"][0]["document_name"] == "员工考勤制度.txt"
    assert body["sources"][0]["content"]


def test_agent_mode_for_calculation(client, auth):
    resp = client.post("/ask", json={"question": "100 * 1.08"}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "agent"


# ---------- 输入边界（历史问题：50 万字 / top_k=10^6 都会被接受） ----------

def test_oversized_question_rejected(client, auth, settings):
    payload = {"question": "x" * (settings.max_question_chars + 1)}
    assert client.post("/ask", json=payload, headers=auth).status_code == 422


def test_empty_question_rejected(client, auth):
    assert client.post("/ask", json={"question": ""}, headers=auth).status_code == 422


def test_top_k_out_of_range_rejected(client, auth, settings):
    too_big = {"question": "你好", "top_k": settings.max_top_k + 1}
    assert client.post("/ask", json=too_big, headers=auth).status_code == 422
    assert client.post("/ask", json={"question": "你好", "top_k": 0}, headers=auth).status_code == 422


def test_max_top_k_is_accepted(client, auth, settings):
    resp = client.post("/ask", json={"question": "你好", "top_k": settings.max_top_k}, headers=auth)
    assert resp.status_code == 200


# ---------- 多轮会话 ----------

def test_multi_turn_keeps_same_conversation(client, auth):
    _upload_policy(client, auth)
    first = client.post("/ask", json={"question": "年假有几天？"}, headers=auth).json()
    cid = first["conversation_id"]

    second = client.post("/ask", json={"question": "那出差住宿标准呢？", "conversation_id": cid}, headers=auth).json()
    assert second["conversation_id"] == cid

    history = client.get(f"/conversations/{cid}", headers=auth).json()
    roles = [m["role"] for m in history["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_assistant_message_persists_sources(client, auth):
    _upload_policy(client, auth)
    cid = client.post("/ask", json={"question": "出差住宿标准是多少？"}, headers=auth).json()["conversation_id"]
    history = client.get(f"/conversations/{cid}", headers=auth).json()
    assistant = [m for m in history["messages"] if m["role"] == "assistant"][0]
    assert assistant.get("sources"), "助手消息应落库引用来源"


def test_ask_with_unknown_conversation_404(client, auth):
    resp = client.post("/ask", json={"question": "你好", "conversation_id": "no-such-id"}, headers=auth)
    assert resp.status_code == 404


def test_conversation_list_shows_message_count(client, auth):
    client.post("/ask", json={"question": "你好"}, headers=auth)
    rows = client.get("/conversations", headers=auth).json()
    assert len(rows) == 1
    assert rows[0]["message_count"] == 2


def test_rename_conversation(client, auth):
    cid = client.post("/ask", json={"question": "你好"}, headers=auth).json()["conversation_id"]
    assert client.patch(f"/conversations/{cid}", json={"title": "新标题"}, headers=auth).status_code == 200
    assert client.get(f"/conversations/{cid}", headers=auth).json()["title"] == "新标题"


def test_delete_conversation_also_removes_messages(client, auth, fake_db):
    """回归：删除会话必须同时删消息，否则留下孤儿（历史实测残留 2 条）。"""
    cid = client.post("/ask", json={"question": "你好"}, headers=auth).json()["conversation_id"]
    assert fake_db.count_orphan_messages() == 0

    assert client.delete(f"/conversations/{cid}", headers=auth).status_code == 200
    assert client.get(f"/conversations/{cid}", headers=auth).status_code == 404
    assert fake_db.count_orphan_messages() == 0, "不允许出现孤儿消息"
    assert fake_db.messages == []


def test_delete_unknown_conversation_404(client, auth):
    assert client.delete("/conversations/nope", headers=auth).status_code == 404
