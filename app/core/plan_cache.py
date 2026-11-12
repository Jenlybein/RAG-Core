"""套餐缓存的接缝：只有 ``get`` / ``set`` / ``delete`` 三个方法。

为什么要有这个接口：套餐解析在每一次调用的热路径上（ADR-0018「运行时只复核套餐」），
读一次 Redis 比读一次库便宜，而「少读一次库」这件事又必须在测试里看得见——所以缓存
做成注入的，测试塞一个内存假实现就能断言「命中缓存时一次数据库都没碰」，不必连真 Redis
也不必 monkeypatch 全局。

**缓存键只由 ``plan_id`` 组成，不含 ``code``。** 管理员改一个套餐的 code（``pro`` →
``pro-max``）时，若键里带 code，旧键会整批变成孤儿：它们既不会被读到，也不会在删缓存
时被删掉，只能等过期；而键只认 id，改名就是一次纯展示层改动。

**失效一律靠删，不靠写。** 写缓存要求每一处修改都记得同步，漏一处就留下一个「看起来
更新过、实际是旧值」的条目；删则让下一次读自然拿到新值，漏不掉。

**三个方法都是 ``async``（票 #129）。** 它们背后是 ``redis.asyncio`` 的客户端
（:func:`app.core.redis_client.get_redis_client`）：同步 Redis 会在事件循环上按秒计地
阻塞，而套餐解析在**每一次调用**的热路径上。改成异步之后，同步 Redis 客户端只剩运行时
配置覆盖层一个用户（它跑在 Celery 与线程池里，见 ``app/core/redis_client``）。

调用方因此分两类：异步上下文 ``await``（控制面读、检索链路）；同步上下文（判定内核
:func:`app.core.auth.resolve_identity`、同步服务函数）走
:func:`app.services.plan_service.resolve_plan_blocking` 那个显式过渡桥，理由与删除条件
写在那个函数上。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Final, Protocol

from app.core.config import settings
from app.models.plan import PlanSpec, plan_spec_from_mapping

#: 缓存键前缀；与 Celery 的键空间不冲突（它们不在这个库里，也各有各的前缀）。
KEY_PREFIX: Final[str] = "plan:"


def cache_key(plan_id: int) -> str:
    """套餐缓存的键：**只有 id**，理由见模块文档。"""

    return f"{KEY_PREFIX}{plan_id}"


class ExpiringKeyValueClient(Protocol):
    """``redis.asyncio`` 里套餐缓存真正用到的那三条命令。

    刻意不写成 ``Redis``：这个模块因此不 import redis 包，测试传一个字典伪装的对象就够，
    也不会有人顺手在缓存实现里加一条只有真 Redis 才认的命令。
    """

    async def get(self, key: str) -> str | bytes | None: ...

    async def setex(self, key: str, ttl: int, value: str) -> Any: ...

    async def delete(self, key: str) -> Any: ...


class PlanCache(Protocol):
    """判定内核与套餐服务之间的缓存契约：三个方法，不接受第四个。"""

    async def get(self, plan_id: int) -> PlanSpec | None: ...

    async def set(self, plan_id: int, plan: PlanSpec) -> None: ...

    async def delete(self, plan_id: int) -> None: ...


class RedisPlanCache:
    """生产实现：Redis 里存一份 JSON 快照，短期过期只是兜底（真正生效靠删）。"""

    def __init__(
        self,
        client: ExpiringKeyValueClient,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._ttl = settings.PLAN_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds

    async def get(self, plan_id: int) -> PlanSpec | None:
        raw = await self._client.get(cache_key(plan_id))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return _from_payload(json.loads(text))

    async def set(self, plan_id: int, plan: PlanSpec) -> None:
        await self._client.setex(cache_key(plan_id), self._ttl, json.dumps(_to_payload(plan)))

    async def delete(self, plan_id: int) -> None:
        await self._client.delete(cache_key(plan_id))


def _to_payload(plan: PlanSpec) -> dict[str, Any]:
    """快照 → 可 JSON 化的字典；``allowed_profiles`` 的元组由 ``json`` 自然写成数组。"""

    return asdict(plan)


def _from_payload(payload: dict[str, Any]) -> PlanSpec:
    """字典 → 快照：列清单来自 :data:`PLAN_SPEC_COLUMNS`，缺一列就是 ``KeyError``。

    转换只有一个来源（:func:`app.models.plan.plan_spec_from_mapping`）：``allowed_profiles``
    的数组 → 元组只在那里做一次，这里不复制一份。缓存里的脏数据（多一个键、少一个键）
    仍然会按列清单暴露出来，而缓存的职责只是「把刚才读到的行原样还回去」。
    """

    return plan_spec_from_mapping(payload)
