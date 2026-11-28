"""保留期清理在真库上的集成用例（第 17 步现场八）。

## 为什么必须在真库上跑

这一层的全部判据都是**库里那几行还在不在**：

* 明细删不删，取决于「那一天的汇总格在不在」——而这个「在不在」是一次 ``SELECT DISTINCT``
  的结果，离线替身不回它；
* 汇总删不删，是一句 ``DELETE ... WHERE day < 截止日``，离线替身不模拟行的变化。

在替身上断言「调用过删除」正是这一票要消灭的东西（真缺陷是「调用了但删错了一批」）。

## 纪律

与 ``tests/test_index_sweeper.py`` 同一套：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块
skip；只建自己用到的两张表、只删本文件种下的行（按专用 ``tenant_id`` 收口）。这个租户名是
本文件独有的，绝不可能撞上别人的数据。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.transaction import async_transaction
from app.models.usage_daily import UsageDaily
from app.models.usage_event import UsageEvent
from app.services.retention import purge_usage
from app.services.usage_rollup import day_bounds

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]

#: 本文件专用租户：清理只按它收口。
TENANT_ID = "tenant_retention"


def _sync_url() -> str:
    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


def _async_url() -> str:
    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


TODAY = date(2026, 11, 27)
#: 有汇总格、且超出明细保留期的那一天（保留期 90 天 → 它该被删）。
DAY_COVERED = TODAY - timedelta(days=95)
#: 有明细、**没有**汇总格的那一天（补射还没补上 → 它的明细必须留着）。
DAY_GAP = TODAY - timedelta(days=96)
#: 汇总格自己也过期的那一天（汇总保留期 395 天）。
DAY_DAILY_EXPIRED = TODAY - timedelta(days=500)


@pytest.fixture(scope="module")
def sync_engine() -> Iterator[Engine]:
    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(_sync_url())
    Base.metadata.create_all(bind=engine, tables=[UsageEvent.__table__, UsageDaily.__table__])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(sync_engine: Engine) -> Iterator[Session]:
    session = Session(bind=sync_engine)
    _clean(session)
    _seed(session)
    try:
        yield session
    finally:
        _clean(session)
        session.close()


@pytest_asyncio.fixture
async def async_session() -> Any:
    """清理的会话：指向同一个真库，好让断言看得见它的删。"""

    engine = create_async_engine(_async_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _clean(db: Session) -> None:
    try:
        db.execute(delete(UsageEvent).where(UsageEvent.tenant_id == TENANT_ID))
        db.execute(delete(UsageDaily).where(UsageDaily.tenant_id == TENANT_ID))
        db.commit()
    except Exception:
        db.rollback()
        raise


def _seed(db: Session) -> None:
    """三种处境各一行：有格的过期天、没格的过期天、汇总也过期的天。"""

    for day, tag in ((DAY_COVERED, "covered"), (DAY_GAP, "gap")):
        start, _ = day_bounds(day)
        db.add(
            UsageEvent(
                id=f"ret-{tag}-1",
                tenant_id=TENANT_ID,
                trace_id=f"ret-trace-{tag}",
                endpoint="retrieve",
                code=0,
                kb_ids=[],
                doc_versions={},
                chunk_ids=[],
                top_k=5,
                latency_ms=100,
                created_at=start + timedelta(hours=12),
            )
        )
    db.add(
        UsageDaily(
            day=DAY_COVERED,
            tenant_id=TENANT_ID,
            key_id="",
            endpoint="retrieve",
            code=0,
            requests=1,
            ok_requests=1,
        )
    )
    db.add(
        UsageDaily(
            day=DAY_DAILY_EXPIRED,
            tenant_id=TENANT_ID,
            key_id="",
            endpoint="retrieve",
            code=0,
            requests=1,
            ok_requests=1,
        )
    )
    db.commit()


def _event_count(db: Session) -> int:
    return db.execute(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == TENANT_ID)
    ).scalar_one()


def _daily_days(db: Session) -> set[date]:
    return {
        row
        for row in db.execute(
            select(UsageDaily.day).where(UsageDaily.tenant_id == TENANT_ID)
        ).scalars()
    }


@pytest.mark.asyncio
async def test_covered_days_lose_their_detail_and_gaps_keep_theirs(
    db: Session, async_session: Any
) -> None:
    """有汇总格的过期天：明细删掉；没格的过期天：明细**留着**并记账。

    这就是「先汇总后删明细」的全部意义：汇总表里没有那一天，明细就是唯一的数据。
    一刀切地按日期删，删掉的就是永久丢失——长窗口看板只读汇总表。
    """

    # 与生产同一个事务边界的调用方式（``retention_once`` 就是这么包的）：清理本身
    # 不提交，提交点集中在 ``app.db.transaction``。
    async with async_transaction(async_session):
        outcome = await purge_usage(async_session, now=TODAY)

    assert outcome.events_deleted == 1, "只删有格的那一天"
    assert outcome.skipped_days == [str(DAY_GAP)]
    assert _event_count(db) == 1, "没有汇总格的那一天必须原样留着"

    remaining = db.execute(select(UsageEvent.id).where(UsageEvent.tenant_id == TENANT_ID)).scalars()
    assert list(remaining).count("ret-gap-1") == 1


@pytest.mark.asyncio
async def test_the_daily_cell_expires_on_its_own_longer_clock(
    db: Session, async_session: Any
) -> None:
    """明细 90 天、汇总 395 天：两条保留期各自算，互不牵连。

    「汇总比明细留得久」是刻意的：一天几十行，留一年多也不占地方，而「去年这个时候」这种
    问题只有它答得出。
    """

    async with async_transaction(async_session):
        outcome = await purge_usage(async_session, now=TODAY)

    days = _daily_days(db)
    assert DAY_DAILY_EXPIRED not in days, "过了 395 天的汇总格要删"
    assert DAY_COVERED in days, "95 天前的汇总格还在保留期内，不能跟着明细一起删"
    assert outcome.daily_deleted == 1
