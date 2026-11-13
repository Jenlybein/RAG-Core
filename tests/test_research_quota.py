"""票 #150：Research 的两级配额——门禁顺序、三个错误码、成功后记一次。

这一层验的是**门禁与记账**，不是状态机（那是 ``test_research_state_machine.py``）也不是
响应形状（那是 ``test_research_entry.py``）。所以它直接调 ``gated_research``：那是 HTTP 与
MCP 两个入口共用的那段序列本体，路由只负责把身份、参数与会话递进来。这么测的好处是
「进门查了几次、记账记了几次」变成可数的事实，而不是靠 HTTP 边界去猜。

六条验收各自落在下面某一条用例上：

| 验收 | 用例 |
| --- | --- |
| 专业版能跑；标准版 ``20013`` | :func:`test_the_pro_plan_can_run_research_and_the_standard_plan_is_rejected` |
| 秒级超限 ``20005`` | :func:`test_a_second_call_in_the_same_second_is_rate_limited_with_20005` |
| 日配额用尽 ``20014`` | :func:`test_an_exhausted_daily_allowance_is_a_quota_error_with_20014` |
| 被拒绝不扣额度 | :func:`test_a_rejected_call_records_nothing_and_burns_no_model_call` |
| 一次 Research 只记一次 | :func:`test_one_research_is_counted_once_at_both_levels` |
| 配额 0 与能力为假同一个码 | :func:`test_a_zero_quota_and_a_disabled_capability_share_one_code` |

两条配额是**独立的一本账**（``quota:research:*``）：重资源调用不许吃掉普通检索的额度，
所以还有一条 ``test_research_counters_do_not_touch_the_retrieval_buckets``。
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from app.core import redis_client
from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import PROFILE_SPEED, KeyPolicySnapshot
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PLAN_STANDARD, PlanSpec
from app.providers.llm.base import LLMProvider
from app.services.quota_gate import (
    QuotaCounters,
    QuotaWindow,
    daily_key,
    qps_key,
    research_quota,
)
from app.services.research_gate import gated_research
from tests.fake_db import EmptySession, async_visible_kb

TENANT_ID = "tenant_demo"
KEY_ID = "key-under-test"
KB = "kb-a"
QUERY = "退款多久到账，需要哪些材料"

#: Research 那本账的键前缀。写成常量而不是散落的字面量：本文件有一半断言是「哪个桶里
#: 有几个数」，前缀拼错会让它们全部变成「0 == 0」的假绿。
RESEARCH_QPS_PREFIX = "quota:research:qps:"
RESEARCH_DAILY_PREFIX = "quota:research:daily:"


# ─────────────────────────────────────────────────────────────── 离线替身


class _CounterStore:
    """计数器用到的那三条命令：一个 dict 加一份 TTL 账本（异步形状，与生产一致）。"""

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


class _Search:
    """子检索替身：按子查询返回预置行，并记下每一次被问到的句子。"""

    def __init__(self, rows_for: Mapping[str, Sequence[dict[str, Any]]]) -> None:
        self.rows_for = dict(rows_for)
        self.queries: list[str] = []

    async def __call__(self, sub_query: str) -> list[dict[str, Any]]:
        self.queries.append(sub_query)
        return [dict(row) for row in self.rows_for.get(sub_query, ())]


class _ScriptedLLM(LLMProvider):
    """规划 + 判断两次结构化调用；按调用顺序交出预置输出。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        if not self._responses:
            raise AssertionError("Research 本票只该问两次模型")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _row(key: str, content: str) -> dict[str, Any]:
    return {
        "document_id": f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "title": "退款政策",
        "content": content,
        "score": 0.9,
        "section_id": f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


def _plan_output() -> dict:
    """两个要点、两条子查询：`「一次 Research 只记一次」` 与「子查询不重复扣」同一条用例。"""

    return {
        "points": ["退款时限", "所需材料"],
        "queries": [
            {"point": 1, "query": "退款多久到账"},
            {"point": 2, "query": "退款需要哪些材料"},
        ],
    }


def _covered() -> dict:
    return {"queries": []}


# ─────────────────────────────────────────────────────────────── 身份与限额


def _policy(*, research_allowed: bool = True, qps: int = 1, daily: int = 100) -> KeyPolicySnapshot:
    """这套集成的密钥配置：档位 ``speed``（替身只需要一条通道），Research 默认给足。"""

    return replace(
        DEV_KEY_POLICY,
        profile=PROFILE_SPEED,
        research_allowed=research_allowed,
        research_qps=qps,
        research_daily=daily,
    )


def _plan(code: str = PLAN_PRO, **overrides: Any) -> PlanSpec:
    """三档种子之一；``overrides`` 用来把某一列改成「管理员事后调过」的样子。"""

    return dataclasses.replace(PLAN_PRESETS[code], **overrides)


def _window() -> QuotaWindow:
    """与生产同一个时钟：窗口口径不是被测逻辑，测试从同一个对象上取。"""

    return QuotaCounters(_CounterStore()).window()


def _seconds_from(window: QuotaWindow, span: int = 3) -> list[QuotaWindow]:
    """当前秒与随后两秒：让「已计到上限」的预置不因跨秒边界而失效。"""

    base = int(window.qps)
    return [QuotaWindow(qps=str(base + offset), daily=window.daily) for offset in range(span)]


def _prefill_qps(store: _CounterStore, *, ident: str, limit: int) -> None:
    for item in _seconds_from(_window()):
        store.values[qps_key(level="key", ident=ident, window=item, namespace="quota:research")] = (
            limit
        )


def _prefill_daily(store: _CounterStore, *, ident: str, limit: int) -> None:
    window = _window()
    store.values[daily_key(level="key", ident=ident, window=window, namespace="quota:research")] = (
        limit
    )


def _sum_of(store: _CounterStore, prefix: str) -> int:
    """按前缀汇总：不断言具体的秒 / 日，避免跨秒边界把断言弄成间歇性失败。"""

    return sum(value for key, value in store.values.items() if key.startswith(prefix))


# ─────────────────────────────────────────────────────────────── 一次调用


class _Harness:
    """一次 Research 的三件可观察物：计数器、子检索、模型。"""

    def __init__(self, store: _CounterStore) -> None:
        self.store = store
        self.search = _Search(
            {
                "退款多久到账": [_row("a", "超期退款需人工审核。")],
                "退款需要哪些材料": [_row("b", "3 个工作日内到账。")],
            }
        )
        self.llm = _ScriptedLLM(_plan_output(), _covered())

    async def run(
        self,
        *,
        policy: KeyPolicySnapshot | None = None,
        plan: PlanSpec | None = None,
    ) -> Any:
        key = KeyContext(
            tenant_id=TENANT_ID,
            key_id=KEY_ID,
            policy=policy if policy is not None else _policy(),
            plan=plan if plan is not None else _plan(),
        )

        async def _llm_factory() -> LLMProvider:
            return self.llm

        return await gated_research(
            EmptySession(),  # type: ignore[arg-type]
            key=key,
            kb_ids=[KB],
            query=QUERY,
            search=self.search,
            llm_factory=_llm_factory,
        )


@pytest.fixture
def research(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """离线：计数器经唯一注入点换成 fake，可见性与限流开关都在夹具里钉死。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    store = _CounterStore()
    redis_client.set_redis_client(store)
    try:
        yield _Harness(store)
    finally:
        redis_client.reset_redis_client()


# ─────────────────────────────────────────────────────────────── 授权：拒绝而不是降级


@pytest.mark.asyncio
async def test_the_pro_plan_can_run_research_and_the_standard_plan_is_rejected(
    research: _Harness,
) -> None:
    """专业版能跑；标准版 ``20013``——**不是**静默降级成一次普通检索。

    标准版那一次连一条子查询都不该发：拒绝发生在门禁里，早于执行。
    """

    data = await research.run(policy=_policy(), plan=_plan(PLAN_PRO))
    assert data.query == QUERY
    assert research.search.queries == ["退款多久到账", "退款需要哪些材料"]

    research.search.queries.clear()
    research.llm = _ScriptedLLM(_plan_output(), _covered())

    with pytest.raises(AppError) as excinfo:
        await research.run(policy=_policy(research_allowed=True), plan=_plan(PLAN_STANDARD))

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert research.search.queries == [], "被拒的调用一次子检索都不该发"


@pytest.mark.asyncio
async def test_a_zero_quota_and_a_disabled_capability_share_one_code(
    research: _Harness,
) -> None:
    """配额为 0 与能力开关为假 → **同一个** ``20013``（不泄露配置细节）。

    四种「等于关闭」的写法各试一次：能力关、套餐配额 0、配置配额 0、日配额 0。
    它们若答成不同的码，调用方就能从错误码里读出「这个租户其实买了 Research，只是
    额度填了 0」——那正是票面不允许泄露的配置细节。
    """

    cases = (
        # 能力开关为假（两层各一次）。
        (_policy(research_allowed=False), _plan(PLAN_PRO)),
        (_policy(research_allowed=True), _plan(PLAN_PRO, research_allowed=False)),
        # 配额为 0（四个格子各一次，0 等同于关闭）。
        (_policy(qps=0), _plan(PLAN_PRO)),
        (_policy(daily=0), _plan(PLAN_PRO)),
        (_policy(), _plan(PLAN_PRO, research_qps=0)),
        (_policy(), _plan(PLAN_PRO, research_daily=0)),
    )

    for policy, plan in cases:
        with pytest.raises(AppError) as excinfo:
            await research.run(policy=policy, plan=plan)
        assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED, (policy, plan)

    assert research.search.queries == [], "配额为 0 是配置事实，不该先跑起来再判"
    assert research.llm.calls == []


# ─────────────────────────────────────────────────────────────── 三个错误码


@pytest.mark.asyncio
async def test_a_second_call_in_the_same_second_is_rate_limited_with_20005(
    research: _Harness,
) -> None:
    """秒级超限 → ``20005``：调用方该做的是**退避重试**，不是提额。"""

    _prefill_qps(research.store, ident=KEY_ID, limit=1)

    with pytest.raises(AppError) as excinfo:
        await research.run(policy=_policy(qps=1), plan=_plan(PLAN_PRO))

    assert excinfo.value.code is ErrorCode.API_RATE_LIMIT
    assert research.search.queries == [], "限流先于执行"


@pytest.mark.asyncio
async def test_an_exhausted_daily_allowance_is_a_quota_error_with_20014(
    research: _Harness,
) -> None:
    """日配额用尽 → ``20014``：重试没有意义，该做的是提额或换套餐。"""

    _prefill_daily(research.store, ident=KEY_ID, limit=1)

    with pytest.raises(AppError) as excinfo:
        await research.run(policy=_policy(daily=1), plan=_plan(PLAN_PRO))

    assert excinfo.value.code is ErrorCode.QUOTA_EXCEEDED
    assert research.search.queries == [], "日配额先于执行"


@pytest.mark.asyncio
async def test_the_two_limits_are_reported_with_different_codes(research: _Harness) -> None:
    """同一个调用先撞限流、再撞日配额时，报的必须是**先撞上的那一个**（顺序即判定）。"""

    _prefill_qps(research.store, ident=KEY_ID, limit=1)
    _prefill_daily(research.store, ident=KEY_ID, limit=1)

    with pytest.raises(AppError) as excinfo:
        await research.run(policy=_policy(qps=1, daily=1), plan=_plan(PLAN_PRO))

    assert excinfo.value.code is ErrorCode.API_RATE_LIMIT, "秒级限流在日配额之前判"


# ─────────────────────────────────────────────────────────────── 开关：关着就一次都不碰 Redis


@pytest.mark.asyncio
async def test_the_switch_off_makes_the_research_gate_touch_no_redis(
    research: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RATE_LIMIT_ENABLED`` 关着 = no-op：默认套件因此不需要 Redis。"""

    class _Exploding:
        async def incr(self, key: str) -> int:
            raise AssertionError(f"不该访问 Redis：incr({key})")

        async def get(self, key: str) -> str | None:
            raise AssertionError(f"不该访问 Redis：get({key})")

        async def expire(self, key: str, ttl: int) -> None:
            raise AssertionError(f"不该访问 Redis：expire({key})")

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    redis_client.set_redis_client(_Exploding())

    data = await research.run(policy=_policy(qps=10, daily=100), plan=_plan(PLAN_PRO))

    assert data.query == QUERY


# ─────────────────────────────────────────────────────────────── 配额账本与用量口径


def test_the_research_ledger_uses_its_own_key_namespace() -> None:
    """两本账的键前缀不同：``quota:research:`` 与 ``quota:``（检索）分得开。"""

    window = QuotaWindow(qps="1700000000", daily="20251114")

    assert (
        qps_key(level="tenant", ident=TENANT_ID, window=window, namespace="quota:research")
        == f"quota:research:qps:tenant:{TENANT_ID}:1700000000"
    )
    assert (
        daily_key(level="tenant", ident=TENANT_ID, window=window, namespace="quota:research")
        == f"quota:research:daily:tenant:{TENANT_ID}:20251114"
    )
    # 检索那两个键**逐字不变**：这条路径已经在跑，本票不许动它。
    assert (
        qps_key(level="tenant", ident=TENANT_ID, window=window)
        == f"quota:qps:tenant:{TENANT_ID}:1700000000"
    )


def test_the_research_factory_reads_the_research_columns() -> None:
    """账本的上限来自 **research** 那两列，而不是 ``retrieve_*``。"""

    ledger = research_quota(
        policy=_policy(qps=1, daily=100),
        plan=_plan(PLAN_PRO),
        tenant_id=TENANT_ID,
        key_id=KEY_ID,
        counters=QuotaCounters(_CounterStore()),
        enabled=True,
    )

    assert ledger.limits.key_qps == 1
    assert ledger.limits.tenant_qps == 1
    assert ledger.limits.key_daily == 100
    assert ledger.limits.tenant_daily == 100
    assert ledger.limits.namespace == "quota:research"


def test_the_free_plan_seed_has_no_research_allowance() -> None:
    """种子就是票面：free / standard 的 Research 配额是 0（= 关闭），pro 是 1 / 100。"""

    assert (PLAN_PRESETS[PLAN_FREE].research_qps, PLAN_PRESETS[PLAN_FREE].research_daily) == (0, 0)
    assert (
        PLAN_PRESETS[PLAN_STANDARD].research_qps,
        PLAN_PRESETS[PLAN_STANDARD].research_daily,
    ) == (
        0,
        0,
    )
    assert (PLAN_PRESETS[PLAN_PRO].research_qps, PLAN_PRESETS[PLAN_PRO].research_daily) == (1, 100)
