"""控制面业务：登录 / 登出 / 「我的视图」 + 套餐与账号的**两个**写入口。

写入口刻意只有两条（:func:`patch_plan` / :func:`patch_account`），每条都走同一套收口：

    校验 → （可选）试算 → 落库（与审计同一个事务）→ 删套餐缓存

三处顺序都是被理由钉住的，不是随手排的：

- **校验在任何写入之前**：一次请求里改了三项，其中一项非法时不能留下「改了一半」的库；
- **审计与改动同一个事务**：不允许出现「改了但没有审计」的窗口。审计落库失败就让整次
  改动一起回滚——没有账本的改动本来就不该生效；
- **删缓存放在 commit 之后**：先删再提交时，另一个请求可能刚好读回旧行并把它写回缓存，
  于是缓存里又留下旧值。提交后删，下一次读要么读库、要么读到别人刚填进去的新值。

登录那条路对账号是否存在**不做区分**：账号不存在时也跑一遍哈希（
``app.core.passwords.dummy_verify``），耗时相同才不构成用户名枚举接口。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from sqlalchemy.orm import Session

from app.core import passwords
from app.core.console_auth import (
    ACCOUNT_STATUSES,
    MSG_ACCOUNT_DISABLED,
    STATUS_ACTIVE,
    ConsoleIdentity,
    session_ttl_seconds,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache
from app.db.base import now
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.console_session import ConsoleSession
from app.models.plan import Plan, PlanSpec
from app.models.tenant import Tenant
from app.repositories import (
    account_repository,
    audit_log_repository,
    console_session_repository,
    plan_repository,
    tenant_repository,
)
from app.services import plan_admin, plan_service
from app.services.quota_gate import read_tenant_daily_usage
from app.utils.id_generator import new_id

#: 登录失败的**唯一**对外提示：它同时覆盖「查无此人」与「口令不对」。
MSG_BAD_CREDENTIALS: Final[str] = "invalid username or password"
#: 会话标识的随机字节数（``token_urlsafe`` 之后约 43 个字符，放进 String(64)）。
SESSION_ID_BYTES: Final[int] = 32
#: 审计里的动作名。列表与筛选都按它读，所以写在一处。
ACTION_PLAN_UPDATE: Final[str] = "plan.update"
ACTION_PLAN_DELETE: Final[str] = "plan.delete"
ACTION_ACCOUNT_UPDATE: Final[str] = "account.update"


@dataclass(frozen=True)
class DailyUsageView:
    """今日租户级检索用量 + 套餐日上限。

    ``used=None`` 是**一个明确的状态**，不是缺值：限流开关关着时计数从未被写入，平台
    根本不知道今天用了多少。把它当成 0 会让面板显示一个错误的「还很充裕」。
    """

    used: int | None
    limit: int


@dataclass(frozen=True)
class ConsoleView:
    """/console/me 与登录成功的同一份载荷：我在哪个租户、买的是哪一档、今天用了多少。

    用量来自限流配额服务（票 #79）的**只读**路径：租户级日桶，与检索门禁判的是同一个
    桶（UTC、按天分桶）。账号不直接对应 Key，所以面板报的是这个租户的聚合——那正是
    「套餐日配额还剩多少」这个问题的答案。开关关着时 ``usage.used`` 为 ``None``
    （未统计），不是 0（见 :class:`DailyUsageView`）。
    """

    account: Account
    tenant: Tenant | None
    plan: PlanSpec | None
    usage: DailyUsageView | None = None


@dataclass(frozen=True)
class PlanPatchResult:
    """一次套餐改动的结果：改前、改后、影响面（只有试算才有）。"""

    plan_id: int
    dry_run: bool
    deleted: bool
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    impact: plan_admin.PlanImpact | None


@dataclass(frozen=True)
class AccountPatchResult:
    account_id: str
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True)
class AccountRowView:
    """账号清单的一行：账号 + 它所属的租户 + 那个租户当前的套餐。"""

    account: Account
    tenant: Tenant | None
    plan: Plan | None


# ──────────────────────────────────────────────────────────────── 登录 / 登出 / 我的


def login(
    db: Session, *, username: str, password: str, cache: PlanCache | None = None
) -> tuple[ConsoleSession, ConsoleView]:
    """账号口令 → 服务端会话行。返回会话（路由拿它的 id 发 cookie）与「我的视图」。

    四种失败里前两种对外完全同形（同码同消息），后两种只可能发生在**口令已经验过**之后：
    知道正确口令的人本来就知道这个账号存在，此时告诉他「账号已停用」不泄露任何东西，
    却能让真正的用户知道该找谁。
    """

    account = account_repository.get_by_username(db, username)
    if account is None:
        # 等时：账号不存在也要付一次哈希的代价，否则「查无此人」比「口令不对」快一个数量级。
        passwords.dummy_verify(password)
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_BAD_CREDENTIALS)

    if not passwords.verify_password(password, account.password_hash):
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_BAD_CREDENTIALS)

    if account.status != STATUS_ACTIVE:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_ACCOUNT_DISABLED)

    session = ConsoleSession(
        id=secrets.token_urlsafe(SESSION_ID_BYTES),
        account_id=account.id,
        expires_at=now() + timedelta(seconds=session_ttl_seconds()),
    )
    console_session_repository.create(db, session)
    db.commit()
    return session, view_for(db, account, cache=cache)


def logout(db: Session, *, session_id: str | None) -> None:
    """删服务端会话行。浏览器那一半（删 cookie）由路由做——**两半都要**。"""

    if not session_id:
        return
    session = console_session_repository.get_by_id(db, session_id)
    if session is None:
        # 已经登出过：幂等，不报错。cookie 仍然会被路由清掉。
        return
    console_session_repository.delete(db, session)
    db.commit()


def me_view(db: Session, *, account_id: str, cache: PlanCache | None = None) -> ConsoleView:
    """``/console/me``：判定结论里只有 id，这里再读一次账号行拼「我的视图」。

    这次重读不是多余：判定依赖刻意只回答「谁、什么角色、哪个租户」，而响应还要
    账号的显示状态。把状态塞进判定结论会让「改状态」必须在两个地方同时生效。
    """

    account = account_repository.get_by_id(db, account_id)
    if account is None:
        # 判定刚读过同一行，这里仍可能为空（并发停用 + 删除）。与判定同码同消息。
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_ACCOUNT_DISABLED)
    return view_for(db, account, cache=cache)


def view_for(db: Session, account: Account, *, cache: PlanCache | None = None) -> ConsoleView:
    """拼「我的视图」：账号 → 它的租户 → 租户当前套餐 → 租户今日用量。

    套餐走 :func:`app.services.plan_service.resolve_plan`（按 id 的缓存），于是管理员刚
    改完的额度，面板刷新一次就看到——改套餐那条路提交后会删缓存。用量走配额服务的
    **只读**路径（见 :func:`app.services.quota_gate.read_tenant_daily_usage`）。
    """

    tenant = _tenant_of(db, account)
    if tenant is None:
        # 平台管理员不属于任何租户：没有套餐、也没有租户级用量可显示，这不是错误。
        return ConsoleView(account=account, tenant=None, plan=None, usage=None)
    plan = plan_service.resolve_plan(db, tenant.plan_id, cache=cache)
    return ConsoleView(
        account=account,
        tenant=tenant,
        plan=plan,
        usage=DailyUsageView(
            # 只读路径：读一次日桶，不在任何地方 +1（见 quota_gate.read_tenant_daily_usage）。
            used=read_tenant_daily_usage(tenant_id=tenant.id),
            limit=plan.retrieve_daily,
        ),
    )


def _tenant_of(db: Session, account: Account) -> Tenant | None:
    """账号 → 它的租户；管理员不属于任何租户，返回 ``None``（不是错误）。

    ``/console/me``、登录成功后的视图与账号清单问的是同一个问题，条件读法因此只写一处：
    「没有 tenant_id」与「tenant_id 指向的行不存在」在这里是同一个答案（没有租户），
    调用方不必各自再判一次。
    """

    return tenant_repository.get_by_id(db, account.tenant_id) if account.tenant_id else None


# ─────────────────────────────────────────────────────────────────────── 套餐


def list_plans(db: Session) -> list[Plan]:
    """管理员看全部套餐（含管理员刚改过、缓存里可能还没有的那些）。"""

    return plan_repository.list_all(db)


def patch_plan(
    db: Session,
    *,
    plan_id: int,
    operation: str,
    patch: dict[str, Any],
    dry_run: bool,
    actor: ConsoleIdentity,
    cache: PlanCache | None = None,
) -> PlanPatchResult:
    """改 / 删套餐的**唯一**入口；``dry_run=True`` 时只算不写。

    ``dry_run`` 由路由给（默认 ``True``）：忘了这个参数时，一个 PATCH 只会试算——最坏的
    结果是「少改了一次」，而不是「改错了生产配置」。
    """

    row = plan_repository.get_by_id(db, plan_id)
    if row is None:
        raise AppError(ErrorCode.PLAN_NOT_FOUND, f"plan not found: {plan_id}")
    before = plan_admin.plan_snapshot(row)

    if operation == "delete":
        if patch:
            raise AppError(ErrorCode.PARAM_ERROR, "delete takes no fields")
        plan_admin.ensure_deletable(db, plan_id=plan_id)
        if dry_run:
            return PlanPatchResult(
                plan_id=plan_id,
                dry_run=True,
                deleted=True,
                before=before,
                after=None,
                impact=None,
            )
        db.delete(row)
        _append_audit(
            db,
            actor=actor,
            action=ACTION_PLAN_DELETE,
            target=f"plan:{plan_id}",
            before=before,
            after=None,
        )
        db.commit()
        _invalidate(cache, plan_id)
        return PlanPatchResult(
            plan_id=plan_id, dry_run=False, deleted=True, before=before, after=None, impact=None
        )

    if operation != "update":
        raise AppError(ErrorCode.PARAM_ERROR, f"unknown operation: {operation}")

    plan_admin.validate_patch(patch)
    _ensure_code_free(db, plan_id=plan_id, patch=patch)
    after = plan_admin.merged_snapshot(before, patch)

    if dry_run:
        impact = plan_admin.preview_impact(
            db,
            plan_id=plan_id,
            max_kb=after["max_kb"],
            allowed_profiles=after["allowed_profiles"],
        )
        return PlanPatchResult(
            plan_id=plan_id,
            dry_run=True,
            deleted=False,
            before=before,
            after=after,
            impact=impact,
        )

    plan_admin.apply_patch(row, patch)
    _append_audit(
        db,
        actor=actor,
        action=ACTION_PLAN_UPDATE,
        target=f"plan:{plan_id}",
        before=before,
        after=after,
    )
    db.commit()
    # 删缓存，不写缓存（ADR-0017）：写要每处修改都记得同步，漏一处就留下「看起来更新过、
    # 实际是旧值」的条目；删让下一次读自然拿到新值。
    _invalidate(cache, plan_id)
    return PlanPatchResult(
        plan_id=plan_id,
        dry_run=False,
        deleted=False,
        before=before,
        after=after,
        impact=None,
    )


def _ensure_code_free(db: Session, *, plan_id: int, patch: dict[str, Any]) -> None:
    """改 ``code`` 时不许撞上另一档：``code`` 是脚本与日志指代套餐的名字。

    「另一档」按主键比，不按 code 比：把自己改成自己原来那个 code 是合法的空改动。
    """

    code = patch.get("code")
    if code is None:
        return
    existing = plan_repository.get_by_code(db, code)
    if existing is not None and str(existing.id) != str(plan_id):
        raise AppError(ErrorCode.PLAN_INVALID, f"plan code already in use: {code}")


def _invalidate(cache: PlanCache | None, plan_id: int) -> None:
    plan_service.invalidate_plan(_cache_or_default(cache), plan_id)


def _cache_or_default(cache: PlanCache | None) -> PlanCache:
    """缓存缺省取生产实现（Redis）；测试注入内存假实现，于是「改了之后缓存里没有了」可断言。

    通过 ``plan_service.default_plan_cache`` 取而不是在这里再建一个：缓存的构造点只能有
    一处，否则「连的是哪个库」会散开。
    """

    return plan_service.default_plan_cache() if cache is None else cache


# ─────────────────────────────────────────────────────────────────────── 账号


def list_accounts(db: Session) -> list[AccountRowView]:
    """全部账号 + 各自租户与套餐。管理员跨租户看，所以这里**故意**不按租户过滤。"""

    views: list[AccountRowView] = []
    for account in account_repository.list_all(db):
        tenant = _tenant_of(db, account)
        plan = plan_repository.get_by_id(db, tenant.plan_id) if tenant is not None else None
        views.append(AccountRowView(account=account, tenant=tenant, plan=plan))
    return views


def patch_account(
    db: Session,
    *,
    account_id: str,
    plan_id: int | None,
    status: str | None,
    tenant_status: str | None,
    actor: ConsoleIdentity,
) -> AccountPatchResult:
    """改一个账号：换它租户的套餐 / 停用账号 / 停用租户。

    **先全量校验、再逐项落库**：三项里最后一项非法时，不能留下前两项已经改掉的库。
    停用只改状态列，**不删会话行**——账号状态是每个请求现读的，所以旧 cookie 下一个
    请求就失效；删会话只是清理，不该成为「停用是否生效」的前提。
    """

    account = account_repository.get_by_id(db, account_id)
    if account is None:
        raise AppError(ErrorCode.NOT_FOUND, f"account not found: {account_id}")
    if plan_id is None and status is None and tenant_status is None:
        raise AppError(ErrorCode.PARAM_ERROR, "no fields to change")

    tenant = tenant_repository.get_by_id(db, account.tenant_id) if account.tenant_id else None
    if (plan_id is not None or tenant_status is not None) and tenant is None:
        raise AppError(ErrorCode.PARAM_ERROR, "account belongs to no tenant")

    if status is not None and status not in ACCOUNT_STATUSES:
        raise AppError(ErrorCode.PARAM_ERROR, f"unknown account status: {status}")
    if tenant_status is not None and tenant_status not in ACCOUNT_STATUSES:
        raise AppError(ErrorCode.PARAM_ERROR, f"unknown tenant status: {tenant_status}")
    plan: Plan | None = None
    if plan_id is not None:
        plan = plan_repository.get_by_id(db, plan_id)
        if plan is None:
            raise AppError(ErrorCode.PLAN_NOT_FOUND, f"plan not found: {plan_id}")

    before = _account_state(account, tenant)

    if status is not None:
        account.status = status
    if tenant is not None and tenant_status is not None:
        tenant.status = tenant_status
    if tenant is not None and plan is not None:
        tenant.plan_id = plan.id

    after = _account_state(account, tenant)
    _append_audit(
        db,
        actor=actor,
        action=ACTION_ACCOUNT_UPDATE,
        target=f"account:{account_id}",
        before=before,
        after=after,
    )
    db.commit()
    return AccountPatchResult(account_id=account_id, before=before, after=after)


def _account_state(account: Account, tenant: Tenant | None) -> dict[str, Any]:
    """账号 / 租户 / 套餐指针的同一份快照：改前与改后都从这里造。

    审计的前后快照必须是同一个形状，否则「改了什么」要靠人肉对比两份不同键名的 JSON。
    三键的清单因此只写一处；租户为空（管理员）时两项如实为 ``None``。
    """

    return {
        "account_status": account.status,
        "tenant_status": tenant.status if tenant is not None else None,
        "tenant_plan_id": tenant.plan_id if tenant is not None else None,
    }


# ─────────────────────────────────────────────────────────────────────── 审计


def list_audit_logs(db: Session, *, limit: int) -> list[AuditLog]:
    return audit_log_repository.list_recent(db, limit=limit)


def _append_audit(
    db: Session,
    *,
    actor: ConsoleIdentity | None,
    action: str,
    target: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> AuditLog:
    """写一条账本行（不 commit）。``actor=None`` 表示系统动作——不塞假账号。"""

    return audit_log_repository.create(
        db,
        AuditLog(
            id=new_id(),
            actor_id=None if actor is None else actor.account_id,
            action=action,
            target=target,
            before_snapshot=before,
            after_snapshot=after,
            # 显式给时间戳，不靠列的 default：审计的时间是「这次改动发生在哪一刻」的
            # 唯一证据，让它在落库路径之外（如测试里的假会话）也能被读到。
            created_at=now(),
        ),
    )


#: 审计列表的默认条数与硬上限：面板上没有分页控件，一次拉太多只会把浏览器拖住。
AUDIT_LOG_DEFAULT_LIMIT: Final[int] = 50
AUDIT_LOG_MAX_LIMIT: Final[int] = 200


def audit_limit(requested: int | None) -> int:
    """把调用方给的条数收进 ``[1, AUDIT_LOG_MAX_LIMIT]``；不合法就按默认值。"""

    if requested is None or requested < 1:
        return AUDIT_LOG_DEFAULT_LIMIT
    return min(requested, AUDIT_LOG_MAX_LIMIT)
