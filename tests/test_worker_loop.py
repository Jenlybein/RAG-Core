"""Worker 进程的事件循环归属（票 #131）：一个常驻循环，任务投进去，连接池跟着它活。

这一层断言的是**机制**，不是「跑通了」：两个任务是不是落在同一个 loop 上、引擎是不是在
那个 loop 上被 dispose、关停之后线程与循环有没有真的收掉、关停之后再投任务是不是明确
拒绝而不是挂死。**真实连接复用**（`pg_backend_pid` 不变、物理连接只开一次）只有真
PostgreSQL 说得清，见 `tests/test_integration_worker_loop.py`。

为什么值得单独有这么一层：错法的症状全是沉默的。任务里 `asyncio.run` 一次也「能跑」，
连接甚至还能被复用——psycopg 3.2 的异步连接并不把 loop 记在自己身上，跨 loop 复用一条
连接在 happy path 上是**成功的**（实测，见 ADR-0023 的读数表）。真正的代价出现在**关停
与异常路径**上：连接没人关、池里的槽位漏掉、进程退出时刷 SAWarning。这些用假引擎就能
钉死，不必等真库。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.db.worker_loop import (
    WorkerLoop,
    get_worker_loop,
    reset_worker_loop,
    shutdown_worker_loop,
)


class _FakeEngine:
    """异步引擎的最小替身：只回答「谁在哪个 loop 上 dispose 了我」。"""

    def __init__(self) -> None:
        self.disposed_on: list[int] = []

    async def dispose(self) -> None:
        self.disposed_on.append(id(asyncio.get_running_loop()))


async def _loop_id_job() -> int:
    """跑在哪个循环上——`id()` 在一次会话里唯一，正是「是不是同一个循环」的证据。"""

    return id(asyncio.get_running_loop())


async def _thread_name_job() -> str:
    return threading.current_thread().name


# ───────────────────────────────────────────── 复用：一个进程一个循环


def test_both_tasks_run_on_the_same_resident_loop() -> None:
    """连续两个任务落在**同一个**循环上——不是每任务一个循环。"""

    worker = WorkerLoop(name="probe-loop")
    try:
        first = worker.run(_loop_id_job)
        second = worker.run(_loop_id_job)
    finally:
        worker.shutdown()

    assert first == second, "两个任务必须复用同一个常驻循环"


def test_tasks_run_off_the_calling_thread() -> None:
    """任务跑在循环自己的线程里：调用方（Celery 任务线程）只等结果，不跑协程。"""

    worker = WorkerLoop(name="probe-loop")
    try:
        assert worker.run(_thread_name_job) == "probe-loop"
    finally:
        worker.shutdown()

    assert threading.current_thread().name != "probe-loop"


def test_a_failing_task_raises_to_the_caller_and_the_loop_keeps_working() -> None:
    """任务里的异常原样抛回调用方（不许被吞），循环随后还能服务下一个任务。"""

    worker = WorkerLoop(name="probe-loop")

    async def _boom() -> None:
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            worker.run(_boom)
        assert worker.run(_loop_id_job)
    finally:
        worker.shutdown()


# ───────────────────────────────────────────── 归属：引擎的生死都在这一个循环上


def test_the_engine_is_disposed_on_the_loop_that_used_it() -> None:
    """引擎在哪个循环上被用，就必须在**那个循环还活着的时候**被 dispose。

    跨循环 dispose 不是「效果差一点」：连接在别的（或已关闭的）循环上关不掉，SQLAlchemy
    只能把它丢掉并刷一条 SAWarning——服务端那条连接于是留到超时才消失。
    """

    engine = _FakeEngine()
    worker = WorkerLoop(engine=engine, name="probe-loop")
    used: list[int] = []

    async def _job() -> None:
        used.append(id(asyncio.get_running_loop()))
        assert worker.engine() is engine

    try:
        worker.run(_job)
        worker.run(_job)
    finally:
        worker.shutdown()

    assert len(used) == 2
    assert engine.disposed_on == used[:1], "dispose 必须发生在任务用的那个循环上"


def test_the_default_engine_is_the_process_async_engine() -> None:
    """缺省用进程级那一个异步引擎：一个进程一套池，连接预算不用重算。"""

    from app.db.async_session import async_engine

    worker = WorkerLoop(name="probe-loop")

    async def _job() -> object:
        return worker.engine()

    try:
        assert worker.run(_job) is async_engine
    finally:
        worker.shutdown()


# ───────────────────────────────────────────── 关停：确定性、幂等、不留尾巴


def test_shutdown_stops_the_thread_and_refuses_new_tasks() -> None:
    """关停之后：线程收掉、循环标记为关闭，再投任务立刻报错而不是挂死。"""

    worker = WorkerLoop(name="probe-loop")
    worker.run(_loop_id_job)

    worker.shutdown()

    assert worker.closed is True
    assert worker.alive is False
    with pytest.raises(RuntimeError):
        worker.run(_loop_id_job)


def test_shutdown_is_idempotent() -> None:
    """重复关停是安静的 no-op；引擎只 dispose 一次。"""

    engine = _FakeEngine()
    worker = WorkerLoop(engine=engine, name="probe-loop")
    worker.run(_loop_id_job)

    worker.shutdown()
    worker.shutdown()

    assert len(engine.disposed_on) == 1


def test_shutdown_closes_the_http_clients_of_the_resident_loop() -> None:
    """常驻循环名下的 HTTP 客户端也归它收：同一个循环上两次任务共用一个客户端。

    这一条是给 #132 的提醒：循环一旦常驻，任务体的 ``finally`` 里就不能再关当前循环的
    客户端了——那等于每个任务都把共享客户端关掉，复用立刻归零。
    """

    from app.core.http_clients import get_http_client

    worker = WorkerLoop(name="probe-loop")
    clients: list[object] = []

    async def _job() -> None:
        clients.append(get_http_client())

    try:
        worker.run(_job)
        worker.run(_job)
    finally:
        worker.shutdown()

    assert clients[0] is clients[1], "同一个循环上必须复用同一个客户端"
    assert getattr(clients[0], "is_closed", False) is True, "关停后客户端必须已关闭"


# ───────────────────────────────────────────── 进程级单例


def test_the_process_singleton_is_one_loop_and_reset_gives_a_fresh_one() -> None:
    """单例：拿到的是同一个循环；``reset_worker_loop`` 之后旧循环已关停、新循环另起。"""

    try:
        first = get_worker_loop()
        assert get_worker_loop() is first
        assert first.run(_loop_id_job) == first.run(_loop_id_job)

        reset_worker_loop()
        assert first.closed is True

        second = get_worker_loop()
        assert second is not first
        assert second.run(_loop_id_job)
    finally:
        reset_worker_loop()


def test_shutdown_worker_loop_releases_the_singleton() -> None:
    """进程收尾（Celery 的 ``worker_process_shutdown`` / ``atexit``）关掉单例，下次重新起。"""

    try:
        worker = get_worker_loop()
        shutdown_worker_loop()

        assert worker.closed is True
        assert get_worker_loop() is not worker
    finally:
        reset_worker_loop()
