"""改套餐的服务端：校验、影响面试算、落库用的快照。

**这里是仓库里唯一一处会改 ``plans`` 行的地方**（``apply_patch`` / 删除只从这里走）。留
第二个「顺手 UPDATE 一下」的入口，就等于留了一条不试算、不审计、不删缓存的路——而它平时
看起来完全正常，只在出事那天才被发现少做了什么。

三件事分开写，各有各的理由：

- :func:`validate_patch`：**档位必须是已知枚举**、**数值上限必须 ≥ 1**（ADR-0017 的五道护栏
  之二）。校验放在服务层而不是 pydantic 的 ``ge=1`` 上：写在 schema 里时，``0`` 会先被
  判成「参数结构错误」（20002），而票面要求的是「套餐非法」（20018）——同一个手滑，两个
  错误码，运维分不清该找谁；
- :func:`preview_impact`：只读，回答「这次改动会让哪几个账号当场超限、哪几个会失去档位」；
- :func:`ensure_deletable`：被任何租户引用的套餐不许删（``20019``）。

影响面按**租户**算而不是按账号行算：库与密钥配置都挂在租户上，一个租户下有几个账号并不
改变「它有 4 个库、而新上限是 2」这件事。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import PROFILES
from app.models.plan import PLAN_BUSINESS_COLUMNS, PLAN_NUMERIC_COLUMNS, Plan
from app.repositories import (
    key_policy_repository,
    knowledge_base_repository,
    tenant_repository,
)

#: 影响面里「名字最多列几个」。计数永远是全量，名字只是给人快速扫一眼的样本——
#: 一份 80 行的名单在面板里没人看，而「80 个」这个数字恰恰是决策依据。
IMPACT_SAMPLE_LIMIT: Final[int] = 5


@dataclass(frozen=True)
class ImpactGroup:
    """一组受影响的对象：**全量计数** + 若干个名字。"""

    count: int
    sample: tuple[str, ...]


@dataclass(frozen=True)
class PlanImpact:
    """一次套餐改动的影响面（只读试算的结果）。"""

    #: 这个套餐下被看过的租户数：有了它，「0 受影响」才和「没算」区分得开。
    tenants_checked: int
    #: 新上限之下库数会超的租户。
    over_max_kb: ImpactGroup
    #: 新档位白名单里没有它们当前档位的租户。
    lose_profile: ImpactGroup
    #: 会失去档位的密钥配置条数（一个租户可能有多套配置）。
    policies_losing_profile: int


def plan_snapshot(row: Plan) -> dict[str, Any]:
    """一行套餐 → 审计/预览用的快照：主键、两个展示列、11 个业务列。

    逐列点名而不是 ``row.__dict__``：后者会把 ``_sa_instance_state`` 这类内部状态一起
    写进审计的 JSON 里，而审计要能被人读，也要能被另一段代码安全地读回来。
    """

    snapshot: dict[str, Any] = {"plan_id": row.id, "code": row.code, "name": row.name}
    for column in PLAN_BUSINESS_COLUMNS:
        value = getattr(row, column)
        snapshot[column] = list(value) if column == "allowed_profiles" else value
    return snapshot


def validate_patch(patch: Mapping[str, Any]) -> None:
    """校验这次要写的字段；任何一项不合格都是 ``20018``，且**一项都还没写**。

    ``allowed_profiles`` 为空列表同样拒绝：一份谁都不能用的档位白名单，等于把这个套餐下
    所有 Key 当场停用——与「QPS 填 0」是同一类事故（ADR-0017 护栏表最后一行）。
    """

    if not patch:
        raise AppError(ErrorCode.PARAM_ERROR, "no fields to change")

    profiles = patch.get("allowed_profiles")
    if profiles is not None:
        known = [profile for profile in profiles if profile in PROFILES]
        if len(known) != len(profiles):
            unknown = [profile for profile in profiles if profile not in PROFILES]
            raise AppError(
                ErrorCode.PLAN_INVALID,
                f"unknown profile: {unknown[0]}（可选：{'/'.join(PROFILES)}）",
            )
        if not profiles:
            raise AppError(ErrorCode.PLAN_INVALID, "allowed_profiles must not be empty")

    for field in PLAN_NUMERIC_COLUMNS:
        value = patch.get(field)
        if value is not None and value < 1:
            # 「0 或负数」不是一种更严格的限制，而是一次事故：QPS=0 等于当场停服。
            raise AppError(ErrorCode.PLAN_INVALID, f"{field} must be at least 1")


def merged_snapshot(before: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """改前快照 + 这次要写的字段 = 改后快照。试算与实际落库看的是同一份结果。"""

    merged = dict(before)
    for field, value in patch.items():
        merged[field] = list(value) if field == "allowed_profiles" else value
    return merged


def apply_patch(row: Plan, patch: Mapping[str, Any]) -> None:
    """把已经校验过的字段写进行；**不 commit**，事务由调用方收口。"""

    for field, value in patch.items():
        setattr(row, field, list(value) if field == "allowed_profiles" else value)


def ensure_deletable(db: Session, *, plan_id: int) -> None:
    """被任何租户引用的套餐不许删（``20019``）：删了就是把客户挂空。

    与「可改」是配套的：改是调额度，删是让客户失去定义，所以对同一份数据改开、删关。
    """

    tenants = tenant_repository.list_by_plan_id(db, plan_id)
    if tenants:
        raise AppError(
            ErrorCode.PLAN_IN_USE,
            f"plan in use by {len(tenants)} tenant(s)",
        )


def preview_impact(
    db: Session,
    *,
    plan_id: int,
    max_kb: int,
    allowed_profiles: Sequence[str],
) -> PlanImpact:
    """只读试算：这个套餐下的租户里，谁会当场超库数、谁会失去档位。

    两件事都按**改动后的值**算：

    - 超库数：租户现有库数 > 新的 ``max_kb``。注意它与「新建第 N+1 个库被拒」直接对应，
      所以预览说的就是「确认保存那一刻会发生什么」，不是某种估算；
    - 失去档位：租户现有密钥配置里 ``profile`` 不在新的白名单内。这些配置的 Key 下一
      次调用会被档位门禁拒绝（``20013``）——预览把它们提前点出来，管理员可以先改配置
      再降套餐，而不是等客户打电话。
    """

    allowed = set(allowed_profiles)
    over_names: list[str] = []
    losing_names: list[str] = []
    policies_losing = 0
    tenants = tenant_repository.list_by_plan_id(db, plan_id)

    for tenant in tenants:
        name = tenant.name or tenant.id
        if knowledge_base_repository.count_by_tenant(db, tenant.id) > max_kb:
            over_names.append(name)
        losing = [
            policy
            for policy in key_policy_repository.list_by_tenant(db, tenant.id)
            if policy.profile not in allowed
        ]
        if losing:
            losing_names.append(name)
            policies_losing += len(losing)

    return PlanImpact(
        tenants_checked=len(tenants),
        over_max_kb=ImpactGroup(count=len(over_names), sample=tuple(over_names[:IMPACT_SAMPLE_LIMIT])),
        lose_profile=ImpactGroup(
            count=len(losing_names), sample=tuple(losing_names[:IMPACT_SAMPLE_LIMIT])
        ),
        policies_losing_profile=policies_losing,
    )
