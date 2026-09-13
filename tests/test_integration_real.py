"""真实环境端到端验证（默认跳过）。

需要同时具备：MySQL 8、真实 Chroma、本地 bge 模型、可用的 LLM API（.env 已配置）。
运行方式：

    $env:RUN_INTEGRATION=1
    .venv\\Scripts\\python -m pytest tests\\test_integration_real.py -v -s

注意：这些用例会真实调用 LLM API（产生费用）并写入 MySQL，因此不放进默认 CI。
"""
from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="需要 RUN_INTEGRATION=1"),
]

POLICY = (
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00，午休 12:00-13:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
    "4. 市内交通费：凭票实报实销，单日上限 100 元。\n"
)


@pytest.fixture(scope="module")
def real_client():
    """用仓库真实 .env 构造应用（真实 MySQL / Chroma / bge / DeepSeek）。"""
    from app.config import Settings
    from app.factory import create_app

    return TestClient(create_app(Settings()))


@pytest.fixture(scope="module")
def real_auth(real_client):
    import uuid

    username = f"itest_{uuid.uuid4().hex[:8]}"
    resp = real_client.post("/auth/register", json={"username": username, "password": "secret123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(scope="module")
def uploaded(real_client, real_auth):
    resp = real_client.post(
        "/upload",
        files={"file": ("员工考勤制度.txt", io.BytesIO(POLICY.encode("utf-8")), "text/plain")},
        headers=real_auth,
    )
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["document_id"]
    yield doc_id
    real_client.delete(f"/documents/{doc_id}", headers=real_auth)


def test_real_rag_answer_with_citation(real_client, real_auth, uploaded):
    resp = real_client.post(
        "/ask", json={"question": "出差住宿标准是多少？", "top_k": 3}, headers=real_auth
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"].strip()
    assert body["sources"], "命中题必须带引用来源"
    assert any("600" in s["content"] or "450" in s["content"] for s in body["sources"])
    assert any(s["document_name"] == "员工考勤制度.txt" for s in body["sources"])


def test_real_refusal_on_unknown_question(real_client, real_auth, uploaded):
    resp = real_client.post(
        "/ask", json={"question": "公司年会抽奖的一等奖奖品是什么？", "top_k": 3}, headers=real_auth
    )
    assert resp.status_code == 200, resp.text
    answer = resp.json()["answer"]
    assert any(p in answer for p in ("无法回答", "没有相关", "未检索到", "知识库中没有", "不包含")), answer


def test_real_agent_tool_calling(real_client, real_auth):
    resp = real_client.post("/ask", json={"question": "100 * 1.08"}, headers=real_auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "agent"
    assert "108" in body["answer"], body["answer"]


def test_real_evaluation_metrics(real_client, real_auth, uploaded):
    resp = real_client.post("/evaluation/run", json={}, headers=real_auth)
    assert resp.status_code == 200, resp.text
    metrics = resp.json()
    assert metrics["total"] >= 5
    for key in ("keyword_accuracy", "source_accuracy", "refusal_accuracy", "overall_accuracy"):
        assert key in metrics
