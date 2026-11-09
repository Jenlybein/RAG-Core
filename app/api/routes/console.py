"""控制面 HTTP 接口（``/console/*``）：登录 / 登出 / 我的视图 / 套餐与账号管理 / 审计。

这一层只做「取参数、调服务、拼响应」，判定全在 :mod:`app.core.console_auth`，写入收口
在 :mod:`app.services.console_service`。两条红线写在这里，因为它们都是「少一个参数」的
事故：

- **鉴权只用会话 cookie**，不 import 数据面的 ``get_current_key``：浏览器不会带
  ``Authorization``，而数据面的 Key 也不该能用来管理平台；
- **所有会改东西的接口都走 ``require_console_admin_write`` / ``require_console_write_header``**：
  带 cookie 的浏览器请求会自动跟着跨站请求一起发出，没有显式头的写操作必须拒绝。

``PATCH /console/plans/{plan_id}`` 的 ``dry_run`` **默认 True**：忘记传这个参数时最坏的
结果是「只试算、没保存」，而不是「改了生产配置却没看到影响面」。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.console_deps import (
    get_console_account,
    get_console_admin,
    get_plan_cache,
    require_console_admin_write,
    require_console_write_header,
)
from app.core.console_auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    ConsoleIdentity,
    is_production,
    session_ttl_seconds,
)
from app.core.plan_cache import PlanCache
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.console import (
    AccountItem,
    AccountListData,
    AccountPatchData,
    AccountPatchRequest,
    AuditLogListData,
    ImpactGroupData,
    LoginRequest,
    MeData,
    PlanData,
    PlanImpactData,
    PlanListData,
    PlanPatchData,
    PlanPatchRequest,
    account_data,
    audit_log_item,
    plan_data,
    plan_snapshot_data,
    tenant_data,
    usage_data,
)
from app.services import console_service
from app.services.login_throttle import login_throttle
from app.services.plan_admin import PlanImpact

router = APIRouter(prefix="/console", tags=["console"])


def _me_data(view: console_service.ConsoleView) -> MeData:
    """「我的视图」→ 响应体；管理员没有租户，于是 tenant / plan / usage 三栏为 ``null``。"""

    return MeData(
        account=account_data(view.account),
        tenant=tenant_data(view.tenant) if view.tenant is not None else None,
        plan=plan_snapshot_data(view.plan) if view.plan is not None else None,
        usage=(
            usage_data(used=view.usage.used, limit=view.usage.limit)
            if view.usage is not None
            else None
        ),
    )


def _plan_data_or_none(snapshot: dict | None) -> PlanData | None:
    return None if snapshot is None else PlanData(**snapshot)


def _impact_data(impact: PlanImpact | None) -> PlanImpactData | None:
    if impact is None:
        return None
    return PlanImpactData(
        tenants_checked=impact.tenants_checked,
        over_max_kb=ImpactGroupData(
            count=impact.over_max_kb.count,
            sample=list(impact.over_max_kb.sample),
        ),
        lose_profile=ImpactGroupData(
            count=impact.lose_profile.count,
            sample=list(impact.lose_profile.sample),
        ),
        policies_losing_profile=impact.policies_losing_profile,
    )


def _set_session_cookie(response: Response, session_id: str) -> None:
    """下发会话 cookie：HttpOnly 永远开；Secure 只在生产开（本地 http 带 Secure 会被丢）。"""

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=session_ttl_seconds(),
        path=SESSION_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=is_production(),
    )


def _delete_session_cookie(response: Response) -> None:
    """删浏览器那一半；路径必须与下发时一致，否则浏览器会保留原 cookie。"""

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=is_production(),
    )


def _client_ip(request: Request) -> str:
    """限流键里的 IP 分量：**传输层对端地址**，不是 ``X-Forwarded-For``。

    后者在没配置可信代理拓扑时完全由请求方填写，拿它做限流键等于把「换一个头就换一份
    额度」交给攻击者。代价是部署在反向代理之后时，所有用户共享代理地址这一个分量——
    键里还有账号，所以「一个账号被暴力猜测」仍然按账号单独计数，那是本票的主线。
    """

    client = request.client
    return client.host if client is not None else "unknown"


# ─────────────────────────────────────────────────────────────────── 登录 / 登出


@router.post("/login", response_model=ApiResponse[MeData], summary="控制台登录")
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    cache: PlanCache = Depends(get_plan_cache),
) -> ApiResponse[MeData]:
    """账号口令 → 服务端会话行 + HttpOnly cookie；失败统一 ``20010``，超限 ``20005``。

    限流在验口令**之前**：一次尝试先记上，失败与不存在的账号都会计数；超限时连账号表
    都不读，所以「是否存在」不会通过耗时或错误码泄露。开关关着时 ``login_throttle``
    返回 ``None``，整段是 no-op（默认套件因此不碰 Redis）。
    """

    client_ip = _client_ip(request)
    throttle = login_throttle()
    if throttle is not None:
        # 先记这次尝试，再验口令；被拒的那一次不写（见 login_throttle 模块文档）。
        throttle.register_attempt(username=payload.username, client_ip=client_ip)

    session, view = console_service.login(
        db, username=payload.username, password=payload.password, cache=cache
    )

    if throttle is not None:
        # 成功才清除这一对：合法用户输错几次后登进来，不该在下一个窗口继续被惩罚。
        throttle.reset(username=payload.username, client_ip=client_ip)
    _set_session_cookie(response, session.id)
    return ok(_me_data(view))


@router.post("/logout", response_model=ApiResponse[None], summary="控制台登出")
def logout(
    request: Request,
    response: Response,
    _write_header: None = Depends(require_console_write_header),
    db: Session = Depends(get_db),
) -> ApiResponse[None]:
    """服务端删行 + 客户端删 cookie：**两半都做**，少一半就留下一个还能用的会话。

    不要求会话有效：已经掉线的人点登出也要能把浏览器里的旧 cookie 清掉，否则共用电脑
    上会一直留下一个已经失效、却还在被发送的凭证。
    """

    console_service.logout(db, session_id=request.cookies.get(SESSION_COOKIE_NAME))
    _delete_session_cookie(response)
    return ok(None)


# ─────────────────────────────────────────────────────────────────────── 我的视图


@router.get("/me", response_model=ApiResponse[MeData], summary="我的账号、租户、套餐与今日用量")
def me(
    identity: ConsoleIdentity = Depends(get_console_account),
    db: Session = Depends(get_db),
    cache: PlanCache = Depends(get_plan_cache),
) -> ApiResponse[MeData]:
    """调用方的账号、所属租户、套餐快照与今日用量。

    用量读的是租户级日桶（只读、不 +1）。开关关着时 ``usage.used`` 为 ``null``、
    ``usage.source`` 为 ``not_tracked``——平台压根没在统计，报 0 会骗人。
    """

    view = console_service.me_view(db, account_id=identity.account_id, cache=cache)
    return ok(_me_data(view))


# ─────────────────────────────────────────────────────────────────────── 套餐


@router.get("/plans", response_model=ApiResponse[PlanListData], summary="套餐列表（管理员）")
def list_plans(
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: Session = Depends(get_db),
) -> ApiResponse[PlanListData]:
    rows = console_service.list_plans(db)
    return ok(PlanListData(plans=[plan_data(row) for row in rows]))


@router.patch(
    "/plans/{plan_id}",
    response_model=ApiResponse[PlanPatchData],
    summary="改 / 删套餐（先试算、后落库）",
)
def patch_plan(
    plan_id: int,
    payload: PlanPatchRequest,
    dry_run: bool = Query(default=True, description="true 只试算，false 才落库"),
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
    cache: PlanCache = Depends(get_plan_cache),
) -> ApiResponse[PlanPatchData]:
    """套餐的**唯一**写入口：校验 → 试算 → 落库 + 审计 → 删缓存。"""

    result = console_service.patch_plan(
        db,
        plan_id=plan_id,
        operation=payload.operation,
        patch=payload.provided(),
        dry_run=dry_run,
        actor=identity,
        cache=cache,
    )
    return ok(
        PlanPatchData(
            plan_id=result.plan_id,
            dry_run=result.dry_run,
            deleted=result.deleted,
            before=_plan_data_or_none(result.before),
            after=_plan_data_or_none(result.after),
            impact=_impact_data(result.impact),
        )
    )


# ─────────────────────────────────────────────────────────────────────── 账号


@router.get("/accounts", response_model=ApiResponse[AccountListData], summary="账号列表（管理员）")
def list_accounts(
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: Session = Depends(get_db),
) -> ApiResponse[AccountListData]:
    """管理员跨租户看所有账号；每行带上它所属租户与那个租户当前的套餐。"""

    rows = console_service.list_accounts(db)
    return ok(
        AccountListData(
            accounts=[
                AccountItem(
                    account_id=row.account.id,
                    username=row.account.username,
                    role=row.account.role,
                    status=row.account.status,
                    tenant_id=row.account.tenant_id,
                    tenant_name=row.tenant.name if row.tenant is not None else None,
                    tenant_status=row.tenant.status if row.tenant is not None else None,
                    plan=plan_data(row.plan) if row.plan is not None else None,
                )
                for row in rows
            ]
        )
    )


@router.patch(
    "/accounts/{account_id}",
    response_model=ApiResponse[AccountPatchData],
    summary="换套餐 / 停用账号 / 停用租户（管理员）",
)
def patch_account(
    account_id: str,
    payload: AccountPatchRequest,
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
) -> ApiResponse[AccountPatchData]:
    """改一个账号：三项字段全量校验后再落库，并写一条带前后快照的审计。"""

    result = console_service.patch_account(
        db,
        account_id=account_id,
        plan_id=payload.plan_id,
        status=payload.status,
        tenant_status=payload.tenant_status,
        actor=identity,
    )
    return ok(AccountPatchData(account_id=result.account_id, before=result.before, after=result.after))


# ─────────────────────────────────────────────────────────────────────── 审计


@router.get(
    "/audit-logs",
    response_model=ApiResponse[AuditLogListData],
    summary="审计列表（管理员）",
)
def list_audit_logs(
    limit: int | None = Query(default=None, description="最多返回几条，缺省 50、上限 200"),
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: Session = Depends(get_db),
) -> ApiResponse[AuditLogListData]:
    rows = console_service.list_audit_logs(db, limit=console_service.audit_limit(limit))
    return ok(AuditLogListData(audit_logs=[audit_log_item(row) for row in rows]))
