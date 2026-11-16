"""控制面（``/console/*``）的请求与响应模型。

与数据面同一套壳（``{code, msg, data}``），但**没有一处复用数据面的入参**：那一边的请求
体只允许 ``kb_ids`` + ``query``，这一边是人手动填的表单。

两处刻意的写法：

- 套餐的六个数值上限**不写** ``ge=1``。写在 schema 上时，``0`` 会先被判成「参数结构错误」
  （``20002``），而票面要求的是「套餐非法」（``20018``）。校验放在
  :func:`app.services.plan_admin.validate_patch` 里，错误码才是对的那个；
- PATCH 的字段全部可空，且 ``None`` 表示「这次不动它」（``exclude_none``）。客户端发
  ``"max_kb": null`` 与不发这个键是同一件事——「把它改成空」不是一种合法意图。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.plan import Plan, PlanSpec, plan_columns_of
from app.models.tenant import Tenant


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AccountData(BaseModel):
    account_id: str
    username: str
    role: str
    status: str
    tenant_id: str | None


class TenantData(BaseModel):
    tenant_id: str
    name: str
    status: str
    plan_id: int


class PlanSnapshotData(BaseModel):
    """``/console/me`` 里的套餐快照：**没有 plan_id**，租户那一栏里已经有了。

    快照来自缓存/解析结果（:class:`app.models.plan.PlanSpec`），它刻意不带主键——判定内核
    只关心「这一档的能力位、额度与上限」。
    """

    code: str
    name: str
    # ── 五个授权位（与密钥配置同名同义，这里是上限）。
    retrieve_allowed: bool
    rewrite_allowed: bool
    decompose_allowed: bool
    research_allowed: bool
    rerank_allowed: bool
    # ── 三本账 × 两格。
    retrieve_qps: int
    retrieve_daily: int
    rerank_qps: int
    rerank_daily: int
    llm_qps: int
    llm_daily: int
    # ── 质量封顶与规模上限。
    max_top_k: int
    max_kb: int
    max_kb_per_retrieve: int
    max_documents_per_kb: int
    max_processing_documents: int


class PlanData(PlanSnapshotData):
    """管理员视角的一行套餐：多一个主键与更新时间（面板按它渲染与排序）。"""

    plan_id: int
    updated_at: datetime | None = None


#: 用量的来源标记：``used`` 是否是一个**被统计过**的数字，而不是「恰好没有值」。
UsageSource = Literal["daily_counter", "not_tracked"]

USAGE_SOURCE_DAILY_COUNTER: Final[UsageSource] = "daily_counter"
USAGE_SOURCE_NOT_TRACKED: Final[UsageSource] = "not_tracked"

#: 「没在统计」的对外措辞。刻意不写成 0：面板显示「今日 0 次」而实际已经跑了几千次，
#: 比明说「不知道」更糟——用户会据此判断额度还很充裕。
USAGE_NOTE_NOT_TRACKED: Final[str] = (
    "today's usage is not tracked: rate limiting is disabled (RATE_LIMIT_ENABLED=false)"
)


class UsageData(BaseModel):
    """今日租户级检索用量。

    ``used=None`` + ``source="not_tracked"``：限流开关关着，计数从未被写入，平台不知道
    今天用了多少；``limit`` 仍然给出（它来自套餐，与统计开关无关），让面板能如实显示
    「上限已知、用量未知」。开关打开且今天还没有成功检索时是 ``used=0`` +
    ``source="daily_counter"``——那是**已知的 0**，与「不知道」必须可区分。
    """

    used: int | None
    limit: int
    source: UsageSource
    note: str | None = None


class MeData(BaseModel):
    account: AccountData
    tenant: TenantData | None
    plan: PlanSnapshotData | None
    #: 今日租户级检索用量。管理员没有租户，因此为 ``None``；租户账号一定有一份
    #: ``UsageData``，其中 ``used=None`` 表示「开关关着、平台没在统计」。
    usage: UsageData | None = None
    #: 今日租户级 **Research** 用量（票 #150）：与 ``usage`` **同形**（``used`` + ``limit``
    #: + ``source=daily_counter``），只是读另一本账（``quota:research:*``）。分开报是因为
    #: 两条链路各有各的额度——把它们加在一起，面板上就没有「哪一项快用完了」这个答案了。
    llm_usage: UsageData | None = None
    # 第 13 步：profile_presets 字段随档位表删除——参数直存，档位预设不再存在。


class ImpactGroupData(BaseModel):
    count: int
    sample: list[str]


class PlanImpactData(BaseModel):
    tenants_checked: int
    over_max_kb: ImpactGroupData
    #: 新授权位之下「配置给了、套餐不给」的租户（下一次调用撞 20013，改配置撞 20015）。
    over_capability: ImpactGroupData
    policies_over_capability: int


class PlanPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``delete`` 也走这条入口：删与改共用一套校验、审计与缓存失效，不给第二条写路径。
    operation: Literal["update", "delete"] = "update"
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    # ── 五个授权位。
    retrieve_allowed: bool | None = None
    rewrite_allowed: bool | None = None
    decompose_allowed: bool | None = None
    research_allowed: bool | None = None
    rerank_allowed: bool | None = None
    # ── 三本账 × 两格。``retrieve`` 那两格必须 ≥ 1；加工能力的两本**允许填 0**
    #: （= 这项能力没买），负数仍被 ``20018`` 拒掉（判据在
    #: :func:`app.services.plan_admin.validate_patch`）。
    retrieve_qps: int | None = None
    retrieve_daily: int | None = None
    rerank_qps: int | None = None
    rerank_daily: int | None = None
    llm_qps: int | None = None
    llm_daily: int | None = None
    #: 质量封顶：0 = 不额外封顶（退回平台硬上限）。
    max_top_k: int | None = None
    max_kb: int | None = None
    max_kb_per_retrieve: int | None = None
    max_documents_per_kb: int | None = None
    max_processing_documents: int | None = None

    def provided(self) -> dict[str, Any]:
        """这次真的要写的字段：没发的、以及发了 ``null`` 的都不算。"""

        return self.model_dump(exclude_unset=True, exclude_none=True, exclude={"operation"})


class PlanPatchData(BaseModel):
    plan_id: int
    dry_run: bool
    deleted: bool
    before: PlanData | None
    after: PlanData | None
    impact: PlanImpactData | None


class PlanListData(BaseModel):
    plans: list[PlanData]


class AccountItem(BaseModel):
    account_id: str
    username: str
    role: str
    status: str
    tenant_id: str | None
    tenant_name: str | None
    tenant_status: str | None
    plan: PlanData | None


class AccountListData(BaseModel):
    accounts: list[AccountItem]


class AccountPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 换的是**这个账号所属租户**的套餐：套餐挂在租户上，不挂在账号上。
    plan_id: int | None = None
    status: str | None = None
    tenant_status: str | None = None


class AccountPatchData(BaseModel):
    account_id: str
    before: dict[str, Any]
    after: dict[str, Any]


class AuditLogItem(BaseModel):
    audit_id: str
    actor_id: str | None
    action: str
    target: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime


class AuditLogListData(BaseModel):
    """审计列表的一页：行 + 总数 + 本页起点。

    ``total`` 是**当前筛选条件下**的总条数（不是全表）；前端用 ``offset + len(audit_logs)``
    与本页 ``limit`` 一起判断「还有没有下一页」，用 ``ceil(total / limit)`` 画页码。
    ``offset`` 原样回显请求里的起点：响应自解释，不必让前端自己记住上一次的入参。
    """

    audit_logs: list[AuditLogItem]
    total: int
    offset: int


# ─────────────────────────────────────────────────────────── 行 → 响应体
#
# 逐个字段点名，不用 ``model_validate(row)``：ORM 行上还有 ``_sa_instance_state`` 与
# 将来会新增的内部属性，让它们进入响应体只是把「内部结构」变成了对外契约。


def account_data(account: Account) -> AccountData:
    return AccountData(
        account_id=account.id,
        username=account.username,
        role=account.role,
        status=account.status,
        tenant_id=account.tenant_id,
    )


def tenant_data(tenant: Tenant) -> TenantData:
    return TenantData(
        tenant_id=tenant.id,
        name=tenant.name,
        status=tenant.status,
        plan_id=tenant.plan_id,
    )


def _business_values(source: object) -> dict[str, Any]:
    """套餐的全部业务列值。

    列清单来自 ``PLAN_SPEC_COLUMNS``，所以加一列时两个映射函数一起跟上，不会出现
    「API 少回一列而服务端算得出」这种半截状态。
    """

    return plan_columns_of(source)


def plan_snapshot_data(plan: PlanSpec) -> PlanSnapshotData:
    return PlanSnapshotData(**_business_values(plan))


def plan_data(plan: Plan) -> PlanData:
    return PlanData(
        plan_id=plan.id,
        updated_at=plan.updated_at,
        **_business_values(plan),
    )


def usage_data(*, used: int | None, limit: int) -> UsageData:
    """内部用量视图 → 对外响应：``None`` 走「未统计」，绝不补成 0。"""

    if used is None:
        return UsageData(
            used=None,
            limit=limit,
            source=USAGE_SOURCE_NOT_TRACKED,
            note=USAGE_NOTE_NOT_TRACKED,
        )
    return UsageData(used=used, limit=limit, source=USAGE_SOURCE_DAILY_COUNTER)


def audit_log_item(entry: AuditLog) -> AuditLogItem:
    return AuditLogItem(
        audit_id=entry.id,
        actor_id=entry.actor_id,
        action=entry.action,
        target=entry.target,
        before=entry.before_snapshot,
        after=entry.after_snapshot,
        created_at=entry.created_at,
    )
