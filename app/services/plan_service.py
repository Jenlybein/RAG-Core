"""套餐解析：判定内核在热路径上要的那一份「这个租户买的是哪一档」。

读路径走缓存（按 ``plan_id``），写路径不在这里——改套餐的那张票负责「落库 → 删缓存 →
写审计」，这里只提供 ``resolve_plan`` 与 ``invalidate_plan`` 两个动作，让那一票有唯一
可调用的收口点，而不是自己 ``cache.delete`` 一次。

为什么套餐要缓存：ADR-0018 决定「运行时只复核套餐」，于是每一次调用都要读一次套餐行；
用按 id 的短期缓存兜住这次读，同时保留「管理员把套餐调低，下一次调用就按新套餐判」的
语义——靠的是改完**删缓存**，不是等过期。

缓存未命中时读库失败（行不存在）映射成 ``20017``：一个 ``plan_id`` 指向不存在的套餐是
数据被手工改坏的征兆，报「套餐不存在」比让后续每一道关卡拿着 ``None`` 崩掉更好查。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache, RedisPlanCache
from app.core.redis_client import get_redis_client
from app.models.plan import PlanSpec, plan_spec_of
from app.repositories import plan_repository


def default_plan_cache() -> PlanCache:
    """生产用的缓存实现：Redis（多 worker 才一致，进程内字典不行）。

    每次调用返回一个新的包装对象，但它只握着进程内唯一的那个客户端——构造是廉价的，
    真正的连接池在 :func:`app.core.redis_client.get_redis_client` 里。
    """

    return RedisPlanCache(get_redis_client())


def resolve_plan(db: Session, plan_id: int, *, cache: PlanCache | None = None) -> PlanSpec:
    """按 ``plan_id`` 取套餐快照：先缓存、后库，未命中时回填。

    ``cache`` 缺省取 Redis 实现。测试传内存假实现，于是「命中缓存时不读库」「未命中时
    读一次库并回填」都能被直接断言，不需要真 Redis。
    """

    plan_cache = default_plan_cache() if cache is None else cache

    cached = plan_cache.get(plan_id)
    if cached is not None:
        return cached

    row = plan_repository.get_by_id(db, plan_id)
    if row is None:
        raise AppError(ErrorCode.PLAN_NOT_FOUND, f"plan not found: {plan_id}")

    spec = plan_spec_of(row)
    plan_cache.set(plan_id, spec)
    return spec


def invalidate_plan(cache: PlanCache, plan_id: int) -> None:
    """改完套餐调它：**删**掉缓存项，不写回新值（ADR-0017）。

    参数顺序是「缓存在前」：调用方在改套餐的写入口里手里已经有缓存对象，而 plan_id 是
    刚被写过的那一行的主键；这样写出来读作「让这份缓存忘掉这一档」。
    """

    cache.delete(plan_id)
