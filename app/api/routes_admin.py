"""租户管理接口（V2 新增）：本租户用户列表 + admin 代建账号。

为什么单独成文件：用户管理属于"管理面"，与面向知识库业务的
文档 / 问答 / 评测路由不是同一类关注点；分文件能让权限审计时一眼看到
**管理面只有这一处入口**（挂载点在 `app/factory.py`，由 Lead 负责）。

权限模型（见 docs/v2-plan.md §6.2）：

- 两个端点都要求 ``admin``；
- 租户边界**不可跨越**：admin 只能看 / 只能建**自己租户**的账号。
  body 里若指定了别的租户，直接 403，而**不是静默改写** ——
  静默改写会让调用方误以为"在别的租户建号成功"，是更危险的失败模式。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_admin
from app.models.schemas import CreateUserRequest, UserInfo
from app.services.tenancy import Principal, normalize_role, normalize_tenant
from app.utils.exceptions import AppError

router = APIRouter(tags=["租户管理"])


def _as_text(value: object) -> str | None:
    """`created_at` 可能是 datetime 也可能是字符串，统一转成字符串交给 pydantic。"""
    return None if value is None else str(value)


@router.get(
    "/admin/users",
    response_model=List[UserInfo],
    summary="租户内用户列表",
    description="列出**当前管理员所在租户**的全部用户（角色、创建时间）。",
    response_description="用户列表",
)
def list_users(
    request: Request,
    principal: Principal = Depends(require_admin),
) -> List[dict]:
    rows = request.app.state.container.db.list_users(principal.tenant_id)
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "role": normalize_role(r.get("role")),
            # 行内 tenant_id 缺省时回填调用者租户：查询本身已限定租户，回填不会泄漏跨租户数据
            "tenant_id": r.get("tenant_id") or principal.tenant_id,
            "created_at": _as_text(r.get("created_at")),
        }
        for r in rows
    ]


@router.post(
    "/admin/users",
    response_model=UserInfo,
    summary="代建账号",
    description=(
        "由管理员在本租户内创建账号（自助注册关闭时的开通途径）。"
        "租户固定为调用者所在租户；角色经归一化，未知值降级为 viewer。"
    ),
    response_description="新建用户信息（不含密码）",
)
def create_user(
    request: Request,
    body: CreateUserRequest,
    principal: Principal = Depends(require_admin),
) -> dict:
    # 唯一的准入判断：不允许指定别的租户。归一化后比较，避免大小写/空白导致的误判
    if normalize_tenant(body.tenant, principal.tenant_id) != principal.tenant_id:
        raise AppError("管理员只能在自己所属租户内创建账号", 403)

    role = normalize_role(body.role)
    auth = request.app.state.container.auth
    # 用户名在本租户内已存在时，AuthService 会抛 AppError(409)；这里不重复查库，避免两处规则漂移
    result = auth.create_user(body.username, body.password, principal.tenant_id, role)

    # 取刚建账号的 user_id：AuthService.create_user 的返回体是 token/username/role/tenant_id，
    # 不含 user_id，而 UserInfo 需要它 —— 用"建完立刻按租户+用户名回查"补齐。
    # 不选"解 JWT 取 sub"：那会把路由层和 JWT 密钥耦合起来，且并不比这次查询便宜。
    row = request.app.state.container.db.get_user_by_username(result["username"], principal.tenant_id) or {}
    return {
        "user_id": int(row.get("id") or row.get("user_id") or 0),
        "username": result["username"],
        "role": result["role"],
        "tenant_id": result["tenant_id"],
        "created_at": _as_text(row.get("created_at")),
    }
