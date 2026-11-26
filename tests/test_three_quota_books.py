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
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO
from app.services import console_service
from app.services.quota_gate import (
    KEY_NAMESPACE,
    LLM_NAMESPACE,
    CHUNK_NAMESPACE,
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


# ─────────────────────────────────────────────── 读数：三本账清单只写一处


def test_meter_usage_reads_each_book_from_its_own_namespace_and_column(
    monkeypatch: Any,
) -> None:
    """``meter_usage`` 按 ``METER_BOOKS`` 那张清单读三本账，各取各的桶与各取各的上限列。

    这张清单是「三本账」的唯一真相源：``/console/me``、``/auth/me`` 与面板都靠它循环。
    所以这里钉的是三件事：**三个命名空间各读一次**（不多不少）、上限来自**自己那一列**、
    以及 free 档那两列是 0——前端据此画「未开通」，后端不替它补成任何数。
    """

    seen: list[str] = []

    async def fake(*, tenant_id: str, namespace: str = KEY_NAMESPACE, **_: object) -> int:
        assert tenant_id == TENANT_ID
        seen.append(namespace)
        return 3

    monkeypatch.setattr(console_service, "read_tenant_daily_usage", fake)

    free = PLAN_PRESETS[PLAN_FREE]
    books = asyncio.run(console_service.meter_usage(tenant_id=TENANT_ID, plan=free))

    # 第四本账（#222/#223）是新增的第四栏：切块的 token 桶，与前三本互不相干。
    assert seen == [KEY_NAMESPACE, RERANK_NAMESPACE, LLM_NAMESPACE, CHUNK_NAMESPACE]
    assert books["usage"] == console_service.DailyUsageView(used=3, limit=free.retrieve_daily)
    assert books["chunk_usage"].limit == free.chunk_daily_tokens
    assert books["chunk_usage"].unit == "tokens", "第四本账按 token 计，不是次数"
    assert books["rerank_usage"].limit == 0, "free 档不含重排：上限就该是 0，由面板画「未开通」"
    assert books["llm_usage"].limit == 0
    assert set(books) == {book.field for book in console_service.METER_BOOKS}

    pro = PLAN_PRESETS[PLAN_PRO]
    pro_books = asyncio.run(console_service.meter_usage(tenant_id=TENANT_ID, plan=pro))
    assert pro_books["rerank_usage"].limit == pro.rerank_daily
    assert pro_books["llm_usage"].limit == pro.llm_daily
    assert pro_books["chunk_usage"].limit == pro.chunk_daily_tokens
