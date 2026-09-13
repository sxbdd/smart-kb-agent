"""认证服务：注册 / 登录（JWT + PBKDF2 密码哈希）。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.utils.exceptions import AppError

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


class AuthService:
    def __init__(self, db, jwt_secret: str, jwt_expire_minutes: int) -> None:
        self.db = db
        self.jwt_secret = jwt_secret
        self.jwt_expire_minutes = jwt_expire_minutes

    def register(self, username: str, password: str) -> dict:
        if self.db.get_user_by_username(username) is not None:
            raise AppError("用户名已存在", 409)
        user_id = self.db.create_user(username, hash_password(password))
        return {"token": self._issue_token(user_id, username), "username": username}

    def login(self, username: str, password: str) -> dict:
        user = self.db.get_user_by_username(username)
        if user is None or not verify_password(password, user["password_hash"]):
            raise AppError("用户名或密码错误", 401)
        return {"token": self._issue_token(user["id"], username), "username": username}

    def _issue_token(self, user_id: int, username: str) -> str:
        payload = {
            "sub": str(user_id),
            "username": username,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=self.jwt_expire_minutes),
        }
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")

    def decode_token(self, token: str) -> dict:
        return jwt.decode(token, self.jwt_secret, algorithms=["HS256"])