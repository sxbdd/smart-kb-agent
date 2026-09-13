"""多租户与角色权限的基础类型（V2）。

为什么单独成模块：租户与角色是**横切关注点**，会被路由层、服务层、DAO 层反复使用。
把"值对象 + 判定规则"集中在这里，避免各处自己写字符串比较导致规则不一致。

职责边界：
- 本模块只做**类型与判定**，不碰数据库；
- 真正的隔离强制点在 DAO（查询条件必须带 ``tenant_id``）与向量库（chunk metadata 过滤），
  见 ``docs/v2-plan.md`` §6.3 与 ``app/models/database.py``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.utils.exceptions import AppError

#: 三个角色，权限从低到高
ROLE_VIEWER = "viewer"
ROLE_USER = "user"
ROLE_ADMIN = "admin"

ALL_ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_USER, ROLE_ADMIN)

#: 角色等级：用于"至少需要某角色"的判断（越大权限越高）
_ROLE_RANK: dict[str, int] = {ROLE_VIEWER: 1, ROLE_USER: 2, ROLE_ADMIN: 3}

#: 新建账号的默认角色：保持"注册后即可上传与提问"的既有可用性
DEFAULT_ROLE = ROLE_USER


def normalize_role(role: str | None) -> str:
    """把任意输入归一到合法角色；未知值一律降级为最低权限（fail-safe）。"""
    value = (role or "").strip().lower()
    return value if value in _ROLE_RANK else ROLE_VIEWER


def role_rank(role: str | None) -> int:
    return _ROLE_RANK[normalize_role(role)]


def normalize_tenant(tenant: str | None, default: str = "default") -> str:
    """租户标识归一：去空白、限长、非法字符替换。

    这是**演示级**的租户准入（注册时带 tenant 字段）。生产应由邀请码 / SSO 决定，
    但无论准入怎么变，"标识必须被归一并且非空"这条不会变。
    """
    value = (tenant or "").strip() or (default or "default")
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value)
    return (safe[:64] or "default")


@dataclass(frozen=True)
class Principal:
    """一次请求的身份：谁、属于哪个租户、什么角色。

    取代 V1 里"``get_current_user`` 只返回 ``int``"的写法 —— 那样路由拿不到
    租户与角色，只能退化成全局共享，无法做隔离。
    """

    user_id: int
    username: str = ""
    tenant_id: str = "default"
    role: str = DEFAULT_ROLE

    def __post_init__(self) -> None:
        # 归一化放在构造期，保证后续比较永远面对规范值
        object.__setattr__(self, "tenant_id", normalize_tenant(self.tenant_id))
        object.__setattr__(self, "role", normalize_role(self.role))

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def can_write(self) -> bool:
        """能否上传/修改文档（viewer 只读）。"""
        return role_rank(self.role) >= _ROLE_RANK[ROLE_USER]

    def at_least(self, role: str) -> bool:
        return role_rank(self.role) >= role_rank(role)

    def require(self, *roles: str) -> None:
        """不在允许角色内则 403。用于服务层二次校验（路由层已用依赖拦过一道）。"""
        allowed = {normalize_role(r) for r in roles} or {ROLE_ADMIN}
        if self.role not in allowed:
            raise AppError(
                f"当前角色（{self.role}）无权执行该操作，需要：{'/'.join(sorted(allowed))}",
                403,
            )


def require_role(principal: Principal, *roles: str) -> None:
    """函数式写法，便于在依赖里直接调用。"""
    principal.require(*roles)


def require_any(principal: Principal, roles: Iterable[str]) -> None:
    principal.require(*list(roles))
