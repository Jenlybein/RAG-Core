"""第 13 步 · 三本账接线的计数验收（quota_gate 接缝）。

证明 rewrite / decompose / research 成功后，llm 账按实际模型调用次数记、
rerank 账按「这次排没排」记。用 dict 假计数器，不碰 Redis。
"""

from __future__ import annotations
from tests.support.policy import policy as support_policy

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from app.core.auth import KeyContext
from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PLAN_PRESETS, PLAN_PRO
from app.services.quota_gate import (
    LLM_NAMESPACE,
    RERANK_NAMESPACE,
    QuotaCounters,
)
from tests.support.policy import PROFILE_SPEED

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
        v = self.values.get(key)
        return None if v is None else str(v)

    async def expire(self, key: str, ttl: int) -> None:
        pass


def _policy(**overrides: Any) -> KeyPolicySnapshot:
    """一份只关心授权位与额度的快照；夹具词汇由 tests.support 翻译（含已退役列名）。"""

    return support_policy(**overrides)


def _plan(**overrides: int) -> object:
    values: dict[str, int] = dict(
        rerank_qps=7, rerank_daily=70, llm_qps=3, llm_daily=30
    )
    values.update(overrides)
    return replace(PLAN_PRESETS[PLAN_PRO], **values)


def test_an_all_zero_llm_book_is_a_noop_for_legacy_keys() -> None:
    """旧 Key 升级上来 llm 四格全 0：账本 no-op，不查不记不拒。"""

    from app.services.quota_gate import llm_quota

    store = _Store()
    counters = QuotaCounters(store, clock=lambda: MOMENT, namespace=LLM_NAMESPACE)
    ledger = llm_quota(
        policy=_policy(llm_qps=0, llm_daily=0), plan=_plan(llm_qps=0, llm_daily=0),  # type: ignore[arg-type]
        tenant_id=TENANT_ID, key_id=KEY_ID, counters=counters,
    )
    assert ledger.enabled is False
    asyncio.run(ledger.ensure_available())
    asyncio.run(ledger.record_n(4))
    assert store.values == {}


def test_a_configured_llm_book_records_the_actual_count() -> None:
    """配了额度的 llm 账：一次研究跑 4 次模型，就记 4 次（两级各 4）。"""

    from app.services.quota_gate import llm_quota

    store = _Store()
    counters = QuotaCounters(store, clock=lambda: MOMENT, namespace=LLM_NAMESPACE)
    ledger = llm_quota(
        policy=_policy(), plan=_plan(), tenant_id=TENANT_ID, key_id=KEY_ID,  # type: ignore[arg-type]
        counters=counters, enabled=True,
    )
    asyncio.run(ledger.record_n(4))
    assert all(value == 4 for value in store.values.values())
    assert all(key.startswith(LLM_NAMESPACE) for key in store.values)


def test_a_configured_rerank_book_records_only_witheld_applied() -> None:
    """重排账按实际发生记：只有 applied 那一次记 1。"""

    from app.services.quota_gate import rerank_quota

    store = _Store()
    counters = QuotaCounters(store, clock=lambda: MOMENT, namespace=RERANK_NAMESPACE)
    ledger = rerank_quota(
        policy=_policy(), plan=_plan(), tenant_id=TENANT_ID, key_id=KEY_ID,  # type: ignore[arg-type]
        counters=counters, enabled=True,
    )
    applied = True  # 模拟 metadata.retrieval.rerank.applied
    asyncio.run(ledger.record_n(1 if applied else 0))
    assert all(value == 1 for value in store.values.values())
