"""真实 PostgreSQL 上的「连接到底复用没复用」探针（票 #131 / ADR-0023）。

默认套件不碰它：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块 skip，CI 里只有
``workflow_dispatch`` 触发的 ``integration`` job 会设它（与 ``test_integration_cas.py``
同一条门禁）。**替身在这里没有意义**：这一票问的就是「物理连接开了几条、服务端会话换了
没有」，内存 SQLite 与假引擎都答不了。

三个方案各跑**两个任务**，看三件可观察的事：

1. **物理连接开了几条**——SQLAlchemy 的 ``connect`` 事件。这是「复用」的硬证据，
   不是「跑通了」；
2. **后端 pid**（``select pg_backend_pid()``）——服务端确实是同一条会话；
3. **关停之后服务端还有没有这条会话**（``pg_stat_activity``）——这是「确定性释放」的
   硬证据。

三条用例对应的方案：

* ``test_the_resident_loop_reuses_one_physical_connection``：**A 常驻循环**（本文选的）；
* ``test_the_task_level_nullpool_opens_a_new_connection_per_task``：**B 任务级 NullPool**；
* ``test_the_status_quo_leaves_a_connection_nothing_in_the_task_path_can_close``：
  **现状**（模块级引擎 + 每任务 ``asyncio.run``，任务里没有 dispose）——它证明的恰恰是
  「不这么做会怎样」：happy path 上连接**照样被复用**（所以这个错法是沉默的），代价落在
  关停：两个任务的循环都关了，池里那条连接还在、服务端会话也还开着，任务路径里没有任何
  一步关得掉它。
"""

from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.async_session import MAX_OVERFLOW, POOL_RECYCLE_SECONDS, POOL_SIZE
from app.db.worker_loop import WorkerLoop, new_worker_loop

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]

T = TypeVar("T")


def _engine(**kwargs: object) -> AsyncEngine:
    assert INTEGRATION_DATABASE_URL is not None
    return create_async_engine(INTEGRATION_DATABASE_URL, **kwargs)


def _pooled_engine() -> AsyncEngine:
    """与生产 ``app.db.async_session`` **同一组池参数**（数字从那里 import，不重抄）。"""

    return _engine(
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )


def _count_physical_connects(engine: AsyncEngine) -> list[int]:
    """每开一条物理连接记一笔——「复用没复用」只能从这里读出来。"""

    opened: list[int] = []
    event.listen(engine.sync_engine, "connect", lambda *args, **kwargs: opened.append(1))
    return opened


async def _session_pid(sessions: object) -> int:
    """走一次真正的会话（与 #132 将要写的任务体同形），返回服务端后端 pid。"""

    async with sessions() as session:  # type: ignore[operator]
        return (await session.execute(text("select pg_backend_pid()"))).scalar_one()


async def _running_loop_id() -> int:
    return id(asyncio.get_running_loop())


async def _server_has(pid: int) -> bool:
    """这条服务端会话还活着吗（用一条自己的 NullPool 连接去问，不污染被观察的池）。"""

    engine = _engine(poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("select count(*) from pg_stat_activity where pid = :pid"), {"pid": pid}
                )
            ).scalar_one()
        return bool(count)
    finally:
        await engine.dispose()


def _run_in_a_fresh_loop(factory: Callable[[], Awaitable[T]]) -> T:
    """模拟现状的「每任务一个循环」：造一个新循环、跑完就关。"""

    loop = new_worker_loop()
    try:
        return loop.run_until_complete(factory())
    finally:
        loop.close()


# ───────────────────────────────────────────── A：常驻循环（本文选的方案）


def test_the_resident_loop_reuses_one_physical_connection() -> None:
    """两个任务：同一个循环、同一条物理连接、同一个后端 pid；关停后服务端会话消失。"""

    engine = _pooled_engine()
    opened = _count_physical_connects(engine)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    worker = WorkerLoop(name="probe-worker", engine=engine)
    loops: list[int] = []
    pids: list[int] = []
    try:
        for _ in range(2):
            loops.append(worker.run(_running_loop_id))
            pids.append(worker.run(lambda: _session_pid(sessions)))

        assert len(set(loops)) == 1, "两个任务必须落在同一个常驻循环上"
        assert len(opened) == 1, f"两个任务只该开一条物理连接，实际开了 {len(opened)} 条"
        assert pids[0] == pids[1], "服务端必须是同一条会话"
        assert engine.pool.checkedout() == 0, "任务结束后连接必须已经还回池里"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            worker.shutdown()
    finally:
        worker.shutdown()

    assert "Connections in pool: 0" in engine.pool.status(), "关停后池里不许留连接"
    assert not _run_in_a_fresh_loop(lambda: _server_has(pids[0])), "关停后服务端会话必须已经结束"
    noise = [str(item.message) for item in caught if "AsyncConnection" in str(item.message)]
    assert noise == [], f"关停不该刷连接噪音：{noise}"


# ─────────────────────────── 并发索引：连接数有上限，超出的排队而不是再开（票 #132）


async def _concurrent_sessions(sessions: object, count: int) -> None:
    """一个常驻循环里同时跑 ``count`` 个「任务」：每个开一个会话、做一次查询。"""

    async def _one() -> None:
        async with sessions() as session:  # type: ignore[operator]
            await session.execute(text("select 1"))

    await asyncio.gather(*[_one() for _ in range(count)])


def test_concurrent_tasks_stay_under_the_pool_ceiling_and_return_every_connection() -> None:
    """并发索引不会变成连接风暴：超过池上限的那些**排队**，跑完池里一条不剩。

    这是票 #132 的验收里「连接数有上限、有观测」那一条的硬证据：24 个并发任务、池上限
    15（``POOL_SIZE + MAX_OVERFLOW``），物理连接不会超过 15 条；跑完之后
    ``checkedout() == 0``（每任务 ``finally`` 关会话没被省掉），关停后池里也归零。
    """

    engine = _pooled_engine()
    opened = _count_physical_connects(engine)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    worker = WorkerLoop(name="concurrent-probe-worker", engine=engine)
    ceiling = POOL_SIZE + MAX_OVERFLOW
    try:
        worker.run(lambda: _concurrent_sessions(sessions, ceiling + 9))

        assert len(opened) <= ceiling, f"并发不该超过池上限 {ceiling}，实际开了 {len(opened)} 条"
        assert engine.pool.checkedout() == 0, "跑完之后连接必须全部还回池里"
    finally:
        worker.shutdown()

    assert "Connections in pool: 0" in engine.pool.status()


# ───────────────────────────────────────────── B：任务级 NullPool（没选的方案）

def test_the_task_level_nullpool_opens_a_new_connection_per_task() -> None:
    """每任务一条新连接（两次 connect、两个后端 pid），但每次都关得干净。"""

    pids: list[int] = []
    connects_per_task: list[int] = []
    for _ in range(2):
        engine = _engine(poolclass=NullPool)
        opened = _count_physical_connects(engine)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

        async def _task(own_engine: AsyncEngine = engine, own_sessions: object = sessions) -> int:
            try:
                return await _session_pid(own_sessions)
            finally:
                await own_engine.dispose()

        pids.append(_run_in_a_fresh_loop(_task))
        connects_per_task.append(len(opened))

    # NullPool 不存连接（没有 checkedout 可读）：残留只能从服务端看，见下面那条断言。

    assert connects_per_task == [1, 1], "NullPool 下每个任务都要重新握手"
    assert len(set(pids)) == 2, "每个任务都是新的服务端会话"
    assert all(not _run_in_a_fresh_loop(lambda pid=pid: _server_has(pid)) for pid in pids)


# ───────────────────────────────────────────── 现状：模块级引擎 + 每任务一个循环


def test_the_status_quo_leaves_a_connection_nothing_in_the_task_path_can_close() -> None:
    """不这么做会怎样：连接**照样被复用**，但两个任务的循环都关了，它还在。

    这条用例断言的是「沉默的错法」本身：happy path 全绿（一条物理连接、同一个 pid），
    代价出现在关停——池里那条连接由最后一个任务的循环持有，而那个循环已经关闭；任务
    路径里没有任何一步能在活着的循环上 ``dispose()``。本用例最后**必须自己造一个新循环**
    才关得掉它，正是这件事的证明。
    """

    engine = _pooled_engine()
    opened = _count_physical_connects(engine)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

    pids = [_run_in_a_fresh_loop(lambda: _session_pid(sessions)) for _ in range(2)]

    assert pids[0] == pids[1] and len(opened) == 1, "现状在 happy path 上确实复用了连接"
    assert "Connections in pool: 1" in engine.pool.status(), "两个任务结束后连接仍在池里"
    assert _run_in_a_fresh_loop(lambda: _server_has(pids[0])), "服务端会话也还开着"

    # 只有在一个**还活着的**循环上 dispose 才关得掉——任务路径里已经没有这样的循环了。
    _run_in_a_fresh_loop(engine.dispose)

    assert "Connections in pool: 0" in engine.pool.status()
    assert not _run_in_a_fresh_loop(lambda: _server_has(pids[0]))
