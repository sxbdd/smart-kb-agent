"""冒烟：应用可创建、健康检查、静态资源、路由齐备。"""
from __future__ import annotations


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_frontend_and_static_assets(client):
    assert client.get("/").status_code == 200
    assert client.get("/marked.min.js").status_code == 200
    # DOMPurify 必须可访问，否则 renderMarkdown 会降级为纯文本
    assert client.get("/purify.min.js").status_code == 200


def test_openapi_lists_all_business_routes(client):
    schema = client.get("/openapi.json").json()
    expected = {
        "/auth/register", "/auth/login",
        "/upload", "/documents", "/documents/{document_id}",
        "/ask",
        "/conversations", "/conversations/{conversation_id}",
        "/evaluation/run", "/evaluation/runs",
    }
    missing = expected - set(schema["paths"])
    assert not missing, f"缺少路由: {missing}"


def test_frontend_ships_both_libraries_in_html(client):
    html = client.get("/").text
    assert "/marked.min.js" in html
    assert "/purify.min.js" in html
    # 渲染必须经过 DOMPurify，不能再裸 innerHTML
    assert "DOMPurify.sanitize" in html
