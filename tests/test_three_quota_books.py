"""第 13 步 · 三本账的原语：新账工厂 + 按实际发生记 N 次。

接缝与 ``test_quota_gate.py`` 相同（QuotaCounters + dict 假实现，不碰 Redis）；
这里只新增第 13 步的两条性质：

1. ``for_rerank`` / ``for_llm`` 读新列、用独立键前缀（与检索 / Research 不相干）；
2. ``record_n(n)`` 记 n 次，``n<=0`` 是 no-op——llm/rerank 一本账里一次调用可能跑多次。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PLAN_PRESETS, PLAN_PRO
from app.services.quota_gate import (
    LLM_NAMESPACE,
    RERANK_NAMESPACE,
    QuotaCounters,
    QuotaLimits,
    llm_quota,
    rerank_quota,
)
from tests.support.policy import policy as support_policy

TENANT_ID = "tenant_acme"
KEY_ID = "key-1"
MOMENT = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> str | None:
        value = self.values.get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, ttl: int) -> None:
        pass


def _policy(**overrides: Any) -> KeyPolicySnapshot:
    """一份只关心授权位与额度的快照；夹具词汇由 tests.support 翻译（含已退役列名）。"""

    return support_policy(**overrides)


def _plan(**overrides: int) -> object:
    return replace(
        PLAN_PRESETS[PLAN_PRO],
        rerank_qps=7,
        rerank_daily=70,
        llm_qps=3,
        llm_daily=30,
        **overrides,
    )


def test_rerank_and_llm_books_read_their_own_columns_and_namespaces() -> None:
    p = _policy(rerank_qps=5, rerank_daily=50, llm_qps=2, llm_daily=20)
    r = QuotaLimits.for_rerank(policy=p, plan=_plan())  # type: ignore[arg-type]
    m = QuotaLimits.for_llm(policy=p, plan=_plan())  # type: ignore[arg-type]

    assert (r.key_qps, r.tenant_qps, r.key_daily, r.tenant_daily) == (5, 7, 50, 70)
    assert r.namespace == RERANK_NAMESPACE
    assert (m.key_qps, m.tenant_qps, m.key_daily, m.tenant_daily) == (2, 3, 20, 30)
    assert m.namespace == LLM_NAMESPACE
    assert r.namespace != m.namespace


def test_record_n_counts_n_times_at_both_levels() -> None:
    store = _Store()
    # 注入 counters 时由它自己定 namespace（生产路径则由 limits.namespace 构造）。
    counters = QuotaCounters(store, clock=lambda: MOMENT, namespace=LLM_NAMESPACE)
    ledger = llm_quota(
        policy=_policy(),
        plan=_plan(),
        tenant_id=TENANT_ID,
        key_id=KEY_ID,  # type: ignore[arg-type]
        counters=counters,
        enabled=True,
    )

    asyncio.run(ledger.record_n(3))

    # 每个 level × 每个 window 各 3 次，共 3*2*2 = 12 个 incr。
    assert len(store.values) == 4
    assert all(value == 3 for value in store.values.values())
    assert all(key.startswith(LLM_NAMESPACE) for key in store.values)


def test_record_n_of_zero_or_negative_is_a_noop() -> None:
    store = _Store()
    counters = QuotaCounters(store, clock=lambda: MOMENT, namespace=RERANK_NAMESPACE)
    ledger = rerank_quota(
        policy=_policy(),
        plan=_plan(),
        tenant_id=TENANT_ID,
        key_id=KEY_ID,  # type: ignore[arg-type]
        counters=counters,
        enabled=True,
    )

    asyncio.run(ledger.record_n(0))
    asyncio.run(ledger.record_n(-1))

    assert store.values == {}
