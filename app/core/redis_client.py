"""Redis 客户端的**唯一**构造点。

两条硬要求：

1. **不在 import 时连库**。``Redis.from_url`` 本身是惰性的（第一次发命令才建连接），
   但「模块一被 import 就构造客户端」仍然会让一个不关心 Redis 的测试（或一条只读的
   运维脚本）莫名其妙地持有一个指向 ``localhost:6379`` 的连接对象。这里把构造推迟到
   第一次真正需要它的时候。
2. **可以被测试换掉，而不是靠 monkeypatch 全局**。``set_redis_client`` / ``reset_redis_client``
   是这个模块的公开接口：注入一个假客户端、用完复位，比在每个测试里
   ``monkeypatch.setattr("app.core.redis_client._client", ...)`` 更不容易漏、也不依赖
   私有名字。

其余模块一律只调 :func:`get_redis_client`，不自己 ``Redis.from_url(...)``——否则「连的是
哪个库」这件事会散落在若干个文件里，而它们迟早会指向不同的库。

## 两个客户端：异步是主，同步只剩一个用户（票 #133 → #129）

:func:`get_redis_client` 给的是 **``redis.asyncio`` 的客户端**：配额、限流、登录节流与
套餐缓存都在异步链路上（``gated_retrieve`` 是 ``async def``，登录路由也是），同步 Redis
会把整个事件循环按住——一次慢 Redis 于是变成所有并发请求一起慢。

:func:`get_sync_redis_client` 是**同步上下文专用的第二个客户端**，票 #129 之后只剩一个用户：

* 运行时配置缓存（:mod:`app.core.runtime_settings_cache`）：刷新发生在请求中间件里
  （``run_in_threadpool``），Celery 任务（``app.tasks.indexing``）也直接同步调用它——
  两条都不在事件循环上，所以同步 Redis 在那里不会阻塞任何请求。

套餐缓存（:mod:`app.core.plan_cache`）在 #129 就改成了异步；它的全部调用方（判定内核、
控制面、检索链路）自 #130 起都是 ``await``，迁移期那个把协程交回主循环的过渡桥也随最后
一个同步调用点删掉了。

**两个客户端长期并存，各管各的上下文**：`async def` 里的缓存操作一律用
:func:`get_redis_client`（``redis.asyncio``）；`run_in_threadpool` 里的覆盖层刷新、Worker
独立任务、启动期装载这类**不在事件循环上**的同步路径用
:func:`get_sync_redis_client`。`RuntimeSettingsCache` 因此有同步的 `get`/`set`/`delete`
与异步的 `adelete` 两个面，指向同一份键（见 `app.core.runtime_settings_cache`）。
判据只有一句：「这个函数是不是 `async def`」——是，就必须用异步客户端。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings

#: 进程内单例（异步）。这里存的是**客户端对象**，不是连接：redis-py 自己管连接池。
_client: Any | None = None

#: 进程内单例（同步，过渡期用；见模块文档「两个客户端」）。
_sync_client: Any | None = None


def get_redis_client() -> Any:
    """取（必要时构造）进程内唯一的**异步** Redis 客户端；此时**不**发任何命令。"""

    global _client
    if _client is None:
        # 延迟到真正要用时才 import 并构造：见模块文档第 1 条。
        from redis.asyncio import Redis

        _client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def set_redis_client(client: Any | None) -> None:
    """注入一个客户端（测试用假实现，生产可换成连到另一个库的实例）。

    注入的形状是**异步**的（``async def get / incr / expire / delete / setex``）：注入缝
    认的就是生产客户端的形状，同步假实现会让「忘了 await」在套件里看不见。
    """

    global _client
    _client = client


def reset_redis_client() -> None:
    """复位成「还没构造」的状态；下一次 ``get_redis_client`` 会按当前配置重建。"""

    set_redis_client(None)


def get_sync_redis_client() -> Any:
    """取（必要时构造）进程内唯一的**同步** Redis 客户端（过渡期，见模块文档）。

    票 #129 之后只剩运行时配置缓存一个用户，理由与删除条件都写在模块文档里。
    """

    global _sync_client
    if _sync_client is None:
        from redis import Redis

        _sync_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _sync_client


def set_sync_redis_client(client: Any | None) -> None:
    """注入一个同步客户端（测试用假实现；名字里带 ``sync`` 是为了不跟主缝混淆）。"""

    global _sync_client
    _sync_client = client


def reset_sync_redis_client() -> None:
    """复位同步客户端；下一次 ``get_sync_redis_client`` 会按当前配置重建。"""

    set_sync_redis_client(None)
