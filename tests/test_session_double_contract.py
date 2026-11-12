"""票 #126：同步替身与异步替身的**共享契约**。

迁移期两套替身会同时存在：同步路径用 ``tests.fake_db.RecordingSession``，异步路径用
``AsyncRecordingSession``。它们一旦漂移，症状是「异步用例绿、同步用例红」，或者更糟——
两边都绿，但记录的已经不是同一件事（异步替身少记一条 SQL、``rollback`` 只数了个寂寞）。

所以这里跑**同一组操作**，断言三样东西一致：发出去的 SQL、拿到的结果、可观测的副作用
（写了哪些行、删了哪些行、提交几次、回滚几次、关没关）。

期望值写成字面量而不是「用另一套替身算一遍」：替身自己算出来的期望永远和它自己一致，
那样的断言在漂移时仍然是绿的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text

from tests.fake_db import AsyncRecordingSession, RecordingSession

_QUERY = text("SELECT id FROM knowledge_base WHERE tenant_id = :tid")
_INSERT = text("INSERT INTO knowledge_base (id) VALUES (:id)")
_DELETE = text("DELETE FROM knowledge_base WHERE id = :id")

#: 同一组操作应得的读数（独立于实现写下的期望值）。
EXPECTED: dict[str, Any] = {
    "ids": ["kb-1", "kb-2"],
    "added": ["kb-9"],
    "deleted": ["kb-9"],
    "commits": 1,
    "rollbacks": 1,
    "closed": True,
}

EXPECTED_STATEMENTS = [str(_QUERY), str(_INSERT), str(_DELETE)]


@dataclass
class _Row:
    """替身只按类型分桶，所以一组「行」用一个最小的数据类就够。"""

    id: str


def _reading(session: RecordingSession, ids: list[str]) -> dict[str, Any]:
    return {
        "ids": ids,
        "added": [row.id for row in session.added],
        "deleted": [row.id for row in session.deleted],
        "commits": session.commits,
        "rollbacks": session.rollbacks,
        "closed": session.closed,
        "statements": list(session.statements),
    }


def _run_sync(session: RecordingSession) -> dict[str, Any]:
    ids = list(session.execute(_QUERY, {"tid": "t-1"}).scalars().all())
    row = _Row(id="kb-9")
    session.add(row)
    session.flush()
    session.execute(_INSERT, {"id": row.id})
    session.commit()
    session.delete(row)
    session.execute(_DELETE, {"id": row.id})
    session.rollback()
    session.close()
    return _reading(session, ids)


async def _run_async(session: AsyncRecordingSession) -> dict[str, Any]:
    ids = list((await session.execute(_QUERY, {"tid": "t-1"})).scalars().all())
    row = _Row(id="kb-9")
    # ``add`` 是 ``AsyncSession`` 上**唯一**仍是同步的写方法（其余写方法都要 await）：
    # 替身跟着真实 API 走，于是「生产代码漏了 await」与「替身写错了一侧」不会互相抵消。
    session.add(row)
    await session.flush()
    await session.execute(_INSERT, {"id": row.id})
    await session.commit()
    await session.delete(row)
    await session.execute(_DELETE, {"id": row.id})
    await session.rollback()
    await session.close()
    return _reading(session, ids)


def _assert_contract(reading: dict[str, Any]) -> None:
    assert {key: reading[key] for key in EXPECTED} == EXPECTED
    assert reading["statements"] == EXPECTED_STATEMENTS


def test_the_sync_double_matches_the_contract() -> None:
    _assert_contract(_run_sync(RecordingSession(tenant_ids=["kb-1", "kb-2"])))


@pytest.mark.asyncio
async def test_the_async_double_matches_the_same_contract() -> None:
    _assert_contract(await _run_async(AsyncRecordingSession(tenant_ids=["kb-1", "kb-2"])))


@pytest.mark.asyncio
async def test_both_doubles_produce_the_same_sql_and_the_same_results() -> None:
    """共享契约本身：同一组操作 → 同一组 SQL、同一组结果。"""

    sync_reading = _run_sync(RecordingSession(tenant_ids=["kb-1", "kb-2"]))
    async_reading = await _run_async(AsyncRecordingSession(tenant_ids=["kb-1", "kb-2"]))

    assert async_reading == sync_reading
