"""票 #74：播种、回填与三条运维动作的业务口径。

守的是「库里到底落了哪些行、行的哪一列是什么」——这一票的交付判据就是「跑完命令直接
查库能看到对应行」。数据库换成 ``tests/fake_db.RecordingSession``，离线可跑。

三条口径各一条用例：新建租户默认最低档、升级前已存在租户按标准档、本地演示租户按最高档。
"""

from __future__ import annotations

import re

import pytest

from app.models import Tenant
from app.models.api_key import ApiKey
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of, preset_mismatches
from app.models.tenant import DEFAULT_PLAN_CODE
from app.services.api_key_service import issue_api_key
from app.services.identity_seeding import backfill_tenants, seed_plans
from app.services.tenant_service import change_tenant_plan, create_tenant
from app.utils.id_generator import new_api_key_value
from tests.fake_db import RecordingSession

DEMO_TENANT = "tenant_demo"
ACME_TENANT = "tenant_acme"
POLICY_ID = "11111111-1111-4111-8111-111111111111"

API_KEY_PATTERN = re.compile(r"^rc_live_[0-9a-f]{32}$")


def _plan_rows() -> list[Plan]:
    """播种后的三档行；主键由数据库自增，假会话里手工给（1=free, 2=standard, 3=pro）。"""

    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _session(*, plans: list[Plan] | None = None, tenants: list[Tenant] | None = None,
             tenant_ids: list[str] | None = None,
             policies: list[KeyPolicy] | None = None) -> RecordingSession:
    rows: dict[type, list[object]] = {}
    if plans is not None:
        rows[Plan] = list(plans)
    if tenants is not None:
        rows[Tenant] = list(tenants)
    if policies is not None:
        rows[KeyPolicy] = list(policies)
    return RecordingSession(rows=rows, tenant_ids=tenant_ids)


def _tenant_row(tenant_id: str, *, plan_id: int) -> Tenant:
    return Tenant(id=tenant_id, name=tenant_id, plan_id=plan_id)


def _policy_row(tenant_id: str = ACME_TENANT) -> KeyPolicy:
    return KeyPolicy(
        id=POLICY_ID,
        tenant_id=tenant_id,
        name="default",
        allowed_kb_ids=[],
        retrieve_qps=3,
        retrieve_daily=500,
        profile="speed",
    )


# ───────────────────────────────────────────────────────────────── 套餐播种


def test_seeding_an_empty_plans_table_writes_the_three_tiers_and_reconciles() -> None:
    session = _session()

    stats = seed_plans(session)

    written = session.added_of(Plan)
    assert stats.inserted == 3
    assert [row.code for row in written] == list(PLAN_CODES)
    assert preset_mismatches(written) == [], "播种出来的行必须与常量逐列一致"
    assert session.commits == 1


def test_seeding_does_not_overwrite_a_table_that_already_has_rows() -> None:
    """运营改过的额度不能被下一次启动悄悄改回去：表非空就整表跳过。"""

    existing = _plan_rows()[0]
    existing.retrieve_daily = 999  # 运营手工提过额度
    session = _session(plans=[existing])

    stats = seed_plans(session)

    assert stats.inserted == 0
    assert session.added == []
    assert session.commits == 0
    assert existing.retrieve_daily == 999


# ─────────────────────────────────────────────────────────── 升级前租户的回填


def test_existing_tenants_get_standard_and_the_demo_tenant_gets_the_top_tier() -> None:
    session = _session(plans=_plan_rows(), tenant_ids=[DEMO_TENANT, ACME_TENANT])

    stats = backfill_tenants(session, demo_tenant_id=DEMO_TENANT)

    created = {row.id: row for row in session.added_of(Tenant)}
    assert created[DEMO_TENANT].plan_id == PLAN_CODES.index("pro") + 1
    assert created[ACME_TENANT].plan_id == PLAN_CODES.index("standard") + 1
    assert (stats.considered, stats.created, stats.kept) == (2, 2, 0)
    assert session.commits == 1


def test_backfill_keeps_a_tenant_that_already_has_a_row() -> None:
    """回填是幂等的：脚本跑第二遍不该把升级后改过的套餐又抹回标准档。"""

    already = _tenant_row(ACME_TENANT, plan_id=1)  # 已经被改成免费档
    session = _session(plans=_plan_rows(), tenants=[already], tenant_ids=[ACME_TENANT])

    stats = backfill_tenants(session, demo_tenant_id=DEMO_TENANT)

    assert session.added_of(Tenant) == []
    assert already.plan_id == 1
    assert (stats.considered, stats.created, stats.kept) == (1, 0, 1)


def test_backfill_writes_only_tenant_rows() -> None:
    """既有四张业务表只读：播种与回填不往里写任何东西。"""

    session = _session(plans=_plan_rows(), tenant_ids=[ACME_TENANT])

    backfill_tenants(session, demo_tenant_id=DEMO_TENANT)

    assert len(session.added) == len(session.added_of(Tenant))


def test_backfill_fails_loudly_when_the_tier_was_never_seeded() -> None:
    session = _session(tenant_ids=[ACME_TENANT])

    with pytest.raises(LookupError, match="seed_plans"):
        backfill_tenants(session, demo_tenant_id=DEMO_TENANT)

    assert session.commits == 0, "缺档位时不能提交出半批租户"


# ───────────────────────────────────────────────────────────────── 开租户


def test_a_new_tenant_defaults_to_the_lowest_tier() -> None:
    session = _session(plans=_plan_rows())

    created = create_tenant(session, tenant_id=ACME_TENANT, name="Acme 公司")

    assert DEFAULT_PLAN_CODE == "free"
    assert created.tenant.plan_id == PLAN_CODES.index(DEFAULT_PLAN_CODE) + 1
    assert created.tenant.name == "Acme 公司"
    assert session.commits == 1


def test_a_new_tenant_gets_a_key_policy_so_a_key_can_be_issued_right_away() -> None:
    """没有配置就发不出 Key：开租户时顺手建一套最小配置（能力全关、上限按套餐）。"""

    session = _session(plans=_plan_rows())

    created = create_tenant(session, tenant_id=ACME_TENANT, name="Acme 公司", kb_ids=["kb-1"])

    policy = created.policy
    assert session.added_of(KeyPolicy) == [policy]
    assert policy.tenant_id == ACME_TENANT
    assert policy.allowed_kb_ids == ["kb-1"]
    assert policy.profile == "speed"
    assert (policy.retrieve_qps, policy.retrieve_daily) == (3, 500)
    assert not any(
        [
            policy.research_allowed,
            policy.hybrid_allowed,
            policy.rerank_allowed,
            policy.query_rewrite_allowed,
        ]
    ), "新配置不授予任何能力：重排与改写是最贵的两项"


def test_a_new_tenant_can_take_a_named_tier() -> None:
    session = _session(plans=_plan_rows())

    created = create_tenant(session, tenant_id=ACME_TENANT, name="Acme", plan_code="pro")

    assert created.tenant.plan_id == PLAN_CODES.index("pro") + 1
    assert created.policy.profile == "speed", "档位白名单的第一档是 speed，与套餐无关"


def test_creating_a_tenant_with_an_unknown_tier_fails_loudly() -> None:
    session = _session(plans=_plan_rows())

    with pytest.raises(ValueError, match="free/standard/pro"):
        create_tenant(session, tenant_id=ACME_TENANT, name="Acme", plan_code="platinum")

    assert session.added == []


def test_creating_a_tenant_twice_fails_loudly() -> None:
    session = _session(plans=_plan_rows(), tenants=[_tenant_row(ACME_TENANT, plan_id=1)])

    with pytest.raises(ValueError, match="已存在"):
        create_tenant(session, tenant_id=ACME_TENANT, name="Acme")

    assert session.added == []


# ───────────────────────────────────────────────────────────────── 改套餐


def test_changing_the_plan_moves_the_tenant_to_the_new_tier() -> None:
    tenant = _tenant_row(ACME_TENANT, plan_id=1)
    session = _session(plans=_plan_rows(), tenants=[tenant])

    change = change_tenant_plan(session, tenant_id=ACME_TENANT, plan_code="pro")

    assert (change.from_code, change.to_code) == ("free", "pro")
    assert tenant.plan_id == PLAN_CODES.index("pro") + 1
    assert session.commits == 1


@pytest.mark.parametrize("plan_code", ["platinum", "Free", "", " standard"])
def test_changing_to_an_unknown_tier_fails_loudly(plan_code: str) -> None:
    """只认三个已知档位码：大小写与空串都不给兜底。"""

    tenant = _tenant_row(ACME_TENANT, plan_id=1)
    session = _session(plans=_plan_rows(), tenants=[tenant])

    with pytest.raises(ValueError, match="未知套餐档位"):
        change_tenant_plan(session, tenant_id=ACME_TENANT, plan_code=plan_code)

    assert tenant.plan_id == 1, "非法档位不能改到半个状态"


def test_changing_the_plan_of_a_missing_tenant_fails_loudly() -> None:
    session = _session(plans=_plan_rows())

    with pytest.raises(LookupError, match="租户不存在"):
        change_tenant_plan(session, tenant_id="tenant_ghost", plan_code="pro")


def test_changing_the_plan_to_a_tier_that_was_not_seeded_fails_loudly() -> None:
    tenant = _tenant_row(ACME_TENANT, plan_id=1)
    session = _session(plans=_plan_rows()[:2], tenants=[tenant])  # 只有 free / standard

    with pytest.raises(LookupError, match="seed_plans"):
        change_tenant_plan(session, tenant_id=ACME_TENANT, plan_code="pro")

    assert tenant.plan_id == 1


# ───────────────────────────────────────────────────────────────── 发 Key


def test_issuing_a_key_binds_it_to_the_policy_and_generates_a_live_value() -> None:
    session = _session(
        plans=_plan_rows(),
        tenants=[_tenant_row(ACME_TENANT, plan_id=1)],
        policies=[_policy_row()],
    )

    key = issue_api_key(
        session, tenant_id=ACME_TENANT, policy_id=POLICY_ID, label="订单机器人"
    )

    assert isinstance(key, ApiKey)
    assert API_KEY_PATTERN.fullmatch(key.key_value), key.key_value
    assert (key.tenant_id, key.policy_id, key.label) == (ACME_TENANT, POLICY_ID, "订单机器人")
    assert session.added_of(ApiKey) == [key]
    assert session.commits == 1


def test_issuing_a_key_with_a_policy_from_another_tenant_is_refused() -> None:
    session = _session(
        plans=_plan_rows(),
        tenants=[_tenant_row(ACME_TENANT, plan_id=1)],
        policies=[_policy_row(tenant_id="tenant_other")],
    )

    with pytest.raises(ValueError, match="不属于租户"):
        issue_api_key(session, tenant_id=ACME_TENANT, policy_id=POLICY_ID)

    assert session.added == []


@pytest.mark.parametrize(
    ("tenant_id", "policy_id", "message"),
    [
        ("tenant_ghost", POLICY_ID, "租户不存在"),
        (ACME_TENANT, "99999999-9999-4999-8999-999999999999", "密钥配置不存在"),
    ],
)
def test_issuing_a_key_with_missing_pieces_fails_loudly(
    tenant_id: str, policy_id: str, message: str
) -> None:
    session = _session(
        plans=_plan_rows(),
        tenants=[_tenant_row(ACME_TENANT, plan_id=1)],
        policies=[_policy_row()],
    )

    with pytest.raises(LookupError, match=message):
        issue_api_key(session, tenant_id=tenant_id, policy_id=policy_id)


def test_generated_key_values_are_live_prefixed_and_distinct() -> None:
    values = {new_api_key_value() for _ in range(200)}

    assert len(values) == 200
    assert all(API_KEY_PATTERN.fullmatch(value) for value in values)
