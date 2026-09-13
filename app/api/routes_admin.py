"""租户管理接口（V2 新增）：本租户用户列表 + admin 代建账号 + 邀请码签发/查看/删除。

为什么单独成文件：用户管理属于"管理面"，与面向知识库业务的
文档 / 问答 / 评测路由不是同一类关注点；分文件能让权限审计时一眼看到
**管理面只有这一处入口**（挂载点在 `app/factory.py`，由 Lead 负责）。

权限模型（见 docs/v2-plan.md §6.2）：

- 所有端点都要求 ``admin``；
- 租户边界**不可跨越**：admin 只能看 / 只能建 / 只能签发 / 只能删**自己租户**的东西。
  body 里若指定了别的租户，直接 403，而**不是静默改写** ——
  静默改写会让调用方误以为"在别的租户建号成功"，是更危险的失败模式。
- 查看与删除的租户条件下沉到 DAO（`list_invites` / `get_invite` 都带 `tenant_id`），
  跨租户的码一律表现为"不存在"（404），不泄漏"这个码存在但你无权"。这样即使路由层
  的判空漏写，也不会越界删到别人的码 —— 防御点不依赖单一处判断。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import require_admin
from app.models.schemas import CreateInviteRequest, CreateUserRequest, InviteInfo, UserInfo
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


# --------------------------------------------------------------------------- #
# 邀请码（V2 准入凭据）
# --------------------------------------------------------------------------- #

@router.post(
    "/admin/invites",
    response_model=InviteInfo,
    summary="签发邀请码",
    description=(
        "签发一个邀请码：持码自助注册的账号，其**租户与角色由这个码决定**。"
        "默认只能签发给调用者自己的租户，指定别的租户直接 403（不静默改写）；"
        "**例外**：`DEFAULT_TENANT`（平台租户）的管理员可以为其它租户签发 —— "
        "否则新租户永远开通不了（要签码得先有那个租户的 admin，鸡生蛋）。"
        "`max_uses` / `expires_in_hours` 留空则用服务端默认值。"
    ),
    response_description="新建邀请码（`code` 即凭据，请安全传给被邀请人）",
)
def create_invite(
    request: Request,
    body: CreateInviteRequest,
    principal: Principal = Depends(require_admin),
) -> dict:
    # 先归一化再比较，避免 " tenant-b " / 大小写之类的写法绕过判断。
    target_tenant = normalize_tenant(body.tenant, principal.tenant_id)
    platform_tenant = normalize_tenant(request.app.state.container.settings.default_tenant)
    if target_tenant != principal.tenant_id and principal.tenant_id != platform_tenant:
        # 跨租户签发只放给平台租户的管理员：他是唯一有全局视角的角色，
        # 也是"新租户怎么开通"这个问题的答案（详见 docs/deployment.md §9.3）。
        raise AppError(
            "管理员只能为自己所属租户签发邀请码；跨租户签发需要平台租户（DEFAULT_TENANT）的管理员",
            403,
        )

    auth = request.app.state.container.auth
    # max_uses / expires_in_hours 为 None 时不在这里填默认值：默认值属于业务参数，
    # 由 AuthService 按 INVITE_DEFAULT_MAX_USES / INVITE_TTL_HOURS 决定，
    # 路由层再兜一次就会形成两份默认值（改动一处忘另一处）。
    return auth.create_invite(
        target_tenant,
        role=body.role,
        max_uses=body.max_uses,
        expires_in_hours=body.expires_in_hours,
        created_by=principal.user_id,
    )


@router.get(
    "/admin/invites",
    response_model=List[InviteInfo],
    summary="本租户邀请码列表",
    description="列出**当前管理员所在租户**的全部邀请码（含已用次数与过期时间）。",
    response_description="邀请码列表",
)
def list_invites(
    request: Request,
    principal: Principal = Depends(require_admin),
) -> List[dict]:
    rows = request.app.state.container.db.list_invites(principal.tenant_id)
    return [
        {
            "code": r["code"],
            # 行内 tenant_id 缺省时回填调用者租户：查询本身已限定租户，回填不会泄漏跨租户数据
            "tenant_id": r.get("tenant_id") or principal.tenant_id,
            "role": normalize_role(r.get("role")),
            "max_uses": int(r.get("max_uses") or 0),
            "used_count": int(r.get("used_count") or 0),
            "expires_at": _as_text(r.get("expires_at")),
            "created_at": _as_text(r.get("created_at")),
        }
        for r in rows
    ]


@router.delete(
    "/admin/invites/{code}",
    summary="删除邀请码",
    description=(
        "删除本租户的邀请码（作废准入凭据）。"
        "跨租户的码按**不存在**处理返回 404，不泄漏「这个码存在但你无权」。"
    ),
    response_description="删除结果",
)
def delete_invite(
    request: Request,
    code: str = Path(..., description="邀请码"),
    principal: Principal = Depends(require_admin),
) -> dict:
    db = request.app.state.container.db
    # 先按租户查存在性：`get_invite` 自带 tenant_id 条件，所以跨租户天然查不到。
    # 不先判存在直接删会让"删掉了"和"没删掉"返回同一个 200，调用方无法分辨。
    if db.get_invite(code, principal.tenant_id) is None:
        raise AppError("邀请码不存在", 404)
    db.delete_invite(code, principal.tenant_id)
    # 与既有 DELETE 端点一致的返回形状（routes_documents / routes_conversations）
    return {"status": "deleted", "code": code}
