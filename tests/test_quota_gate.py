"""票 #79 / 票 #133：两级配额与限流的**纯单元**接缝（计数器 + 门禁）。

接缝是平台层的两个对象：

* :class:`app.services.quota_gate.QuotaCounters` —— 两种计数形状（秒级 QPS 桶、按天分桶的
  日配额）的读写门面。它只认三条 Redis 命令（``incr`` / ``get`` / ``expire``）与一个可注入
  的时钟，测试注入一个 **dict 假实现**，默认套件因此**不碰 Redis**；
* :class:`app.services.quota_gate.RetrievalQuota` —— 一次检索的额度账本：进门查一次、
  成功后记一次。

**假实现是异步的**（票 #133）：生产客户端换成 ``redis.asyncio``，计数命令因此是协程。
假实现跟着写 ``async def`` 不是为了贴合实现，而是这条接缝的**形状**——一个同步假实现
会让「忘了 await」的写法照样读到值，于是那条 bug 在套件里彻底看不见。

这里只测「判定与计数本身」，接线（两个错误码、跨多库只记一次、开关关掉不碰 Redis）在
``tests/test_quota_wiring.py`` 的 ASGI 边界上测。

**边界刻意按「计到上限为止」写**：额度 3 时第 3 次调用是允许的（它把计数带到 3），
第 4 次才被拒。反过来写（``>=`` 与 ``>`` 混用）会让每个租户都少用一次额度，
而且这种偏差在手工试用时根本看不出来。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import PROFILE_SPEED, KeyPolicySnapshot
from app.models.plan import PLAN_PRESETS, PLAN_PRO, PlanSpec
from app.services.quota_gate import (
    DAILY_TTL_SECONDS,
    QPS_TTL_SECONDS,
    QuotaCounters,
    retrieval_quota,
)

TENANT_ID = "tenant_acme"
KEY_ID = "key-1"
MOMENT = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc)


def _run(coroutine: Coroutine[Any, Any, None]) -> None:
    """跑一条异步用例。套件没有 pytest-asyncio（那属于票 #126），沿用仓库既有的
    ``asyncio.run`` 写法：一条用例一个事件循环，与生产里「一个请求在同一个循环上」同形。"""

    asyncio.run(coroutine)


class _FakeCounterStore:
    """计数器真正用到的那三条命令：一个 dict 加一份 TTL 账本。"""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> str | None:
        value = self.values.get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


class _ExplodingStore:
    """任何一条命令都炸：用来证明「开关关着时一次都没碰 Redis」。"""

    async def incr(self, key: str) -> int:  # pragma: no cover - 访问即失败
        raise AssertionError(f"不该访问 Redis：incr({key})")

    async def get(self, key: str) -> str | None:  # pragma: no cover - 访问即失败
        raise AssertionError(f"不该访问 Redis：get({key})")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - 访问即失败
        raise AssertionError(f"不该访问 Redis：expire({key}, {ttl})")


def _counting_at(store: Any, moment: datetime) -> QuotaCounters:
    return QuotaCounters(store, clock=lambda: moment)


def _policy(*, qps: int = 3, daily: int = 100) -> KeyPolicySnapshot:
    """一份最小的密钥配置快照：本票只关心它的两个限额。"""

    return KeyPolicySnapshot(
        allowed_kb_ids=("kb-1",),
        research_allowed=False,
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=qps,
        retrieve_daily=daily,
        profile=PROFILE_SPEED,
        include_parent=True,
        expand_table_rows=True,
    )


def _plan(*, qps: int = 50, daily: int = 1000) -> PlanSpec:
    """最高档套餐，只改两个限额：本票不重测别的列。"""

    return replace(PLAN_PRESETS[PLAN_PRO], retrieve_qps=qps, retrieve_daily=daily)


def _gate(
    store: Any,
    *,
    policy: KeyPolicySnapshot | None = None,
    plan: PlanSpec | None = None,
    moment: datetime = MOMENT,
    key_id: str = KEY_ID,
    enabled: bool = True,
) -> Any:
    return retrieval_quota(
        policy=policy or _policy(),
        plan=plan or _plan(),
        tenant_id=TENANT_ID,
        key_id=key_id,
        counters=_counting_at(store, moment),
        enabled=enabled,
    )


# ───────────────────────────────────────────── 两种计数形状


def test_a_qps_bucket_counts_only_inside_its_own_second() -> None:
    """秒级桶的键里嵌着那一秒，于是它的存活时间只需要覆盖那一秒。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        counters = _counting_at(store, MOMENT)
        window = counters.window()

        await counters.add_qps(level="key", ident=KEY_ID, window=window)
        await counters.add_qps(level="key", ident=KEY_ID, window=window)

        assert await counters.qps(level="key", ident=KEY_ID, window=window) == 2
        # 1794276000 = 2026-11-10T02:00:00Z 的 epoch 秒（独立算出来的常量，不是回算）。
        key = "quota:qps:key:key-1:1794276000"
        assert list(store.values) == [key]
        assert store.ttls[key] == QPS_TTL_SECONDS

    _run(scenario())


def test_the_qps_window_slides_because_the_key_embeds_the_second() -> None:
    async def scenario() -> None:
        store = _FakeCounterStore()
        now = _counting_at(store, MOMENT)
        first = now.window()
        await now.add_qps(level="key", ident=KEY_ID, window=first)

        later = _counting_at(store, MOMENT.replace(second=1))
        second = later.window()

        assert first.qps != second.qps
        assert await later.qps(level="key", ident=KEY_ID, window=second) == 0, "下一秒是新的窗口"

    _run(scenario())


def test_the_daily_bucket_rolls_over_at_midnight_and_outlives_the_day() -> None:
    """日配额按天分桶，TTL **略长于** 24 小时：跨零点的那一刻不能把桶弄丢。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        late = _counting_at(store, datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc))
        window = late.window()
        await late.add_daily(level="tenant", ident=TENANT_ID, window=window)

        key = "quota:daily:tenant:tenant_acme:20261110"
        assert list(store.values) == [key]
        assert store.ttls[key] == DAILY_TTL_SECONDS
        assert DAILY_TTL_SECONDS > 24 * 3600

        # 零点之后（哪怕前一秒刚记过）是新的一天、新的一份额度。
        after = _counting_at(store, datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone.utc))
        next_day = after.window()
        assert next_day.daily != window.daily
        assert await after.daily(level="tenant", ident=TENANT_ID, window=next_day) == 0

    _run(scenario())


def test_an_absent_counter_reads_as_zero() -> None:
    async def scenario() -> None:
        counters = _counting_at(_FakeCounterStore(), MOMENT)
        window = counters.window()

        assert await counters.qps(level="key", ident="never-seen", window=window) == 0
        assert await counters.daily(level="tenant", ident="never-seen", window=window) == 0

    _run(scenario())


def test_the_two_levels_do_not_share_a_bucket() -> None:
    """Key 级与租户级是两个键：否则「一把 Key 跑飞不连累其他 Key」无从谈起。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        counters = _counting_at(store, MOMENT)
        window = counters.window()

        await counters.add_qps(level="key", ident=KEY_ID, window=window)

        assert await counters.qps(level="key", ident=KEY_ID, window=window) == 1
        assert await counters.qps(level="tenant", ident=TENANT_ID, window=window) == 0

    _run(scenario())


# ───────────────────────────────────────────── 门禁：两个错误码，先限流后配额


def test_the_request_that_reaches_the_limit_is_allowed_one_more() -> None:
    """额度 3：计到 2 时放行（这是第 3 次），计到 3 时拒绝（这会是第 4 次）。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store, policy=_policy(qps=3), plan=_plan(qps=50))
        window = _counting_at(store, MOMENT).window()

        store.values[f"quota:qps:key:{KEY_ID}:{window.qps}"] = 2
        await gate.ensure_available()

        store.values[f"quota:qps:key:{KEY_ID}:{window.qps}"] = 3
        with pytest.raises(AppError) as exc:
            await gate.ensure_available()
        assert exc.value.code is ErrorCode.API_RATE_LIMIT

    _run(scenario())


def test_exhausting_the_daily_allowance_is_a_quota_error_not_a_rate_error() -> None:
    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store, policy=_policy(daily=100), plan=_plan(daily=1000))
        window = _counting_at(store, MOMENT).window()

        store.values[f"quota:daily:key:{KEY_ID}:{window.daily}"] = 99
        await gate.ensure_available()

        store.values[f"quota:daily:key:{KEY_ID}:{window.daily}"] = 100
        with pytest.raises(AppError) as exc:
            await gate.ensure_available()
        assert exc.value.code is ErrorCode.QUOTA_EXCEEDED

    _run(scenario())


def test_the_tenant_level_trips_even_when_the_key_level_is_fine() -> None:
    """两级都要在：Key 还剩额度，租户的聚合已经到顶时仍然要拒绝。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store, policy=_policy(daily=100), plan=_plan(daily=10))
        window = _counting_at(store, MOMENT).window()

        store.values[f"quota:daily:tenant:{TENANT_ID}:{window.daily}"] = 10

        with pytest.raises(AppError) as exc:
            await gate.ensure_available()
        assert exc.value.code is ErrorCode.QUOTA_EXCEEDED

    _run(scenario())


def test_rate_limiting_is_checked_before_the_daily_allowance() -> None:
    """两边都超时先报限流（20005）：ADR-0018 的门禁顺序是「限流 → 日配额」。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store, policy=_policy(qps=1, daily=1), plan=_plan(qps=50, daily=1000))
        window = _counting_at(store, MOMENT).window()

        store.values[f"quota:qps:key:{KEY_ID}:{window.qps}"] = 1
        store.values[f"quota:daily:key:{KEY_ID}:{window.daily}"] = 1

        with pytest.raises(AppError) as exc:
            await gate.ensure_available()
        assert exc.value.code is ErrorCode.API_RATE_LIMIT

    _run(scenario())


# ───────────────────────────────────────────── 只有成功才记账


def test_a_successful_call_is_counted_at_both_levels() -> None:
    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store)
        window = gate.window  # typing: ignore[attr-defined]

        await gate.ensure_available()
        assert store.values == {}, "进门只查不记"

        await gate.record_success()
        assert store.values == {
            f"quota:qps:key:{KEY_ID}:{window.qps}": 1,
            f"quota:qps:tenant:{TENANT_ID}:{window.qps}": 1,
            f"quota:daily:key:{KEY_ID}:{window.daily}": 1,
            f"quota:daily:tenant:{TENANT_ID}:{window.daily}": 1,
        }, "一次调用在两级各记一份"

    _run(scenario())


def test_a_rejected_call_records_nothing() -> None:
    """被拒的请求不扣额度：否则一个拼错的请求能在几百次重试里烧掉一整天。"""

    async def scenario() -> None:
        store = _FakeCounterStore()
        gate = _gate(store, policy=_policy(qps=1), plan=_plan(qps=50))
        window = _counting_at(store, MOMENT).window()
        store.values[f"quota:qps:key:{KEY_ID}:{window.qps}"] = 1

        for _ in range(5):
            with pytest.raises(AppError):
                await gate.ensure_available()

        assert store.values == {f"quota:qps:key:{KEY_ID}:{window.qps}": 1}, "拒绝不记账"

    _run(scenario())


def test_the_gate_is_a_no_op_when_the_switch_is_off() -> None:
    """开关关着时它是 no-op，而且**一次都不碰** Redis。"""

    async def scenario() -> None:
        gate = _gate(_ExplodingStore(), policy=_policy(qps=0, daily=0), plan=_plan(), enabled=False)

        await gate.ensure_available()
        await gate.record_success()

    _run(scenario())


def test_the_counter_service_builds_its_client_through_the_single_injection_point() -> None:
    """开关打开且没有注入计数器时，客户端来自 ``app.core.redis_client`` 这唯一一处构造点。

    用模块自己的注入接口（``set_redis_client``）换掉客户端，而不是 monkeypatch 全局
    私有变量：后者测的是「有没有 monkeypatch 成功」，前者测的是「门禁确实走了那个
    唯一构造点」。注入的假实现是**异步**的：接缝换成了 ``redis.asyncio``（票 #133）。
    """

    from app.core import redis_client

    async def scenario() -> None:
        store = _FakeCounterStore()
        redis_client.set_redis_client(store)
        try:
            gate = retrieval_quota(
                policy=_policy(),
                plan=_plan(),
                tenant_id=TENANT_ID,
                key_id=KEY_ID,
                enabled=True,
            )
            await gate.record_success()
        finally:
            redis_client.reset_redis_client()

        assert sum(store.values.values()) == 4, "两级各记 qps + daily 一次"

    _run(scenario())
