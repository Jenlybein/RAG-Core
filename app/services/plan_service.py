"""套餐解析：判定内核在热路径上要的那一份「这个租户买的是哪一档」。

读路径走缓存（按 ``plan_id``），写路径不在这里——改套餐的那张票负责「落库 → 删缓存 →
写审计」，这里只提供 ``resolve_plan`` 与 ``invalidate_plan`` 两个动作，让那一票有唯一
可调用的收口点，而不是自己 ``cache.delete`` 一次。

为什么套餐要缓存：ADR-0018 决定「运行时只复核套餐」，于是每一次调用都要读一次套餐行；
用按 id 的短期缓存兜住这次读，同时保留「管理员把套餐调低，下一次调用就按新套餐判」的
语义——靠的是改完**删缓存**，不是等过期。

缓存未命中时读库失败（行不存在）映射成 ``20017``：一个 ``plan_id`` 指向不存在的套餐是
数据被手工改坏的征兆，报「套餐不存在」比让后续每一道关卡拿着 ``None`` 崩掉更好查。

## 两个入口：``await`` 的那个与过渡桥（票 #129）

套餐缓存改成了 ``redis.asyncio``（见 :mod:`app.core.plan_cache`），所以解析本身是
:func:`resolve_plan`（``async``）。异步调用方直接 ``await``；**同步上下文**里的调用方
（判定内核 :func:`app.core.auth.resolve_identity` 这个同步依赖、``console_kb_service``
里那条同步的重建索引路径、改套餐提交后删缓存的回调）走
:func:`resolve_plan_blocking` / :func:`invalidate_plan_blocking`。

那两个桥**不是第二套实现**：它们只是把同一个协程交给事件循环跑完再取结果
（:func:`app.core.sync_bridge.run_blocking`），业务规则仍然只有一份。它们随 #127 / #130
把剩下的同步调用点搬完一起删除——判据与完整理由写在 ``app/core/sync_bridge.py``。

**套餐行的读仍然是同步的**（``db`` 是同步 ``Session``）：本票只把 Redis 那一半换成异步，
会话的迁移是 #130 / #127 的事。为了不让这次读落在事件循环上，它被
:func:`asyncio.to_thread` 挪进线程池——「谁在等」变了，「谁在读库」没变。
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache, RedisPlanCache
from app.core.redis_client import get_redis_client
from app.core.sync_bridge import run_blocking
from app.models.plan import PlanSpec, plan_spec_of
from app.repositories import plan_repository

#: 进程级可注入的默认缓存；``None`` 表示「按生产实现现构造」。
_default_cache: PlanCache | None = None


def default_plan_cache() -> PlanCache:
    """生产用的缓存实现：Redis（多 worker 才一致，进程内字典不行）。

    每次调用返回一个新的包装对象，但它只握着进程内唯一的那个客户端——构造是廉价的，
    真正的连接池在 :func:`app.core.redis_client.get_redis_client` 里。**异步客户端**：
    套餐解析的调用点里已经有 ``async def``（控制面读、检索链路），同步 Redis 会在事件
    循环上按次阻塞（票 #129）。

    进程级注入点（:func:`set_default_plan_cache`）优先：离线套件因此不碰真 Redis，而
    「本机恰好起着一个 Redis」不再能改变用例的结论——与 ``runtime_settings_cache`` 同一
    套做法、同一个理由（见那边的模块文档）。
    """

    if _default_cache is not None:
        return _default_cache
    return RedisPlanCache(get_redis_client())


def set_default_plan_cache(cache: PlanCache | None) -> None:
    """注入/复位进程级默认缓存（测试用内存假实现；生产复位成 ``None`` 即回到 Redis）。

    为什么需要它：``resolve_plan`` 的 ``cache`` 参数缺省时走这里，而**登录 / 我的视图**
    那条路径正是这么调的。没有这个注入点时，一条与 Redis 无关的用例只要经过登录就会去
    连真 Redis——本机开着 Redis 就绿、没开就红，而红了还看着像「登录坏了」。
    """

    global _default_cache
    _default_cache = cache


async def resolve_plan(db: Session, plan_id: int, *, cache: PlanCache | None = None) -> PlanSpec:
    """按 ``plan_id`` 取套餐快照：先缓存、后库，未命中时回填。

    ``cache`` 缺省取 Redis 实现。测试传内存假实现，于是「命中缓存时不读库」「未命中时
    读一次库并回填」都能被直接断言，不需要真 Redis。

    命中缓存时**一次数据库都不碰**（``to_thread`` 只在未命中时才发生）：这条路径在每一次
    数据面调用的热路径上，省下的那次库读就是它存在的理由。
    """

    plan_cache = default_plan_cache() if cache is None else cache

    cached = await plan_cache.get(plan_id)
    if cached is not None:
        return cached

    # 同步会话读库：本票没搬会话（见模块文档最后一节），所以把它挪出事件循环。
    row = await asyncio.to_thread(plan_repository.get_by_id, db, plan_id)
    if row is None:
        raise AppError(ErrorCode.PLAN_NOT_FOUND, f"plan not found: {plan_id}")

    spec = plan_spec_of(row)
    await plan_cache.set(plan_id, spec)
    return spec


async def invalidate_plan(cache: PlanCache, plan_id: int) -> None:
    """改完套餐调它：**删**掉缓存项，不写回新值（ADR-0017）。

    参数顺序是「缓存在前」：调用方在改套餐的写入口里手里已经有缓存对象，而 plan_id 是
    刚被写过的那一行的主键；这样写出来读作「让这份缓存忘掉这一档」。
    """

    await cache.delete(plan_id)


# ─────────────────────────────────────────── 同步上下文的过渡桥（票 #129，随 #127/#130 删除）


def resolve_plan_blocking(
    db: Session, plan_id: int, *, cache: PlanCache | None = None
) -> PlanSpec:
    """同步上下文里的套餐解析：把 :func:`resolve_plan` 交给事件循环跑完再取结果。

    两个调用方，都在**同步**函数里：判定内核 ``resolve_identity``（同步 FastAPI 依赖，
    跑在 anyio 工作线程上）与 ``console_kb_service`` 里那条同步的重建索引路径。它们不该
    为了一个缓存读变成 ``async``——那会把 ``async`` 传染进数据面的整棵依赖树，而那正是
    #127 的活。
    """

    return run_blocking(resolve_plan(db, plan_id, cache=cache))


def invalidate_plan_blocking(cache: PlanCache, plan_id: int) -> None:
    """同步上下文里删套餐缓存（改套餐的提交后回调）；理由见 :func:`resolve_plan_blocking`。"""

    run_blocking(invalidate_plan(cache, plan_id))


__all__ = [
    "default_plan_cache",
    "invalidate_plan",
    "invalidate_plan_blocking",
    "resolve_plan",
    "resolve_plan_blocking",
    "set_default_plan_cache",
]
