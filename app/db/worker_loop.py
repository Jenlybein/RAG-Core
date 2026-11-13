"""Worker 进程的常驻事件循环：一个进程一个循环，异步连接池跟着它活（ADR-0023）。

## 这一票做了什么、没做什么

一个 ``WorkerLoop``（一条常驻线程 + 一个常驻 loop + 它名下引擎与 HTTP 客户端的收尾）、
一个进程级单例 ``get_worker_loop()``、以及关停入口 ``shutdown_worker_loop()``。
**没有接进任何 Celery 任务**——``app/tasks/indexing.py`` 与 ``app/celery_app.py`` 一行未改，
接线是 #132 的事（接线方式写在 ADR-0023「#132 的接线要求」一节）。

## 为什么不是「每任务一个循环」

异步引擎的连接池**绑在创建连接的那个事件循环上**。改造前 Worker 每个任务
``asyncio.run`` 一次（一个循环、跑完即关），于是：

* **happy path 看起来是对的**（实测，见 ADR-0023 读数表）：psycopg 3.2 的异步连接并不把
  loop 记在自己身上，跨 loop 复用一条连接在查询路径上**能成功**——所以「它坏了」这件事
  不会在第一次索引时暴露；
* **代价落在关停与异常路径**：池里的连接由最后一个任务的循环持有，那个循环已经关了，
  ``dispose()`` 没人 await，进程退出时 SQLAlchemy 只能把连接**丢掉**并刷一条
  ``SAWarning: ... cannot be safely terminated``；被取消的任务还会把池里的槽位永远标成
  checked out（``pool_size`` 个之后，下一个任务卡在 ``pool_timeout`` 上超时）。

「能跑」与「能确定地释放」是两件事，后者才是这条流水线要的。

## 为什么是**一个**循环而不是像检索执行池那样 N 个

``app/core/loop_pool.py`` 的池（**已在 #127 删除**，见 ADR-0022 的修订）是为「同步查询会卡住循环」准备的：那条同步 pgvector 查询
必须被关进线程，且要并发 N 路。索引任务不需要——Celery prefork 每个子进程同一时刻只跑
一个任务，并发由进程数给，不由循环数给。多开循环只会把连接池变成 N 份，连接预算跟着
乘 N。

## 为什么投递的是**工厂函数**而不是协程对象

协程对象在创建它的线程里就绑定了一批上下文（等待中的 Future / 锁 / 客户端都只属于某个
循环）。工厂在**常驻循环里**被调用，协程从一开始就属于那个循环——这与已经删除的
``app/core/loop_pool.py`` 当年的投递约定是同一条纪律。

## Windows 上为什么要单独造循环

psycopg 的异步模式**不支持** Windows 缺省的 ``ProactorEventLoop``，会当场抛
``InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode``。
容器里是 Linux（缺省即 Selector），本机开发 / 调试才会走到这一支。这条不是「顺手加个
兼容」：没有它，本机跑异步 Worker 的第一条查询就炸，而 CI 是绿的。

## 关停顺序（``shutdown``）

**在循环还活着的时候**收掉它名下的一切，然后才停循环：

1. 停止接收（之后的 ``run`` 立刻 ``RuntimeError``，不排队、不挂死）；
2. 在这个循环上 ``await engine.dispose()``（连接在这里才关得掉）；
3. 在这个循环上关掉它名下的 HTTP 客户端（``aclose_http_client``）；
4. 停循环 → join 线程 → 线程的 ``finally`` 里关循环。

重复调用幂等；每一步失败只记 warning——收尾阶段抛异常会盖掉真正的退出原因。
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.http_clients import aclose_http_client

logger = structlog.get_logger(__name__)

T = TypeVar("T")

#: 线程起来到循环可用的等待上限。起不来是部署问题，宁可当场失败也不要静默排队。
START_TIMEOUT_SECONDS = 5.0

#: 关停时每一步的宽限。卡在同步调用里的循环等不到，只记 warning。
SHUTDOWN_GRACE_SECONDS = 5.0


def new_worker_loop() -> asyncio.AbstractEventLoop:
    """造一个能跑 psycopg 异步驱动的循环（见模块文档「Windows 上为什么要单独造循环」）。"""

    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


class WorkerLoop:
    """一条常驻线程 + 一个常驻循环 + 它名下引擎与客户端的生命周期。"""

    def __init__(
        self, *, name: str = "celery-worker-loop", engine: AsyncEngine | None = None
    ) -> None:
        self._name = name
        #: 注入的引擎（测试用替身 / 探针）；``None`` 表示用进程级那一个（见 ``engine()``）。
        self._engine = engine
        self._loop = new_worker_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._ready = threading.Event()
        self._closed = False
        self._started = False
        self._start_lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def closed(self) -> bool:
        """已经关停（不再接收任务，资源已释放或正在释放）。"""

        return self._closed

    @property
    def alive(self) -> bool:
        """循环线程还活着。关停之后必须是 ``False``——线程没死等于没关。"""

        return self._thread.is_alive()

    def start(self) -> None:
        """起线程（幂等）。Worker 进程启动时调一次，让「起不来」在启动阶段就暴露。"""

        with self._start_lock:
            if self._closed:
                raise RuntimeError("worker loop is closed")
            if self._thread.is_alive():
                return
            self._thread.start()
            self._started = True
            if not self._ready.wait(START_TIMEOUT_SECONDS):
                raise RuntimeError(f"worker loop {self._name!r} did not start in time")

    def _run(self) -> None:
        """线程体：跑循环直到 ``shutdown`` 叫停；无论如何都关掉循环。"""

        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            self._drain_pending()
            self._loop.close()

    def _drain_pending(self) -> None:
        """循环停下来之后取消没跑完的任务；失败只记 warning（关停路径不抛）。"""

        try:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:  # noqa: BLE001 - 关停路径不抛，见模块文档
            logger.warning("worker_loop_drain_failed", loop=self._name, exc_info=True)

    # ── 投递 ────────────────────────────────────────────────────

    def run(self, factory: Callable[[], Awaitable[T]], *, timeout: float | None = None) -> T:
        """在常驻循环上跑一个协程并等结果；异常原样抛回调用方。

        ``factory`` 是**工厂**而不是协程对象（见模块文档）。``timeout`` 只给探针与收尾
        路径用：真实任务的预算由 Celery（``soft_time_limit``）与各 Provider 自己兜，
        不在这里再定一个数字。
        """

        if self._closed:
            raise RuntimeError("worker loop is closed")
        self.start()
        future = asyncio.run_coroutine_threadsafe(self._invoke(factory), self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"worker loop task exceeded {timeout}s") from None

    @staticmethod
    async def _invoke(factory: Callable[[], Awaitable[T]]) -> T:
        """在循环里调工厂——协程从这一刻起属于这个循环。"""

        return await factory()

    # ── 引擎 ────────────────────────────────────────────────────

    def engine(self) -> AsyncEngine:
        """当前进程该用的异步引擎：缺省是进程级那一个（一个进程一套池）。

        引擎本身可以在任何线程取到（它惰性连接）；真正绑循环的是**连接**，所以第一次
        ``connect`` 必须发生在 ``run`` 里面。局部 import 是为了不让 import 本模块就
        建引擎（与 ``app.db.session_factory`` 同一个理由）。
        """

        if self._engine is None:
            from app.db.async_session import async_engine

            self._engine = async_engine
        return self._engine

    # ── 关停 ────────────────────────────────────────────────────

    def shutdown(self, *, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """确定性释放（顺序见模块文档）；幂等，不抛。"""

        if self._closed:
            return
        self._closed = True
        self._release_on_loop(grace=grace)
        self._stop_loop()
        if self._started:
            self._thread.join(grace)
        if self._thread.is_alive():
            logger.warning("worker_loop_thread_did_not_stop", loop=self._name)

    def _release_on_loop(self, *, grace: float) -> None:
        """在**循环还活着的时候** dispose 引擎、关掉它名下的 HTTP 客户端。"""

        if not self._thread.is_alive() or self._loop.is_closed():
            return
        if threading.current_thread() is self._thread:
            # 在循环线程里调 shutdown（任务自己要求关停）：同步等自己会死锁。
            logger.warning("worker_loop_shutdown_from_its_own_loop", loop=self._name)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._release(), self._loop)
            future.result(timeout=grace)
        except Exception:  # noqa: BLE001 - 关停路径不抛，见模块文档
            logger.warning("worker_loop_release_failed", loop=self._name, exc_info=True)

    async def _release(self) -> None:
        """收掉这个循环名下的一切；每一步失败只记 warning。"""

        engine = self._engine
        if engine is not None:
            # 观测（ADR-0023「连接数有上限、有观测」）：关停前把池的读数记一行。上限是配置
            # （``pool_size + max_overflow``），这里是它到底用掉了多少；泄漏的样子就是
            # 关停时 ``checked_out`` 还不为 0（被放弃的连接连 dispose 都救不回来）。
            pool = getattr(engine, "pool", None)
            if pool is not None:
                try:
                    logger.info(
                        "worker_loop_pool_state",
                        loop=self._name,
                        pool_size=pool.size(),
                        checked_out=pool.checkedout(),
                    )
                except Exception:  # noqa: BLE001 - 观测失败不许挡住关停
                    logger.warning(
                        "worker_loop_pool_state_failed", loop=self._name, exc_info=True
                    )
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 - 关停路径不抛
                logger.warning("worker_loop_engine_dispose_failed", loop=self._name, exc_info=True)
        try:
            await aclose_http_client()
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("worker_loop_close_clients_failed", loop=self._name, exc_info=True)

    def _stop_loop(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:  # 循环已经停了
            return


_loop: WorkerLoop | None = None
_lock = threading.Lock()


def get_worker_loop() -> WorkerLoop:
    """进程级单例：Worker 进程里所有任务共用的那个常驻循环。

    ``worker_process_init`` 里调一次可以让「起不来」在 Worker 启动阶段暴露；任务体里调
    也安全（第一次 ``run`` 会惰性起线程）。
    """

    global _loop
    with _lock:
        if _loop is None or _loop.closed:
            _loop = WorkerLoop()
        return _loop


def shutdown_worker_loop(*, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """关掉单例并释放它名下的一切（``worker_process_shutdown`` / ``atexit`` 用）。"""

    global _loop
    with _lock:
        loop, _loop = _loop, None
    if loop is not None:
        loop.shutdown(grace=grace)


def reset_worker_loop() -> None:
    """丢掉单例并同步关停（测试清场用；与 ``shutdown_worker_loop`` 等价，名字区分意图）。"""

    shutdown_worker_loop()


# 没有 lifespan、也没有接 Celery 信号的进程（脚本 / 排障）靠它兜底：daemon 线程不会拖住
# 解释器退出，但**连接会不会被关**取决于这里有没有跑过一次 dispose。
atexit.register(shutdown_worker_loop)

__all__ = [
    "SHUTDOWN_GRACE_SECONDS",
    "START_TIMEOUT_SECONDS",
    "WorkerLoop",
    "get_worker_loop",
    "new_worker_loop",
    "reset_worker_loop",
    "shutdown_worker_loop",
]
