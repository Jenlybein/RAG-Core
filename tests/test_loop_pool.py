"""检索执行池的契约：常驻循环、并发上限、排队预算、确定性关停。

这四条都是**外部行为**，不是实现细节：

* 复用 —— 多次投递只落在少数几个循环上（否则「每请求一个新循环」那个毛病又回来了）；
* 上限 —— 同时干活的不超过 N；
* 预算 —— 排队超时按失败回给调用方，且池还能继续服务下一个任务；
* 关停 —— 循环停、线程收、客户端关，重复调用幂等。

**跨线程的同步原语**：任务跑在池的循环上，用例跑在测试自己的循环上，所以两者之间
只能用 ``threading.Event``（``asyncio.Event`` 绑在创建它的循环上，跨循环 await 会炸）。
每条用例都带超时保护：池写错时的典型症状是**挂死**，挂死的用例比失败的用例难查得多。
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time

import pytest

from app.core.loop_pool import LoopPool, reset_retrieval_pool


def _run(coro, *, timeout: float = 15.0):
    """在独立循环里跑一个协程，带超时——池挂死时用例失败而不是卡住套件。"""

    async def _wrapper():
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(_wrapper())


async def _wait_until(event: threading.Event, *, seconds: float) -> None:
    """在池的循环里等一个线程事件（不阻塞循环）。"""

    deadline = time.monotonic() + seconds
    while not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


# ───────────────────────────────────────────── 复用：循环是常驻的


def test_many_jobs_share_a_small_set_of_loops() -> None:
    """投 12 个任务，落到的循环数量不超过池大小——循环是复用的，不是每任务一个。"""

    pool = LoopPool(size=2, budget_seconds=5.0)
    seen: list[int] = []

    async def _job() -> None:
        seen.append(id(asyncio.get_running_loop()))

    async def _scenario() -> None:
        for _ in range(12):
            await pool.run(_job)
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()

    assert len(seen) == 12
    assert 1 <= len(set(seen)) <= 2, "循环数不该超过池大小"


def test_the_same_loop_serves_more_than_one_job() -> None:
    """至少有一个循环被复用过——这正是「每请求新循环」那个毛病消失的证据。"""

    pool = LoopPool(size=1, budget_seconds=5.0)
    seen: list[int] = []

    async def _job() -> None:
        seen.append(id(asyncio.get_running_loop()))

    async def _scenario() -> None:
        for _ in range(3):
            await pool.run(_job)
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()

    assert len(set(seen)) == 1


# ───────────────────────────────────────────── 上限：并发不超过 N


def test_concurrency_never_exceeds_the_pool_size() -> None:
    """同时干活的永远不超过 N：第 N+1 个必须排队。"""

    pool = LoopPool(size=2, budget_seconds=5.0)
    release = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()

    async def _job() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            await _wait_until(release, seconds=10.0)
        finally:
            with lock:
                active -= 1

    async def _scenario() -> None:
        jobs = [asyncio.create_task(pool.run(_job)) for _ in range(4)]
        await asyncio.sleep(0.1)  # 让前两个先跑起来
        release.set()
        await asyncio.gather(*jobs)
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()

    assert peak <= 2, f"并发峰值 {peak} 超过池大小"


# ───────────────────────────────────────────── 预算：排队超时按失败返回


def test_a_job_that_waits_too_long_fails_with_timeout() -> None:
    """排队超过预算 → ``TimeoutError``；池随后还能正常服务下一个任务。"""

    pool = LoopPool(size=1, budget_seconds=0.05)
    blocker = threading.Event()

    async def _blocking() -> None:
        await _wait_until(blocker, seconds=10.0)

    async def _quick() -> str:
        return "ok"

    async def _scenario() -> None:
        first = asyncio.create_task(pool.run(_blocking))
        await asyncio.sleep(0.01)  # 让第一个占住唯一的 worker
        # 300ms 后放行第一个：那时第二个已经在队列里等了远超 50ms 的预算。
        asyncio.get_running_loop().call_later(0.3, blocker.set)
        with pytest.raises(TimeoutError):
            await pool.run(_quick)
        await first
        assert await pool.run(_quick) == "ok"
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()


# ───────────────────────────────────────────── 关停：停循环、收线程、关客户端


def test_shutdown_stops_loops_and_closes_their_clients() -> None:
    """关停后：worker 线程都退出、循环都停、该循环名下的客户端已关闭。"""

    http_clients = importlib.import_module("app.core.http_clients")
    pool = LoopPool(size=2, budget_seconds=5.0)
    clients: list[object] = []

    async def _job() -> None:
        clients.append(http_clients.get_http_client())

    async def _scenario() -> None:
        await pool.run(_job)
        await pool.run(_job)
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()

    assert clients, "任务应当真的跑过"
    assert all(getattr(client, "is_closed", False) for client in clients), "关停后客户端必须已关闭"
    assert all(not worker._thread.is_alive() for worker in pool._workers), "线程必须收掉"


def test_shutdown_is_idempotent() -> None:
    """重复关停是安静的 no-op：关停路径不该抛错。"""

    pool = LoopPool(size=1, budget_seconds=1.0)

    async def _scenario() -> None:
        await pool.aclose()
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()


# ───────────────────────────────────────────── 进程级单例


def test_the_process_singleton_is_reset_between_uses() -> None:
    """``reset_retrieval_pool`` 之后拿到的是新池（测试清场靠它）。"""

    loop_pool = importlib.import_module("app.core.loop_pool")
    first = loop_pool.get_retrieval_pool()
    assert loop_pool.get_retrieval_pool() is first
    reset_retrieval_pool()
    second = loop_pool.get_retrieval_pool()
    assert second is not first
    reset_retrieval_pool()


def test_a_job_stuck_behind_a_blocked_worker_still_fails_on_budget() -> None:
    """worker 卡在同步调用里时任务永不出队——提交侧必须自己用预算兜住，否则无限等。

    这是规格轴抓到的那条：只在出队时判预算，等于「排队预算不存在」。
    """

    pool = LoopPool(size=1, budget_seconds=0.1)
    blocker = threading.Event()

    async def _blocking() -> None:
        await _wait_until(blocker, seconds=30.0)

    async def _quick() -> str:
        return "ok"

    async def _scenario() -> None:
        first = asyncio.create_task(pool.run(_blocking))
        await asyncio.sleep(0.02)  # 让唯一的 worker 忙起来
        started_at = time.perf_counter()
        with pytest.raises(TimeoutError):
            await pool.run(_quick)
        assert time.perf_counter() - started_at < 5.0, "预算必须由提交侧兜住"
        blocker.set()
        await first
        await pool.aclose()

    try:
        _run(_scenario())
    finally:
        pool.close_sync()


def test_sync_close_also_closes_the_loop_clients() -> None:
    """``close_sync``（atexit / 测试清场）也必须关客户端，不能只停循环。"""

    http_clients = importlib.import_module("app.core.http_clients")
    pool = LoopPool(size=1, budget_seconds=5.0)
    clients: list[object] = []

    async def _job() -> None:
        clients.append(http_clients.get_http_client())

    try:
        _run(pool.run(_job))
    finally:
        pool.close_sync()

    assert clients, "任务应当真的跑过"
    assert all(getattr(client, "is_closed", False) for client in clients)


def test_changing_the_configured_size_rebuilds_the_pool(monkeypatch) -> None:
    """面板改了规模，下一次取池就是新池（旧池同步关停）。"""

    from app.core.config import settings

    loop_pool = importlib.import_module("app.core.loop_pool")
    monkeypatch.setattr(settings, "RETRIEVAL_POOL_SIZE", 1)
    monkeypatch.setattr(settings, "RETRIEVAL_QUEUE_BUDGET_MS", 1000)
    first = loop_pool.get_retrieval_pool()
    assert first.size == 1

    monkeypatch.setattr(settings, "RETRIEVAL_POOL_SIZE", 2)
    second = loop_pool.get_retrieval_pool()
    assert second is not first
    assert second.size == 2
    reset_retrieval_pool()