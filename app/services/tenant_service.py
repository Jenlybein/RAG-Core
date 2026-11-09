"""租户业务：开租户、改套餐。

两件事都必须**一次事务落定**：开租户时同时建一套最小的密钥配置（否则「开完租户发不出
Key」），改套餐时只动 ``plan_id`` 一列。「套餐三档」的口径在 ``app.models.tenant``：
新建默认最低档、改套餐只认三个已知码。

这里的错误分三类，脚本与将来的控制面都按这三类处理：

- ``ValueError``：入参本身不对（档位名拼错、租户标识重复）；
- ``LookupError``：库里的前置事实缺失（租户不存在、档位还没播种）；
- ``AppError``：配置**不自洽**（``20015``）——档位要的能力配置没开、配置开的比套餐允许的多、
  或数值超过套餐上限。它带着错误码，将来的控制面可以直接翻成响应；脚本只打消息。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.key_policy import KeyPolicy, policy_snapshot_of
from app.models.plan import Plan, check_plan_code, plan_spec_of
from app.models.tenant import Tenant, resolve_plan_code
from app.repositories import key_policy_repository, plan_repository, tenant_repository
from app.services.tier_policy import ensure_policy_consistent
from app.utils.id_generator import new_id

#: 开租户时顺手建的那套配置的名字。租户内可以再建别的，所以它只是一个默认值。
DEFAULT_POLICY_NAME = "default"


@dataclass(frozen=True)
class CreatedTenant:
    """一次开租户的产物：租户行 + 可以立刻拿去发 Key 的配置。"""

    tenant: Tenant
    policy: KeyPolicy


@dataclass(frozen=True)
class PlanChange:
    """一次改套餐的前后档位码，供脚本报告「从哪一档改到哪一档」。"""

    tenant_id: str
    from_code: str
    to_code: str


def create_tenant(
    db: Session,
    *,
    tenant_id: str,
    name: str,
    plan_code: str | None = None,
    kb_ids: Sequence[str] = (),
) -> CreatedTenant:
    """开一个租户：按口径分档，顺手建一套最小配置。

    ``kb_ids`` 是给这套默认配置圈的数据范围；空列表表示「还没圈定」，不是「全部」。
    """

    if not tenant_id.strip():
        raise ValueError("租户标识不能为空")
    code = resolve_plan_code(plan_code)
    if tenant_repository.get_by_id(db, tenant_id) is not None:
        raise ValueError(f"租户已存在：{tenant_id}")

    plan = _require_plan(db, code)
    row = _default_policy(tenant_id=tenant_id, plan=plan, kb_ids=kb_ids)
    # 配置时自洽校验（ADR-0018 第 2 条）：档位所需能力 ⊆ 配置开关 ⊆ 套餐能力，数值也不得
    # 超限。判在**落库之前**：不自洽的配置一旦进库，第一次调用就会失败，而那时改的是
    # 客户的生产配置；创建这里失败只是一次脚本 / 面板操作，有人盯着，改一下就好。
    ensure_policy_consistent(policy=policy_snapshot_of(row), plan=plan_spec_of(plan))
    tenant = tenant_repository.create(
        db, Tenant(id=tenant_id, name=name, plan_id=plan.id)
    )
    policy = key_policy_repository.create(db, row)
    db.commit()
    return CreatedTenant(tenant=tenant, policy=policy)


def change_tenant_plan(db: Session, *, tenant_id: str, plan_code: str) -> PlanChange:
    """把租户换到另一个已播种的档位。

    档位名先校验再查库：入参错就是入参错，不该先连一次数据库才报「未知档位」。
    """

    code = check_plan_code(plan_code)
    tenant = tenant_repository.get_by_id(db, tenant_id)
    if tenant is None:
        raise LookupError(f"租户不存在：{tenant_id}")
    plan = _require_plan(db, code)

    before = plan_repository.get_by_id(db, tenant.plan_id)
    tenant.plan_id = plan.id
    db.commit()
    return PlanChange(
        tenant_id=tenant_id,
        from_code=before.code if before is not None else "",
        to_code=code,
    )


def _require_plan(db: Session, code: str) -> Plan:
    plan = plan_repository.get_by_code(db, code)
    if plan is None:
        raise LookupError(f"套餐 {code} 还没播种：先跑 scripts/seed_plans.py")
    return plan


def _default_policy(*, tenant_id: str, plan: Plan, kb_ids: Sequence[str]) -> KeyPolicy:
    """最小权限的默认配置：能力全关、上限照抄套餐、档位取白名单第一档。

    能力开关**显式写 False** 而不是留给列默认值：列默认值哪天被改成 ``True``，
    这里发出去的 Key 就自带重排与改写，而签发脚本不会有任何异常。
    """

    spec = plan_spec_of(plan)
    return KeyPolicy(
        id=new_id(),
        tenant_id=tenant_id,
        name=DEFAULT_POLICY_NAME,
        allowed_kb_ids=list(kb_ids),
        research_allowed=False,
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=spec.retrieve_qps,
        retrieve_daily=spec.retrieve_daily,
        profile=spec.allowed_profiles[0],
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )
