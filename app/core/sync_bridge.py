"""同步代码调用异步代码的**唯一**过渡桥（票 #129）。

## 为什么需要它

第 11 步把数据库与 Redis 换成异步栈，按垂直切片推进：一条请求路径搬完之后，**别的路径
仍然是同步的**。于是「同步上下文里的函数要调一个刚变成 ``async`` 的函数」这件事在迁移期
真实存在，而且不是随便挑个写法就能糊过去：

* ``asyncio.run(coro)`` 会**新起一个事件循环**。对数据库连接池是浪费，对 ``redis.asyncio``
  的客户端是错的——那个客户端绑在**创建它的**循环上，从第二个循环里用会当场
  ``RuntimeError: Event loop is closed`` 或悄悄串包（ADR-0009 / ADR-0022 已经为同一类
  问题付过两次学费）；
* 直接 ``run_until_complete`` 在「当前线程就是这个循环」时会**死锁**；
* 同步 FastAPI 依赖跑在 anyio 的工作线程里，那个线程恰好握着一个回到主循环的令牌——
  这正是 :func:`anyio.from_thread.run` 存在的理由。

所以：**先试 anyio 的工作线程令牌**（FastAPI 同步依赖与同步端点、线程池里的刷新中间件都
走这条），它把协程交回**主事件循环**执行、阻塞当前工作线程直到有结果——异步 Redis 客户端
因此始终在同一个循环上。拿不到令牌时（纯线程：运维脚本、Celery 任务里手动开的线程）才
退回 ``asyncio.run``，并因此拿到一个**自己的**循环：那种场合不要跨调用共享异步客户端。

## 它什么时候消失

它不是一层设计，是一次迁移的脚手架。等 #127（检索链路）、#130（控制面写）、#131（入库
与索引）把剩下的同步调用点搬完，所有调用方都在事件循环上 ``await``，这个模块连同它的
调用点（``plan_service.resolve_plan_blocking`` / ``invalidate_plan_blocking``）一起删掉。
**在那之前不许有新的调用点**：判据是「这个调用点所在的函数是不是 ``async def``」——
是，就必须直接 ``await``。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_blocking(coro: Coroutine[Any, Any, T]) -> T:
    """在**同步**上下文里跑一个协程并等它的结果（见模块文档）。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # 当前线程自己就是这个循环：再等下去就是死锁。把协程关掉（否则它永远不被 await，
        # 套件里会多一条 ``coroutine was never awaited`` 的噪音），然后明确报错。
        coro.close()
        raise RuntimeError(
            "run_blocking 不能在事件循环线程上调用：这条路径已经是异步的，请直接 await"
        )

    try:
        import anyio
    except ImportError:  # pragma: no cover - anyio 随 FastAPI/Starlette 一起装
        return asyncio.run(coro)

    try:
        return anyio.from_thread.run(lambda: coro)
    except anyio.NoEventLoopError:
        # 纯线程（没有 anyio 的循环令牌）：自建一个循环跑完即关。
        return asyncio.run(coro)


__all__ = ["run_blocking"]
