"""第 14 步（#200）· **被拒的请求也要落一行**，以及 ``last_used_at`` 终于有人写。

## 这张票要证明的三件事

1. 走到门禁里被拒的请求（``20013`` / ``20014`` / ``20033`` …）在 ``usage_events`` 里**有一行**，
   带业务码、端点、Key 与耗时——否则「成功率」永远没有分母；
2. 那一行**没有交付物**：没有片、没有稿号、没有版本。被拒不是「0 命中的检索」，用空值
   表达这件事比让它们看起来像一次空结果要诚实；
3. ``api_keys.last_used_at`` 在这一列存在了一整年之后，终于被写了一次。

## 为什么直调门禁、不走 ASGI

被拒的路径在**第一、二道关卡**就返回了，一次数据库、一次 Redis 都不碰。走 ASGI 反而要
伪造一整套依赖才能证明「它没碰库」，而这里要证明的恰恰是「没碰库也落了行」。

写库那条旁路仍然是替身（:class:`tests.fake_db.AsyncRecordingSession`）：它记下 ``add()``
进来的对象，所以「落了几行」直接可数。
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Iterator

import pytest

from app.core.auth import KeyContext
from app.core.exceptions import AppError, ErrorCode
from app.models.usage_event import UsageEvent
from app.services.retrieval_gate import gated_decompose, gated_retrieve
from app.services.retrieval_trace import record_retrieval_trace
from app.services.usage_meter import (
    BOOK_LLM,
    BOOK_R,
    BOOK_RR,
    count_meter,
    current_meter_counts,
    meter_tally,
    record_refusal,
)
from tests.fake_db import AsyncRecordingSession

TENANT_ID = "tenant_usage"
KEY_ID = "key_usage_01"

#: 问句哨兵：它走到门禁第二道关卡（``ensure_query_has_content``）就被拦下，
#: 但**一个字都不许**出现在那一行账里。
QUERY_SENTINEL = "T200-BLANK-QUERY-9c1e"


class _BrokenFactory:
    """开不出会话的工厂：用来证明「写不进去」不会把拒绝变成 500。"""

    def __call__(self) -> Any:
        raise RuntimeError("no database today")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncRecordingSession]:
    """把旁路写库的会话换成记录器（唯一的接缝：``app.db.session_factory``）。"""

    factory_mod = importlib.import_module("app.db.session_factory")
    session = AsyncRecordingSession()
    factory_mod.set_async_session_factory(lambda: session)
    try:
        yield session
    finally:
        factory_mod.set_async_session_factory(None)


def _key() -> KeyContext:
    return KeyContext(tenant_id=TENANT_ID, key_id=KEY_ID)


def _rows(session: AsyncRecordingSession) -> list[UsageEvent]:
    return session.added_of(UsageEvent)


# ────────────────────────────────────────────── 被拒：门禁侧落行


@pytest.mark.asyncio
async def test_a_refused_decompose_leaves_exactly_one_row(
    recorder: AsyncRecordingSession,
) -> None:
    """空白问句在第二道关卡被拦（``20033``）——门禁没碰库，账上仍要有一行。"""

    with pytest.raises(AppError) as refusal:
        await gated_decompose(None, key=_key(), kb_ids=["kb-usage"], query="   ")

    assert refusal.value.code == ErrorCode.QUERY_HAS_NO_CONTENT
    rows = _rows(recorder)
    assert len(rows) == 1, "被拒的请求也必须留下分母"
    row = rows[0]
    assert row.code == int(ErrorCode.QUERY_HAS_NO_CONTENT)
    assert row.endpoint == "decompose"
    assert row.key_id == KEY_ID
    assert row.tenant_id == TENANT_ID
    assert row.latency_ms >= 0


@pytest.mark.asyncio
async def test_a_refused_row_carries_no_delivery(recorder: AsyncRecordingSession) -> None:
    """被拒 ≠ 空结果：没有片、没有稿号、没有版本，三本账也是 0（一次都没扣）。"""

    with pytest.raises(AppError):
        await gated_decompose(None, key=_key(), kb_ids=["kb-usage"], query="   ")

    row = _rows(recorder)[0]
    assert row.chunk_ids == []
    assert row.doc_versions == {}
    assert row.index_versions == {}
    assert row.top_k == 0
    assert (row.meter_r, row.meter_rr, row.meter_llm) == (0, 0, 0)


@pytest.mark.asyncio
async def test_the_refused_row_says_which_endpoint_refused(
    recorder: AsyncRecordingSession,
) -> None:
    """``/rag/rewrite`` 被拒时端点是 ``rewrite``，不是 ``retrieve``——账要认得清是哪条路。"""

    with pytest.raises(AppError) as refusal:
        await gated_retrieve(None, key=_key(), kb_ids=["kb-usage"], query="  ", variant="rewrite")

    row = _rows(recorder)[0]
    assert row.endpoint == "rewrite"
    assert row.code == int(refusal.value.code)


@pytest.mark.asyncio
async def test_the_refusal_recorder_cannot_even_accept_a_question(
    recorder: AsyncRecordingSession,
) -> None:
    """结构性守卫：记账函数的签名里**没有**放问句的地方（ADR-0021 的红线）。

    比「往行里塞个哨兵再 grep 一遍」更硬：只要签名里没有 ``query``，任何调用点都无法把它
    传进来；将来有人在签名里加一个 ``query`` 参数，这条用例当场红。
    """

    parameters = set(inspect.signature(record_refusal).parameters)

    assert not {"query", "question", "content", "text"} & parameters, (
        "记账函数不许接收问句/正文：那一列一旦存在，迟早有人往里写"
    )


@pytest.mark.asyncio
async def test_a_broken_writer_does_not_turn_a_refusal_into_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写库是旁路：开不出会话只留 warning，调用方该拿到 20033，不是 500。"""

    factory_mod = importlib.import_module("app.db.session_factory")
    factory_mod.set_async_session_factory(_BrokenFactory())
    try:
        with pytest.raises(AppError) as refusal:
            await gated_decompose(None, key=_key(), kb_ids=["kb-usage"], query="   ")
    finally:
        factory_mod.set_async_session_factory(None)

    assert refusal.value.code == ErrorCode.QUERY_HAS_NO_CONTENT


# ────────────────────────────────────────── 成功：码、三本账、last_used_at


@pytest.mark.asyncio
async def test_a_successful_row_carries_code_zero_and_the_meters(
    recorder: AsyncRecordingSession,
) -> None:
    """成功那一行就是看板上的分子：``code = 0`` + 三本账的实际次数。"""

    await record_retrieval_trace(
        log_id="log-usage-1",
        trace_id="trace-usage-1",
        tenant_id=TENANT_ID,
        key_id=KEY_ID,
        endpoint="decompose",
        code=0,
        meter_r=3,
        meter_rr=2,
        meter_llm=1,
        kb_ids=["kb-usage"],
        index_versions={"kb-usage": "v1"},
        doc_versions={"doc-1": 2},
        chunk_ids=["chunk-1"],
        top_k=5,
        latency_ms=812,
        degraded_reason=None,
        empty_reason=None,
    )

    row = _rows(recorder)[0]
    assert (row.code, row.meter_r, row.meter_rr, row.meter_llm) == (0, 3, 2, 1)
    assert row.endpoint == "decompose"


@pytest.mark.asyncio
async def test_the_successful_row_pushes_last_used_at(
    recorder: AsyncRecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``api_keys.last_used_at`` 从建表起没人写过：这一行顺便把它推到「刚刚」。"""

    from app.repositories import api_key_repository

    touched: list[tuple[str, Any]] = []

    async def _record(session: Any, key_id: str, used_at: Any) -> None:
        touched.append((key_id, used_at))

    monkeypatch.setattr(api_key_repository, "touch_last_used", _record)

    await record_retrieval_trace(
        log_id="log-usage-2",
        trace_id="trace-usage-2",
        tenant_id=TENANT_ID,
        key_id=KEY_ID,
        endpoint="retrieve",
        code=0,
        meter_r=1,
        kb_ids=["kb-usage"],
        index_versions={},
        doc_versions={},
        chunk_ids=[],
        top_k=1,
        latency_ms=12,
        degraded_reason=None,
        empty_reason=None,
    )

    assert [key for key, _ in touched] == [KEY_ID], "这一列只在这一处被写"


@pytest.mark.asyncio
async def test_a_row_without_a_key_does_not_touch_any_key_row(
    recorder: AsyncRecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 ``key_id`` 的行（老调用方、旁证类内部调用）不许去写 ``api_keys``。"""

    from app.repositories import api_key_repository

    touched: list[str] = []

    async def _record(session: Any, key_id: str, used_at: Any) -> None:
        touched.append(key_id)

    monkeypatch.setattr(api_key_repository, "touch_last_used", _record)

    await record_retrieval_trace(
        log_id="log-usage-3",
        trace_id="trace-usage-3",
        tenant_id=TENANT_ID,
        kb_ids=[],
        index_versions={},
        doc_versions={},
        chunk_ids=[],
        top_k=0,
        latency_ms=0,
        degraded_reason=None,
        empty_reason=None,
    )

    assert touched == []


def test_the_meter_tally_accumulates_and_is_absent_outside_the_gate() -> None:
    """账本的三本账来自门禁扣账那一刻的累加；不在门禁里时是 ``None``（调用方据此回落）。

    ``None`` 与 ``(0, 0, 0)`` 必须分得开：前者是「这次没人记账」（内部评测的召回），
    后者是「记了，一次都没扣」。混成一件事，写账的人就只能猜。
    """

    assert current_meter_counts() is None

    with meter_tally():
        count_meter(BOOK_R, 2)
        count_meter(BOOK_R, 1)
        count_meter(BOOK_LLM, 1)
        # 0 或负数不累加：``record_n(0)`` 那几处是「账本 no-op」，不该在账上留痕。
        count_meter(BOOK_RR, 0)
        assert current_meter_counts() == (3, 0, 1)

    assert current_meter_counts() is None, "作用域结束就还回去，不泄漏到下一个请求"


# ─────────────────── 写账那一刻 tally 还是空的：全 0 要当成「没有信号」（现场七）


def test_an_empty_tally_is_not_a_zero_cost() -> None:
    """门禁作用域里、还没扣账时读 tally，必须回 ``None`` 而不是 ``(0, 0, 0)``。

    现场（2026-11-27）：用量页上「检索 / 重排 / 大模型」三本账一直是 0，而数据面明明调了
    十几次。原因是顺序——组合根在响应成形时就写下那一行账，门禁的 ``record_success`` /
    ``record_n`` 要等它**返回之后**才执行，于是写账那一刻 tally 必然全 0。

    把 0 当成事实写进库，比「没有这一列」更坏：看板会给出一个**确定的错数**。所以全 0 时
    回 ``None``，写账方据此回落「这次用了哪些能力」的推断值（与门禁真扣的值相等）。
    """

    with meter_tally():
        # 什么都没扣（门禁还没走到那一步）。
        assert current_meter_counts() is None

        count_meter(BOOK_R, 1)
        # 一旦有信号，它就是权威值。
        assert current_meter_counts() == (1, 0, 0)


def test_a_zero_book_alone_is_still_no_signal() -> None:
    """只记了 0 次（例如重排被跳过）仍然是「没信号」：``count_meter(BOOK_RR, 0)`` 不算证据。"""

    with meter_tally():
        count_meter(BOOK_RR, 0)
        assert current_meter_counts() is None
