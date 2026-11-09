"""票 #75：判定内核（唯一的「这次是谁」）与开发身份。

接缝是**纯函数**：``app.core.auth.resolve_identity`` 收一个「有 ``headers`` 的请求对象」
加一个数据库会话，直接调用、直接断言返回的上下文或抛出的业务失败。不经过任何 Web 框架
——这正是这张票存在的理由：HTTP 与 MCP 各自只剩一层薄适配器，规则只有这一份。

数据库换成 ``tests/fake_db.RecordingSession``（既有假会话助手），套餐缓存换成模块内的
内存假实现：默认套件不碰 Redis、不碰 PostgreSQL。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.api_key import ApiKey
from app.models.key_policy import PROFILE_BALANCED, PROFILE_SPEED, KeyPolicy
from app.models.plan import PLAN_FREE, PLAN_PRO, PLAN_STANDARD, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from tests.fake_db import RecordingSession

TENANT_ID = "tenant_acme"
OTHER_TENANT_ID = "tenant_other"
POLICY_ID = "22222222-2222-4222-8222-222222222222"
KEY_ID = "33333333-3333-4333-8333-333333333333"
KEY_VALUE = "rc_live_0123456789abcdef0123456789abcdef"
PLAN_IDS = {PLAN_FREE: 1, PLAN_STANDARD: 2, PLAN_PRO: 3}


class _Request:
    """最小请求替身：内核只认 ``headers``，不认任何框架类型。"""

    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {} if authorization is None else {"authorization": authorization}


def _plan_rows() -> list[Plan]:
    """三档套餐行；主键在假会话里手工给（与 test_identity_seeding 的约定一致）。"""

    rows: list[Plan] = []
    for code, plan_id in PLAN_IDS.items():
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = plan_id
        rows.append(row)
    return rows


def _tenant_row(
    tenant_id: str = TENANT_ID, *, plan_id: int = PLAN_IDS[PLAN_PRO], status: str = "active"
) -> Tenant:
    return Tenant(id=tenant_id, name="Acme 公司", plan_id=plan_id, status=status)


def _policy_row(
    *,
    tenant_id: str = TENANT_ID,
    profile: str = PROFILE_BALANCED,
    include_parent: bool = False,
    expand_table_rows: bool = True,
) -> KeyPolicy:
    return KeyPolicy(
        id=POLICY_ID,
        tenant_id=tenant_id,
        name="console",
        allowed_kb_ids=["kb-1"],
        research_allowed=True,
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=7,
        retrieve_daily=70,
        profile=profile,
        include_parent=include_parent,
        expand_table_rows=expand_table_rows,
        custom_options={"top_k": 3},
    )


def _key_row(
    key_value: str = KEY_VALUE,
    *,
    tenant_id: str = TENANT_ID,
    status: str = "active",
) -> ApiKey:
    return ApiKey(
        id=KEY_ID,
        tenant_id=tenant_id,
        policy_id=POLICY_ID,
        key_value=key_value,
        label="控制台调试",
        status=status,
    )


def _session(
    *,
    keys: list[ApiKey] | None = None,
    tenants: list[Tenant] | None = None,
    policies: list[KeyPolicy] | None = None,
    plans: list[Plan] | None = None,
) -> RecordingSession:
    rows: dict[type, list[Any]] = {Plan: _plan_rows() if plans is None else list(plans)}
    if keys is not None:
        rows[ApiKey] = list(keys)
    if tenants is not None:
        rows[Tenant] = list(tenants)
    if policies is not None:
        rows[KeyPolicy] = list(policies)
    return RecordingSession(rows=rows)


class _MemoryPlanCache:
    """内存假缓存：``get/set/delete`` 三个方法就是全部接缝。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}
        self.set_calls: list[int] = []
        self.deleted: list[int] = []

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.set_calls.append(plan_id)
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.deleted.append(plan_id)
        self.items.pop(plan_id, None)


def _dev_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {settings.DEV_API_KEY}"}


# ───────────────────────────────────────────── 上下文对象：15 处既有构造点的兼容


def test_context_is_constructible_from_a_tenant_id_alone() -> None:
    """15 个既有测试只传 ``tenant_id``：上下文必须能只用一个标识造出来。

    这些构造点之所以不能改，是因为 ``dependency_overrides`` 认的是可调用对象本身；
    名单一改，测试就在无提示的情况下退化成「真的走了鉴权」。
    """

    from app.core.auth import DEV_KEY_PLAN, DEV_KEY_POLICY, DevKeyContext, KeyContext

    context = DevKeyContext(tenant_id=TENANT_ID)

    assert DevKeyContext is KeyContext
    assert context.tenant_id == TENANT_ID
    assert context.policy == DEV_KEY_POLICY
    assert context.plan == DEV_KEY_PLAN


def test_development_snapshot_is_a_full_pro_policy_at_a_conservative_profile() -> None:
    """开发身份的配置=「全能力 + 保守命名档位 + 两种形状都开」，套餐=最高档。

    能力全开是为了「开发身份不因配置层的开关改变现行为」：重排 / 改写是否真的跑，
    仍由服务端配置决定。档位取保守的那一档，则是因为命名档位是**完整替换**——挂在
    ``quality`` 上会让开发身份凭空多出重排与改写，本地迭代立刻偏离现行为。
    """

    from app.core.auth import DEV_KEY_POLICY

    assert DEV_KEY_POLICY.profile == PROFILE_SPEED
    assert (
        DEV_KEY_POLICY.research_allowed,
        DEV_KEY_POLICY.hybrid_allowed,
        DEV_KEY_POLICY.rerank_allowed,
        DEV_KEY_POLICY.query_rewrite_allowed,
    ) == (True, True, True, True)
    assert DEV_KEY_POLICY.include_parent is True
    assert DEV_KEY_POLICY.expand_table_rows is True
    assert DEV_KEY_POLICY.retrieve_qps == PLAN_PRESETS[PLAN_PRO].retrieve_qps
    assert DEV_KEY_POLICY.retrieve_daily == PLAN_PRESETS[PLAN_PRO].retrieve_daily


# ───────────────────────────────────────────── 开发开关：关着也不许裸奔


def test_dev_mode_still_requires_the_placeholder_token() -> None:
    """开关关闭时也不要 Key？不行：占位 token 仍然要带。

    退回「谁都不用带」意味着一个被意外暴露的开发实例对整个网络敞开；而要求这把固定
    token 对开发者零成本（它本来就写在 ``.env`` 里），还让既有测试保持有意义。
    """

    from app.core.auth import resolve_identity

    missing = _Request()
    with pytest.raises(AppError) as missing_exc:
        resolve_identity(missing, None, auth_enabled=False)
    assert missing_exc.value.code is ErrorCode.UNAUTHORIZED

    wrong = _Request("Bearer not-the-dev-key")
    with pytest.raises(AppError) as wrong_exc:
        resolve_identity(wrong, None, auth_enabled=False)
    assert wrong_exc.value.code is ErrorCode.UNAUTHORIZED


def test_dev_mode_returns_the_built_in_identity_without_touching_the_database() -> None:
    """开关关闭：占位 token 直接换成内置开发身份，连一次查询都不发。"""

    from app.core.auth import KeyContext, resolve_identity

    class _ExplodingSession:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"开发身份不该碰数据库：{name}")

    context = resolve_identity(_Request(_dev_headers()["authorization"]), _ExplodingSession())

    assert isinstance(context, KeyContext)
    assert context.tenant_id == settings.DEV_TENANT_ID
    assert context.key_label == "内置开发身份"


# ───────────────────────────────────────────── 开关打开：四种未授权，同一个码


def _failures(*, session: RecordingSession) -> dict[str, AppError]:
    """跑出四种未授权情形，按情形命名返回异常，供「同一个码、不同消息」比对。"""

    from app.core.auth import resolve_identity

    cases = {
        "missing": _Request(None),
        "unknown": _Request(f"Bearer {KEY_VALUE}"),
        "key_disabled": _Request(f"Bearer {KEY_VALUE}"),
        "tenant_disabled": _Request(f"Bearer {KEY_VALUE}"),
    }
    sessions = {
        "missing": session,
        "unknown": _session(keys=[], tenants=[_tenant_row()], policies=[_policy_row()]),
        "key_disabled": _session(
            keys=[_key_row(status="disabled")],
            tenants=[_tenant_row()],
            policies=[_policy_row()],
        ),
        "tenant_disabled": _session(
            keys=[_key_row()],
            tenants=[_tenant_row(status="disabled")],
            policies=[_policy_row()],
        ),
    }

    caught: dict[str, AppError] = {}
    for name, request in cases.items():
        with pytest.raises(AppError) as excinfo:
            resolve_identity(request, sessions[name], auth_enabled=True)
        caught[name] = excinfo.value
    return caught


def test_switch_on_rejects_a_missing_token() -> None:
    from app.core.auth import resolve_identity

    with pytest.raises(AppError) as excinfo:
        resolve_identity(_Request(None), _session(), auth_enabled=True)

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


def test_switch_on_rejects_an_unknown_token() -> None:
    from app.core.auth import resolve_identity

    with pytest.raises(AppError) as excinfo:
        resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            _session(keys=[], tenants=[_tenant_row()], policies=[_policy_row()]),
            auth_enabled=True,
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


def test_all_four_unauthorized_cases_share_the_code_and_stay_distinguishable() -> None:
    """四种情形**同一个**错误码，消息可区分——交付判据就是这么写的。"""

    caught = _failures(session=_session(keys=[_key_row()], tenants=[_tenant_row()]))

    assert {exc.code for exc in caught.values()} == {ErrorCode.UNAUTHORIZED}
    messages = {name: exc.msg for name, exc in caught.items()}
    assert len(set(messages.values())) == 4, f"四种情形必须能分辨，实际是 {messages}"


def test_the_unauthorized_error_is_a_business_failure_not_a_401() -> None:
    """内核抛的是业务异常，不是 HTTP 异常：能不能变 401 由入口适配器决定（ADR-0007）。"""

    caught = _failures(session=_session(keys=[_key_row()], tenants=[_tenant_row()]))

    for exc in caught.values():
        assert isinstance(exc, AppError)
        assert not hasattr(exc, "status_code")


def test_switch_on_makes_the_development_identity_unreachable() -> None:
    """开关打开后，占位 token 不再是身份来源：它不在 api_keys 表里，一律未授权。

    这条与「关闭时仍要求占位 token」是同一枚硬币的两面：开发身份只存在于开关关闭的
    那条分支，打开后连它自己都得凭真 Key 说话。
    """

    from app.core.auth import resolve_identity

    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()])

    with pytest.raises(AppError) as excinfo:
        resolve_identity(_Request(_dev_headers()["authorization"]), session, auth_enabled=True)

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


# ───────────────────────────────────────────── 开关打开：真 Key 解出完整上下文


def test_an_active_key_resolves_to_its_tenant_policy_and_plan() -> None:
    """一把能用的 Key 解出租户、Key 备注名、配置快照、套餐快照四样东西。"""

    from app.core.auth import KeyContext, resolve_identity
    from app.models.key_policy import KeyPolicySnapshot

    cache = _MemoryPlanCache()
    context = resolve_identity(
        _Request(f"Bearer {KEY_VALUE}"),
        _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()]),
        auth_enabled=True,
        plan_cache=cache,
    )

    assert isinstance(context, KeyContext)
    assert (context.tenant_id, context.tenant_name) == (TENANT_ID, "Acme 公司")
    assert (context.key_id, context.key_label) == (KEY_ID, "控制台调试")
    # 期望值逐字段抄自假行（独立来源），不是把映射函数再算一遍。
    assert context.policy == KeyPolicySnapshot(
        allowed_kb_ids=("kb-1",),
        research_allowed=True,
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=7,
        retrieve_daily=70,
        profile=PROFILE_BALANCED,
        include_parent=False,
        expand_table_rows=True,
        custom_options=(("top_k", 3),),
    )
    assert context.plan == PLAN_PRESETS[PLAN_PRO]
    assert cache.set_calls == [PLAN_IDS[PLAN_PRO]], "套餐缓存的键必须是 plan id"


def test_disabling_a_tenant_takes_effect_on_the_next_call() -> None:
    """停用租户立刻生效：判定每次都重新读状态，身份不做长缓存。"""

    from app.core.auth import resolve_identity

    tenant = _tenant_row()
    session = _session(keys=[_key_row()], tenants=[tenant], policies=[_policy_row()])
    cache = _MemoryPlanCache()

    first = resolve_identity(
        _Request(f"Bearer {KEY_VALUE}"), session, auth_enabled=True, plan_cache=cache
    )
    assert first.tenant_id == TENANT_ID

    tenant.status = "disabled"

    with pytest.raises(AppError) as excinfo:
        resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"), session, auth_enabled=True, plan_cache=cache
        )
    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


def test_a_key_pointing_at_a_deleted_policy_is_unauthorized() -> None:
    """删掉密钥配置 → 引用它的 Key 级联失效（ADR-0018），判定上等同于停用。"""

    from app.core.auth import resolve_identity

    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[])

    with pytest.raises(AppError) as excinfo:
        resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            session,
            auth_enabled=True,
            plan_cache=_MemoryPlanCache(),
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


# ───────────────────────────────────────────── 套餐解析：按 plan id 缓存


def test_resolve_plan_reads_the_row_on_a_miss_and_caches_it() -> None:
    from app.services.plan_service import resolve_plan

    cache = _MemoryPlanCache()

    plan = resolve_plan(_session(), PLAN_IDS[PLAN_STANDARD], cache=cache)

    assert plan == PLAN_PRESETS[PLAN_STANDARD]
    assert cache.set_calls == [PLAN_IDS[PLAN_STANDARD]]


def test_resolve_plan_serves_from_the_cache_without_touching_the_database() -> None:
    from app.services.plan_service import resolve_plan

    class _ExplodingSession:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"命中缓存就不该读库：{name}")

    cache = _MemoryPlanCache()
    cache.items[PLAN_IDS[PLAN_PRO]] = PLAN_PRESETS[PLAN_PRO]

    plan = resolve_plan(_ExplodingSession(), PLAN_IDS[PLAN_PRO], cache=cache)

    assert plan == PLAN_PRESETS[PLAN_PRO]
    assert cache.set_calls == []


def test_resolve_plan_reports_a_missing_plan_as_plan_not_found() -> None:
    from app.services.plan_service import resolve_plan

    with pytest.raises(AppError) as excinfo:
        resolve_plan(_session(), 99, cache=_MemoryPlanCache())

    assert excinfo.value.code is ErrorCode.PLAN_NOT_FOUND


def test_invalidation_deletes_the_cache_entry_instead_of_writing_through() -> None:
    """改完套餐只删缓存、不写缓存：写缓存要求每处修改都记得同步，删则由下一次读兜住。"""

    from app.services.plan_service import invalidate_plan

    cache = _MemoryPlanCache()
    cache.items[PLAN_IDS[PLAN_PRO]] = PLAN_PRESETS[PLAN_PRO]

    invalidate_plan(cache, PLAN_IDS[PLAN_PRO])

    assert cache.deleted == [PLAN_IDS[PLAN_PRO]]
    assert cache.set_calls == []
    assert cache.items == {}


class _FakeRedis:
    """redis-py 的最小替身：只实现套餐缓存用到的那三条命令。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.expiries[key] = ttl

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.expiries.pop(key, None)


def test_redis_plan_cache_keys_by_plan_id_and_round_trips_the_snapshot() -> None:
    """缓存键是 **plan id**：管理员改个 code 不该让半个缓存失联。"""

    from app.core.plan_cache import RedisPlanCache

    client = _FakeRedis()
    cache = RedisPlanCache(client, ttl_seconds=60)

    cache.set(PLAN_IDS[PLAN_STANDARD], PLAN_PRESETS[PLAN_STANDARD])

    assert list(client.values) == [f"plan:{PLAN_IDS[PLAN_STANDARD]}"]
    assert client.expiries == {f"plan:{PLAN_IDS[PLAN_STANDARD]}": 60}
    assert cache.get(PLAN_IDS[PLAN_STANDARD]) == PLAN_PRESETS[PLAN_STANDARD]
    assert cache.get(PLAN_IDS[PLAN_PRO]) is None


def test_renaming_a_plan_code_keeps_the_same_cache_key() -> None:
    from dataclasses import replace

    from app.core.plan_cache import RedisPlanCache

    client = _FakeRedis()
    cache = RedisPlanCache(client, ttl_seconds=60)
    renamed = replace(PLAN_PRESETS[PLAN_PRO], code="pro-max")

    cache.set(PLAN_IDS[PLAN_PRO], PLAN_PRESETS[PLAN_PRO])
    cache.set(PLAN_IDS[PLAN_PRO], renamed)

    assert list(client.values) == [f"plan:{PLAN_IDS[PLAN_PRO]}"], (
        "键里没有 code，改名不产生第二个键"
    )
    assert cache.get(PLAN_IDS[PLAN_PRO]) == renamed


# ───────────────────────────────────────────── 守卫：内核不许认识框架


def test_kernel_module_imports_no_web_or_mcp_framework() -> None:
    """源码级守卫：这条约束不能只靠自觉，改起来太容易（一次补类型标注就够了）。"""

    import app.core.auth as kernel

    tree = ast.parse(Path(kernel.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"fastapi", "fastmcp", "starlette", "flask", "django"}


