"""固定规模的检索执行池：N 条常驻线程，各持一个**常驻事件循环**。

**为什么需要它**（第 10 步票 5）：混合检索的向量分支里有一次**同步**的 pgvector 查询，
它会卡住所在的事件循环。于是这条分支不能跑在主循环上，只能进线程；而「每个请求在
线程里 `asyncio.run` 一次」的写法，会让**每个请求都新建一个事件循环**——绑定循环的
异步 HTTP 客户端因此永远复用不了（请求结束循环就关），每请求漏一个客户端。

**为什么不是「所有请求共用一个常驻循环」**：那条同步查询会把唯一的循环卡住，并发直接
退化成 1。所以是 N 个 worker，每个 worker 一条线程 + 一个循环；并发上限 = N，排队有预算。

**为什么任务用「工厂函数」而不是协程对象投递**：协程对象一创建就绑在**当前**循环上，
在另一个循环里 await 它会炸。工厂在 worker 的循环里被调用，协程从一开始就属于它。

三条与「别把系统搞挂」直接相关的约定：

1. **排队有预算**（``QUEUE_BUDGET_SECONDS``）：worker 取到任务时先看它排了多久，超预算
   直接以 ``TimeoutError`` 失败——上层按「这一路挂了」降级，而不是无限等。预算是**排队**
   的预算，不是干活的预算：干活的超时由各 Provider 自己兜（嵌入 60s、重排按配置）。
2. **关停是确定性的**：先停止接收，再逐个循环关客户端，最后停循环、join 线程；重复调用
   幂等。关不掉（循环正卡在同步调用里）只记 warning，不抛——关停路径抛错会让进程退不干净。
3. **线程是 daemon**：即使某个任务卡死，解释器也能退出；``atexit`` 里再做一次尽力关停，
   评测脚本没有 lifespan，靠它兜底。
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

from app.core.http_clients import aclose_current_loop_clients

logger = structlog.get_logger(__name__)

T = TypeVar("T")

#: worker 数量。I/O 密集、且每个 worker 同一时刻只跑一个任务（同步查询会卡住循环），
#: 4 条足够跑满模型侧配额，又不会把突发流量放大成对上游的瞬时压力。
POOL_SIZE = 4
#: 排队预算：超过就按「这一路失败」处理。与关键词通道的时间预算同一量级。
QUEUE_BUDGET_SECONDS = 2.0
#: 关停时等一轮的宽限。卡在同步调用里的 worker 等不到，只记 warning。
SHUTDOWN_GRACE_SECONDS = 5.0
_START_TIMEOUT_SECONDS = 5.0


class _Worker:
    """一条常驻线程 + 一个常驻循环 + 一个任务队列。"""

    def __init__(self, index: int, *, budget_seconds: float) -> None:
        self.index = index
        self._budget_seconds = budget_seconds
        self._loop = asyncio.new_event_loop()
        self._queue: asyncio.Queue[tuple[Callable[[], Awaitable[Any]], concurrent.futures.Future[Any], float]] | None = None
        self._ready = threading.Event()
        self._accepting = True
        self._active = 0
        self._thread = threading.Thread(
            target=self._run, name=f"retrieval-pool-{index}", daemon=True
        )

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(_START_TIMEOUT_SECONDS):
            raise RuntimeError(f"retrieval pool worker {self.index} did not start in time")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue()
        # 消费任务也是这个循环上的一个任务：循环一跑起来它就等着队列。
        self._loop.create_task(self._consume())
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            self._drain_pending()
            self._loop.close()

    def _drain_pending(self) -> None:
        """循环停下来之后把没跑完的任务取消掉；失败只记 warning（关停路径不抛）。"""

        try:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:  # noqa: BLE001 - 关停路径不抛，见模块文档
            logger.warning("retrieval_pool_drain_failed", worker=self.index, exc_info=True)

    def stop_accepting(self) -> None:
        self._accepting = False

    def stop_loop(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:  # 循环已经停了
            return

    def join(self, timeout: float) -> None:
        self._thread.join(timeout=timeout)

    async def aclose_loop_clients(self, *, grace: float) -> None:
        """在这个 worker 的循环上关掉它名下的 HTTP 客户端。"""

        if self._loop.is_closed() or not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(aclose_current_loop_clients(), self._loop)
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=grace)
        except TimeoutError:
            # 循环正卡在同步调用里：关不掉就交给进程退出，别让关停路径卡住。
            logger.warning("retrieval_pool_close_clients_timed_out", worker=self.index)
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("retrieval_pool_close_clients_failed", worker=self.index, exc_info=True)

    # ── 投递与执行 ──────────────────────────────────────────────

    def submit(
        self, factory: Callable[[], Awaitable[Any]]
    ) -> concurrent.futures.Future[Any]:
        """把任务放进队列（跨线程安全）；返回一个可以在主循环里 await 的 future。"""

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        if not self._accepting:
            future.set_exception(RuntimeError("retrieval pool is shutting down"))
            return future
        submitted_at = time.perf_counter()
        try:
            self._loop.call_soon_threadsafe(self._enqueue, factory, future, submitted_at)
        except RuntimeError as exc:  # 循环已关
            future.set_exception(exc)
        return future

    def _enqueue(
        self,
        factory: Callable[[], Awaitable[Any]],
        future: concurrent.futures.Future[Any],
        submitted_at: float,
    ) -> None:
        assert self._queue is not None  # 只会在自己的循环里被调用
        self._queue.put_nowait((factory, future, submitted_at))

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            factory, future, submitted_at = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue
            waited_ms = (time.perf_counter() - submitted_at) * 1000
            if waited_ms > self._budget_seconds * 1000:
                # 排队超预算：不跑，按「这一路失败」回给调用方。
                if not future.cancelled():
                    future.set_exception(
                        TimeoutError(
                            f"retrieval pool queue budget exceeded ({waited_ms:.0f}ms > "
                            f"{self._budget_seconds * 1000:.0f}ms)"
                        )
                    )
                self._queue.task_done()
                continue
            self._active += 1
            started = time.perf_counter()
            try:
                result = await factory()
            except Exception as exc:  # noqa: BLE001 - 原样交回调用方
                if not future.cancelled():
                    future.set_exception(exc)
            else:
                if not future.cancelled():
                    future.set_result(result)
            finally:
                self._active -= 1
                self._queue.task_done()
                logger.debug(
                    "retrieval_pool_job",
                    worker=self.index,
                    queue_wait_ms=round(waited_ms, 2),
                    work_ms=round((time.perf_counter() - started) * 1000, 2),
                )


class LoopPool:
    """N 个 worker 的轮转调度；``run`` 是唯一入口。"""

    def __init__(
        self,
        *,
        size: int = POOL_SIZE,
        budget_seconds: float = QUEUE_BUDGET_SECONDS,
    ) -> None:
        if size < 1:
            raise ValueError(f"pool size must be >= 1, got {size!r}")
        self._budget_seconds = budget_seconds
        self._workers = [_Worker(index, budget_seconds=budget_seconds) for index in range(size)]
        self._next = 0
        self._closed = False
        for worker in self._workers:
            worker.start()

    @property
    def size(self) -> int:
        return len(self._workers)

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        """投递一个任务并等它的结果；排队超预算会抛 ``TimeoutError``。"""

        if self._closed:
            raise RuntimeError("retrieval pool is closed")
        worker = self._workers[self._next]
        self._next = (self._next + 1) % len(self._workers)
        future = worker.submit(factory)
        return await asyncio.wrap_future(future)

    async def aclose(self) -> None:
        """确定性关停：停止接收 → 逐个循环关客户端 → 停循环 → join。幂等。"""

        if self._closed:
            return
        self._closed = True
        for worker in self._workers:
            worker.stop_accepting()
        for worker in self._workers:
            await worker.aclose_loop_clients(grace=SHUTDOWN_GRACE_SECONDS)
        for worker in self._workers:
            worker.stop_loop()
        for worker in self._workers:
            worker.join(SHUTDOWN_GRACE_SECONDS)

    def close_sync(self, *, grace: float = 1.0) -> None:
        """同步关停（atexit 与测试清场用）：只停循环、join，不去等客户端关闭。"""

        if self._closed:
            return
        self._closed = True
        for worker in self._workers:
            worker.stop_accepting()
        for worker in self._workers:
            worker.stop_loop()
        for worker in self._workers:
            worker.join(grace)


_pool: LoopPool | None = None
_lock = threading.Lock()


def get_retrieval_pool() -> LoopPool:
    """进程级单例。第一次用到时才建线程——离线用例不碰检索就不会付这份代价。"""

    global _pool
    with _lock:
        if _pool is None or _pool.closed:
            # 显式传 size：默认参数在函数定义时就绑定了，改模块常量不会生效。
            _pool = LoopPool(size=POOL_SIZE)
        return _pool


def reset_retrieval_pool() -> None:
    """丢掉当前池并同步停掉它的线程。测试与进程退出用。"""

    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close_sync()


async def aclose_retrieval_pool() -> None:
    """应用关停时调用：确定性关停（含关掉各循环名下的客户端）。"""

    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        await pool.aclose()


atexit.register(reset_retrieval_pool)

__all__ = [
    "POOL_SIZE",
    "QUEUE_BUDGET_SECONDS",
    "LoopPool",
    "aclose_retrieval_pool",
    "get_retrieval_pool",
    "reset_retrieval_pool",
]
