"""票 #72 收尾（缺口 1）：数据面 ``GET /api/v1/auth/me`` —— 外部程序查自己的余量。

接缝只有一条：真实应用（``api_router`` + 统一异常壳）+ ``TestClient``，只看响应体。
只换两个外部依赖——数据库（既有的 ``RecordingSession``）与套餐缓存（内存假实现）；
**身份走真的 ``resolve_identity``**，用量走真的 ``read_tenant_daily_usage``。于是这里
断言的才是「同一个租户上下文答自己的问题」，而不是某个函数有没有被调用。

``/console/me`` 已经由 ``tests/test_console_api.py`` 覆盖；本票只补数据面那一扇门，
并刻意与它核对同一份用量口径：开关关着时 ``not_tracked``（不是 0），开关打开时才是
``daily_counter``。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import redis_client
from app.core.config import settings
from app.models.api_key import ApiKey
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from app.services.quota_gate import LEVEL_TENANT, QuotaCounters, daily_key

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"
ALPHA_NAME = "Alpha 公司"
BETA_NAME = "Beta 公司"

KEY_ALPHA = "rc_live_" + "a" * 32
KEY_BETA = "rc_live_" + "b" * 32
KEY_ALPHA_ID = "key-alpha"
KEY_BETA_ID = "key-beta"
POLICY_ALPHA_ID = "policy-alpha"
POLICY_BETA_ID = "policy-beta"

ME_PATH = "/api/v1/auth/me"


class _MemoryPlanCache:
    """内存假缓存：``get/set/delete`` 三个方法就是全部接缝（与控制面测试同形）。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.items.pop(plan_id, None)


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: Any

    def get(self, key: str | None) -> Any:
        headers = {} if key is None else {"Authorization": f"Bearer {key}"}
        return self.client.get(ME_PATH, headers=headers)

    def data(self, key: str) -> Any:
        body = self.get(key).json()
        assert body["code"] == 0, body
        return body["data"]


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _key_row(key_id: str, key_value: str, *, tenant_id: str, policy_id: str) -> ApiKey:
    return ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_value=key_value,
        label="外部应用",
        status="active",
    )


def _policy_row(policy_id: str, *, tenant_id: str, profile: str, qps: int, daily: int) -> KeyPolicy:
    return KeyPolicy(
        id=policy_id,
        tenant_id=tenant_id,
        name="外部应用",
        allowed_kb_ids=[],
        research_allowed=False,
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=qps,
        retrieve_daily=daily,
        profile=profile,
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )


def _world() -> dict[type, list[Any]]:
    """两个租户、两把 Key、两套配置：``/me`` 只能答自己那一份。"""

    return {
        Plan: _plan_rows(),
        Tenant: [
            Tenant(id=TENANT_ALPHA, name=ALPHA_NAME, plan_id=3, status="active"),
            Tenant(id=TENANT_BETA, name=BETA_NAME, plan_id=2, status="active"),
        ],
        ApiKey: [
            _key_row(KEY_ALPHA_ID, KEY_ALPHA, tenant_id=TENANT_ALPHA, policy_id=POLICY_ALPHA_ID),
            _key_row(KEY_BETA_ID, KEY_BETA, tenant_id=TENANT_BETA, policy_id=POLICY_BETA_ID),
        ],
        KeyPolicy: [
            _policy_row(POLICY_ALPHA_ID, tenant_id=TENANT_ALPHA, profile="speed", qps=50, daily=100000),
            _policy_row(POLICY_BETA_ID, tenant_id=TENANT_BETA, profile="speed", qps=10, daily=5000),
        ],
    }


@pytest.fixture
def me(monkeypatch: pytest.MonkeyPatch) -> Any:
    """真实应用 + 假数据库 + 假套餐缓存；``AUTH_ENABLED`` 打开，走真的判定内核。"""

    from tests.fake_db import RecordingSession

    api_router = importlib.import_module("app.api.router")
    error_handlers = importlib.import_module("app.api.error_handlers")
    db_session = importlib.import_module("app.db.session")
    plan_service = importlib.import_module("app.services.plan_service")

    db = RecordingSession(rows=_world())
    cache = _MemoryPlanCache()
    # 判定内核的套餐缓存缺省是 Redis；这里只换「存储」，规则一行不改。
    monkeypatch.setattr(plan_service, "default_plan_cache", lambda: cache)
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    app = FastAPI()
    app.include_router(api_router.api_router)
    error_handlers.install_handlers(app)
    app.dependency_overrides[db_session.get_db] = lambda: db
    try:
        with TestClient(app) as client:
            yield _Harness(client=client, db=db)
    finally:
        app.dependency_overrides.clear()


class _ReadOnlyCounterStore:
    """与配额服务同形的内存计数器；读路径只该出现 ``get:``，写入直接判失败。

    命令是**异步**的（票 #133）：注入缝换成了 ``redis.asyncio`` 的客户端。
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.calls: list[str] = []

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get:{key}")
        value = self.values.get(key)
        return None if value is None else str(value)

    async def incr(self, key: str) -> int:  # pragma: no cover - 读路径访问即失败
        raise AssertionError(f"读用量不许写计数：incr({key})")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - 读路径访问即失败
        raise AssertionError(f"读用量不许写计数：expire({key}, {ttl})")

    async def delete(self, key: str) -> None:  # pragma: no cover - 读路径访问即失败
        raise AssertionError(f"读用量不许写计数：delete({key})")


class _ExplodingCounterStore:
    """任何命令都炸：开关关着时连一条 Redis 命令都不该发。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, key: str) -> str | None:  # pragma: no cover - 访问即失败
        self.calls.append(f"get:{key}")
        raise AssertionError(f"不该访问 Redis：get({key})")

    async def incr(self, key: str) -> int:  # pragma: no cover - 访问即失败
        self.calls.append(f"incr:{key}")
        raise AssertionError(f"不该访问 Redis：incr({key})")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"expire:{key}")
        raise AssertionError(f"不该访问 Redis：expire({key}, {ttl})")

    async def delete(self, key: str) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"delete:{key}")
        raise AssertionError(f"不该访问 Redis：delete({key})")


@pytest.fixture
def counter_store() -> Any:
    """把配额计数器的唯一注入点换成可观察的内存实现，用完复位。"""

    store = _ReadOnlyCounterStore()
    redis_client.set_redis_client(store)
    try:
        yield store
    finally:
        redis_client.reset_redis_client()


@pytest.fixture
def exploding_store() -> Any:
    store = _ExplodingCounterStore()
    redis_client.set_redis_client(store)
    try:
        yield store
    finally:
        redis_client.reset_redis_client()


def _check_plan_snapshot(data: Any, plan_code: str) -> None:
    """套餐快照的对外形状：名称 + 能力开关 + 六个数值上限，一列不多一列不少。"""

    plan = PLAN_PRESETS[plan_code]
    snapshot = data["plan"]
    assert set(snapshot) == {
        "code",
        "name",
        "allowed_profiles",
        "hybrid_allowed",
        "rerank_allowed",
        "query_rewrite_allowed",
        "research_allowed",
        "retrieve_qps",
        "retrieve_daily",
        "max_kb",
        "max_kb_per_retrieve",
        "max_documents_per_kb",
        "max_processing_documents",
    }
    assert snapshot["code"] == plan.code
    assert snapshot["name"] == plan.name
    assert snapshot["allowed_profiles"] == list(plan.allowed_profiles)
    for column in (
        "hybrid_allowed",
        "rerank_allowed",
        "query_rewrite_allowed",
        "research_allowed",
        "retrieve_qps",
        "retrieve_daily",
        "max_kb",
        "max_kb_per_retrieve",
        "max_documents_per_kb",
        "max_processing_documents",
    ):
        assert snapshot[column] == getattr(plan, column)


# ───────────────────────────────────────────── 身份 + 套餐 + 用量：一次答全


def test_me_returns_the_callers_identity_plan_and_todays_usage(
    me: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _ReadOnlyCounterStore
) -> None:
    """有效 Key → 自己的租户身份、自己的套餐快照、自己今天的用量。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    data = me.data(KEY_ALPHA)

    assert data["tenant"] == {"tenant_id": TENANT_ALPHA, "name": ALPHA_NAME}
    _check_plan_snapshot(data, "pro")
    assert data["usage"] == {
        # 开关刚打开、今天还没有一次成功检索：这是**已知的 0**。
        "used": 0,
        "limit": PLAN_PRESETS["pro"].retrieve_daily,
        "source": "daily_counter",
        "note": None,
    }


def test_a_missing_key_is_unauthorized_with_20010(me: _Harness) -> None:
    body = me.get(None).json()

    assert body["code"] == 20010
    assert body["data"] is None


def test_an_unknown_key_is_unauthorized_with_20010(me: _Harness) -> None:
    body = me.get("rc_live_" + "0" * 32).json()

    assert body["code"] == 20010
    assert body["data"] is None


# ───────────────────────────────────────────── 不跨租户：身份与用量都各算各的


def test_me_never_leaks_another_tenants_data(
    me: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _ReadOnlyCounterStore
) -> None:
    """Alpha 的 Key 只能看到 Alpha；用量也是按租户各读各的日桶。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    window = QuotaCounters(counter_store).window()
    counter_store.values[daily_key(level=LEVEL_TENANT, ident=TENANT_ALPHA, window=window)] = 7

    alpha = me.data(KEY_ALPHA)
    beta = me.data(KEY_BETA)

    assert alpha["tenant"]["tenant_id"] == TENANT_ALPHA
    assert beta["tenant"]["tenant_id"] == TENANT_BETA
    assert alpha["usage"]["used"] == 7
    assert beta["usage"]["used"] == 0, "邻居的消耗不该算到 Beta 头上"

    alpha_text = me.get(KEY_ALPHA).text
    assert TENANT_BETA not in alpha_text
    assert BETA_NAME not in alpha_text
    assert POLICY_BETA_ID not in alpha_text


# ───────────────────────────────────────────── 用量口径：与控制面同一份「诚实」


def test_me_reports_usage_as_not_tracked_when_the_switch_is_off(
    me: _Harness, monkeypatch: pytest.MonkeyPatch, exploding_store: _ExplodingCounterStore
) -> None:
    """开关关着：计数从未被写过，报 0 是编造；如实说「平台没在统计」。"""

    from app.schemas.console import USAGE_NOTE_NOT_TRACKED

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    usage = me.data(KEY_ALPHA)["usage"]

    assert usage == {
        "used": None,
        "limit": PLAN_PRESETS["pro"].retrieve_daily,
        "source": "not_tracked",
        "note": USAGE_NOTE_NOT_TRACKED,
    }
    assert exploding_store.calls == [], "关掉时连一条 Redis 命令都不该发"


def test_reading_today_usage_never_increments_anything(
    me: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _ReadOnlyCounterStore
) -> None:
    """读接口只读：连读两次，值不变，且只出现 ``get``（与控制面同一条路径）。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    window = QuotaCounters(counter_store).window()
    key = daily_key(level=LEVEL_TENANT, ident=TENANT_ALPHA, window=window)
    counter_store.values[key] = 42

    before = dict(counter_store.values)
    counter_store.calls.clear()

    for _ in range(2):
        assert me.data(KEY_ALPHA)["usage"]["used"] == 42

    assert counter_store.values == before, "读路径不许改动任何计数"
    assert counter_store.calls, "读路径确实读了同一个日桶"
    assert all(call.startswith("get:") for call in counter_store.calls), (
        f"读路径只允许 GET，实际：{counter_store.calls}"
    )
