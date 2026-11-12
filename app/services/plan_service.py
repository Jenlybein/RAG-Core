"""套餐解析：判定内核在热路径上要的那一份「这个租户买的是哪一档」。

读路径走缓存（按 ``plan_id``），写路径不在这里——改套餐的那张票负责「落库 → 删缓存 →
写审计」，这里只提供 ``resolve_plan`` 与 ``invalidate_plan`` 两个动作，让那一票有唯一
可调用的收口点，而不是自己 ``cache.delete`` 一次。

为什么套餐要缓存：ADR-0018 决定「运行时只复核套餐」，于是每一次调用都要读一次套餐行；
用按 id 的短期缓存兜住这次读，同时保留「管理员把套餐调低，下一次调用就按新套餐判」的
语义——靠的是改完**删缓存**，不是等过期。

缓存未命中时读库失败（行不存在）映射成 ``20017``：一个 ``plan_id`` 指向不存在的套餐是
数据被手工改坏的征兆，报「套餐不存在」比让后续每一道关卡拿着 ``None`` 崩掉更好查。

**两个动作都是 ``async``，且只有一份**（第 11 步票 #129 / #130）：缓存背后是
``redis.asyncio``（见 :mod:`app.core.plan_cache`），套餐行读的是 ``AsyncSession``——
判定内核、控制面、检索链路与 Celery 侧调用方全部在事件循环上 ``await``。迁移期那两个
「在同步上下文里把协程跑完」的过渡桥（``resolve_plan_blocking`` / ``invalidate_plan_blocking``）
随最后一个同步调用点一起删掉了：它们存在的唯一理由是「有函数还不是 ``async``」，
而这个理由在票 #130 之后不再成立。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache, RedisPlanCache
from app.core.redis_client import get_redis_client
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


async def resolve_plan(
    db: AsyncSession, plan_id: int, *, cache: PlanCache | None = None
) -> PlanSpec:
    """按 ``plan_id`` 取套餐快照：先缓存、后库，未命中时回填。

    ``cache`` 缺省取 Redis 实现。测试传内存假实现，于是「命中缓存时不读库」「未命中时
    读一次库并回填」都能被直接断言，不需要真 Redis。

    命中缓存时**一次数据库都不碰**：这条路径在每一次数据面调用的热路径上，省下的那次
    库读就是它存在的理由。未命中时那次读也是 ``await``（异步会话），不再借道线程池。
    """

    plan_cache = default_plan_cache() if cache is None else cache

    cached = await plan_cache.get(plan_id)
    if cached is not None:
        return cached

    row = await plan_repository.get_by_id_async(db, plan_id)
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


__all__ = [
    "default_plan_cache",
    "invalidate_plan",
    "resolve_plan",
    "set_default_plan_cache",
]
