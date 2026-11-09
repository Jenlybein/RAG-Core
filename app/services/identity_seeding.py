"""播种与回填：把「套餐三档」和「升级前就存在的租户」写进库里。

两件事分开：

- :func:`seed_plans` 只在 ``plans`` 表**为空**时写入三档种子。表里已经有行就整表跳过——
  运营在面板上提过的额度不能被一次重启悄悄改回常量值，这是「表即真相」的落地方式。
- :func:`backfill_tenants` 处理升级路径：``tenants`` 表刚建出来时是空的，而四张业务表上
  早就写着租户标识。它们此前是「全套能力都能用」，所以按标准档建档；本地演示租户按最高档。
  这条回填**只读**四张业务表，一个字都不改它们的行。

新建租户走的是另一条口径（最低档），那条规则在 ``app.models.tenant`` 里，与这里的两条
常量并排放着——三条口径写在同一处，才不会出现「脚本开的租户和升级回来的租户不是同一档」。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.plan import PLAN_CODES, PLAN_PRESETS, plan_row_of
from app.models.tenant import (
    DEMO_TENANT_PLAN_CODE,
    EXISTING_TENANT_PLAN_CODE,
    Tenant,
)
from app.repositories import plan_repository, tenant_repository


@dataclass(frozen=True)
class PlanSeedStats:
    """一次套餐播种的结果；``skipped`` 是「表非空所以没播种的档位数」。"""

    inserted: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已投递」口径。"""

        return self.inserted


@dataclass(frozen=True)
class TenantBackfillStats:
    """一次租户回填的结果；``kept`` 是「已经有租户行、没动它」的数量。"""

    considered: int = 0
    created: int = 0
    kept: int = 0
    failed: int = 0

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已投递」口径。"""

        return self.created


def seed_plans(db: Session) -> PlanSeedStats:
    """表为空时写入三档套餐种子；否则整表跳过。

    返回值里的 ``skipped`` 用「计划写入的档位数」而不是「表里现有行数」：脚本的报告
    语义是「该播三档，播了没」，不是「库里现在有几行」。
    """

    if plan_repository.count(db):
        return PlanSeedStats(inserted=0, skipped=len(PLAN_PRESETS))

    for code in PLAN_CODES:
        plan_repository.create(db, plan_row_of(PLAN_PRESETS[code]))
    db.commit()
    return PlanSeedStats(inserted=len(PLAN_CODES))


def backfill_tenants(
    db: Session, *, demo_tenant_id: str | None = None
) -> TenantBackfillStats:
    """给旧租户建档并按口径分档；已经有租户行的不动。

    ``demo_tenant_id`` 缺省取服务端配置里的开发租户（``DEV_TENANT_ID``）。做成入参是为了
    让这条回填在没有配置的环境里也能测——判定口径本身不该依赖环境变量。
    """

    demo = demo_tenant_id if demo_tenant_id is not None else settings.DEV_TENANT_ID
    tenant_ids = tenant_repository.existing_tenant_ids(db)
    plan_ids: dict[str, int] = {}
    created = 0
    kept = 0

    for tenant_id in tenant_ids:
        if tenant_repository.get_by_id(db, tenant_id) is not None:
            kept += 1
            continue

        code = DEMO_TENANT_PLAN_CODE if tenant_id == demo else EXISTING_TENANT_PLAN_CODE
        if code not in plan_ids:
            plan = plan_repository.get_by_code(db, code)
            if plan is None:
                # 缺档位时立刻停手、不提交：半批「有租户没套餐」比一行都没建更难收拾。
                raise LookupError(
                    f"套餐 {code} 还没播种：先跑 scripts/seed_plans.py 再回填租户"
                )
            plan_ids[code] = plan.id

        tenant_repository.create(
            db,
            # 升级时不知道客户的真名，先拿标识顶上；名字是展示字段，运营随时能改。
            Tenant(id=tenant_id, name=tenant_id, plan_id=plan_ids[code]),
        )
        created += 1

    db.commit()
    return TenantBackfillStats(considered=len(tenant_ids), created=created, kept=kept)
