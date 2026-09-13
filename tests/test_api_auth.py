"""认证与鉴权：注册/登录、401、限流。"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

PROTECTED = [
    ("get", "/documents"),
    ("get", "/conversations"),
    ("get", "/evaluation/runs"),
]


def test_register_returns_token(client):
    resp = client.post("/auth/register", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "alice"
    assert body["token"]


def test_duplicate_register_conflicts(client):
    client.post("/auth/register", json={"username": "bob", "password": "secret123"})
    resp = client.post("/auth/register", json={"username": "bob", "password": "secret123"})
    assert resp.status_code == 409


def test_login_success(client):
    client.post("/auth/register", json={"username": "carol", "password": "secret123"})
    resp = client.post("/auth/login", json={"username": "carol", "password": "secret123"})
    assert resp.status_code == 200
    assert resp.json()["token"]


def test_login_wrong_password_401(client):
    client.post("/auth/register", json={"username": "dave", "password": "secret123"})
    resp = client.post("/auth/login", json={"username": "dave", "password": "wrongpass"})
    assert resp.status_code == 401


def test_login_unknown_user_401(client):
    resp = client.post("/auth/login", json={"username": "nobody", "password": "secret123"})
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_routes_require_token(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, f"{path} 应要求鉴权"


def test_ask_requires_token(client):
    assert client.post("/ask", json={"question": "你好"}).status_code == 401


def test_evaluation_run_requires_token(client):
    assert client.post("/evaluation/run", json={}).status_code == 401


def test_garbage_token_rejected(client):
    resp = client.get("/documents", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_malformed_authorization_header_rejected(client):
    resp = client.get("/documents", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


def test_valid_token_grants_access(client, auth):
    assert client.get("/documents", headers=auth).status_code == 200


def test_register_validation_enforced(client):
    assert client.post("/auth/register", json={"username": "ab", "password": "secret123"}).status_code == 422
    assert client.post("/auth/register", json={"username": "abcdef", "password": "123"}).status_code == 422


def test_auth_rate_limit_returns_429(settings, fake_db):
    """PBKDF2 12 万次迭代，登录必须限流，否则可被放大成 CPU DoS。"""
    from app.factory import create_app

    limited = dataclasses.replace(settings, auth_rate_limit_per_minute=2)
    client = TestClient(create_app(limited))

    codes = [client.post("/auth/login", json={"username": "x", "password": "yyyyyy"}).status_code for _ in range(4)]
    assert codes[:2] == [401, 401], codes
    assert codes[2] == 429, codes
    assert codes[3] == 429, codes
