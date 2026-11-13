"""运行时配置覆盖层的缓存接缝（形态照抄 ``app/core/plan_cache.py``）。

缓存里存的是**覆盖层的原始行**（``{键: 值}``），不是合并后的 ``Settings``：合并结果里
有几十个字段，而真正会变的只有被覆盖的那几行；存原始行还让「库里写了个非法值」这件事
在缓存里也保持可见（每次从缓存读出来都要重新过一遍校验，非法行照样被丢弃并标红）。

**失效一律靠删，不靠写**（ADR-0017 同一条纪律）：写缓存要求每一处修改都记得同步，漏
一处就留下一个「看起来更新过、实际是旧值」的条目；删则让下一次读自然拿到新值。

**为什么缓存对象是「进程级可注入」而不是只在 FastAPI 依赖里注入**：刷新发生在**请求
中间件**里，中间件拿不到 ``Depends``。若允许生产构造点与测试注入点各走一条路，中间件
那条会永远连真 Redis —— 于是「写后 30 秒内收敛」这条行为在测试里根本没有被覆盖。
一个进程级单例（:func:`get_settings_cache` / :func:`set_settings_cache`）让依赖与中间件
看到同一个实现，与 ``app.core.redis_client`` 的注入方式同一套做法。
"""

from __future__ import annotations

import json
from typing import Any, Final, Protocol

#: 缓存键。前缀独立：套餐缓存（``plan:*``）、限流与日配额（``quota:*`` 等）各有自己的
#: 键空间，混用会让「清错一类」变成一次谁也解释不清的配置回退。
CACHE_KEY: Final[str] = "runtime_settings:v1"

#: 缓存有效期的**唯一来源**（30 秒）。它同时就是面板上「各进程 30s 内生效」那句话的
#: 依据：超过这个窗口，任何进程都会自己重新读一遍库。
CACHE_TTL_SECONDS: Final[int] = 30

Overrides = dict[str, Any]


class ExpiringKeyValueClient(Protocol):
    """redis-py 里本缓存真正用到的那三条命令；不 import redis 包，测试塞字典替身即可。"""

    def get(self, key: str) -> str | bytes | None: ...

    def setex(self, key: str, ttl: int, value: str) -> Any: ...

    def delete(self, key: str) -> Any: ...


class RuntimeSettingsCache(Protocol):
    """覆盖层缓存的契约：整张表一份快照，三个方法，不接受第四个。

    **为什么 ``delete`` 有两个面**：刷新覆盖层这件事有两个调用上下文——中间件的
    ``run_in_threadpool`` 与 Celery 任务在**同步**上下文里（用 ``delete``），控制面写入
    的 ``after_commit`` 回调在**事件循环**上（用 ``adelete``）。同一个键、同一份快照，
    只有「谁在等」不同。同步 Redis 出现在循环上会卡住整条请求链（票 #133 的验收），
    所以两个面都要有；等中间件与 Celery 那一侧也搬进常驻循环（票 #137）之后，
    ``delete`` 那一条随同步缝一起删。
    """

    def get(self) -> Overrides | None: ...

    def set(self, overrides: Overrides) -> None: ...

    def delete(self) -> None: ...

    async def adelete(self) -> None: ...


class RedisRuntimeSettingsCache:
    """生产实现：Redis 里存一份原始覆盖行的 JSON 快照，TTL 只是兜底（真正生效靠删）。"""

    def __init__(
        self,
        client: ExpiringKeyValueClient,
        *,
        ttl_seconds: int | None = None,
        async_client: Any = None,
    ) -> None:
        self._client = client
        self._ttl = CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        # 异步面缺省惰性取同一个 Redis：两条客户端指向同一份键，谁写谁删都看得见。
        self._async_client = async_client

    def get(self) -> Overrides | None:
        raw = self._client.get(CACHE_KEY)
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(text)
        # 缓存里存的必须是一个「键 → 值」的对象；形状不对就当没缓存，让读路径回库。
        # （缓存是纯加速层，它坏了可以慢，不能把一份看不懂的数据合进 Settings。）
        return payload if isinstance(payload, dict) else None

    def set(self, overrides: Overrides) -> None:
        self._client.setex(CACHE_KEY, self._ttl, json.dumps(overrides))

    def delete(self) -> None:
        self._client.delete(CACHE_KEY)

    async def adelete(self) -> None:
        """循环上的那一条：走 ``redis.asyncio`` 客户端，不用同步客户端。"""

        if self._async_client is None:
            from app.core.redis_client import get_redis_client

            self._async_client = get_redis_client()
        await self._async_client.delete(CACHE_KEY)


#: 进程内可注入的缓存实例。``None`` 表示还没注入过，按生产实现现构造。
_cache: RuntimeSettingsCache | None = None


def get_settings_cache() -> RuntimeSettingsCache:
    """取（必要时构造）进程内唯一的缓存实现。

    Redis 客户端本身是惰性的（见 ``app.core.redis_client``），所以这里构造缓存对象
    不会连库；真正的连接要等第一次 ``get``。

    **用同步缝**（票 #133；#129 之后它是同步客户端的**唯一**用户）：刷新的三个调用点都在
    同步上下文里——中间件把它丢进 ``run_in_threadpool``、Celery 任务直接同步调用、控制台
    依赖走同步路由。同步 Redis 在这里因此不在事件循环上跑；等这些路径搬进常驻事件循环
    （#131）之后改回异步缝，完整理由见 ``app.core.redis_client`` 的模块文档。
    """

    global _cache
    if _cache is None:
        # 延迟 import：只有真正走生产实现时才需要 redis 那一条依赖链。
        from app.core.redis_client import get_redis_client, get_sync_redis_client

        _cache = RedisRuntimeSettingsCache(
            get_sync_redis_client(), async_client=get_redis_client()
        )
    return _cache


def set_settings_cache(cache: RuntimeSettingsCache | None) -> None:
    """注入一份缓存实现（测试用内存假实现；生产可换成连到另一个库的实例）。"""

    global _cache
    _cache = cache


def reset_settings_cache() -> None:
    """复位成「还没注入」；下一次 :func:`get_settings_cache` 按生产实现重建。"""

    set_settings_cache(None)
