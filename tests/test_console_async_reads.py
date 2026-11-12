"""票 #129：控制面的**只读**查询换到新栈（``AsyncSession`` + 异步套餐缓存）。

这份文件守两件在线下就能钉住的事：

1. **形状**（结构守卫，写法照 ``tests/test_transaction.py``）：八条只读端点从
   :func:`app.db.async_session.get_async_db` 取会话，而控制面的**写入**仍在同步栈上
   （``get_db``）——本票只搬读，写入归 #130。默认套件离线，没有真库可用，「这条路径
   走的是哪套会话」只能从依赖树上读出来；不钉住它，一张票改完可能只改了一半而全绿。
2. **套餐缓存**：``resolve_plan`` 可 ``await``，生产实现握的是**异步** Redis 客户端；
   同步客户端（``get_sync_redis_client``）收缩到只剩运行时配置缓存一个用户。

分页、排序、过滤与越权行为本身不在这里重复断言：它们是既有 ``test_console_*.py`` 的
内容，那批用例的会话替身换成了异步替身，同一组外部断言在新栈上照样成立。
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import pytest

from app.core.plan_cache import RedisPlanCache
from app.core.redis_client import (
    reset_redis_client,
    reset_sync_redis_client,
    set_redis_client,
    set_sync_redis_client,
)

#: 本票搬的八条只读端点：密钥 / 套餐 / 档位 / 运行时设置 / 审计 / traces 的列表与详情。
READ_PATHS: tuple[str, ...] = (
    "/console/keys",
    "/console/plans",
    "/console/profile-presets",
    "/console/settings",
    "/console/audit-logs",
    "/console/retrieval-traces",
    "/console/retrieval-traces/recent",
    "/console/retrieval-traces/{log_id}/waterfall",
)

#: 控制面的写入：**必须留在同步栈上**（#130 搬它们）。少一条都说明这张票越界了。
WRITE_PATHS: tuple[tuple[str, str], ...] = (
    ("POST", "/console/keys"),
    ("DELETE", "/console/keys/{key_id}"),
    ("PATCH", "/console/key-policies/{policy_id}"),
    ("PATCH", "/console/plans/{plan_id}"),
    ("PATCH", "/console/accounts/{account_id}"),
    ("PATCH", "/console/settings"),
    ("PATCH", "/console/profile-presets/{profile}"),
)


def _dependant_calls(dependant: Any, seen: list[Any] | None = None) -> list[Any]:
    """把一条路由的依赖树摊平成「可调用对象」列表（含路由函数自己）。"""

    found = [] if seen is None else seen
    found.append(dependant.call)
    for child in dependant.dependencies:
        _dependant_calls(child, found)
    return found


def _routes() -> dict[tuple[str, str], Any]:
    """``(方法, 路径)`` → 路由对象；路径含占位符时按声明原样比对。"""

    app = importlib.import_module("app.main").app
    routes: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            routes[(method, path)] = route
    return routes


def _session_dependencies(path: str) -> set[Any]:
    """这条路径的依赖树里出现的「取会话」入口。"""

    from app.db.async_session import get_async_db
    from app.db.session import get_db

    route = _routes()[("GET", path)]
    calls = _dependant_calls(route.dependant)
    return {entry for entry in calls if entry in (get_async_db, get_db)}


@pytest.mark.parametrize("path", READ_PATHS)
def test_every_console_read_takes_its_session_from_the_async_stack(path: str) -> None:
    from app.db.async_session import get_async_db
    from app.db.session import get_db

    dependencies = _session_dependencies(path)

    assert get_async_db in dependencies, f"{path} 还没换到异步会话"
    assert get_db not in dependencies, f"{path} 仍然从同步栈取会话（一张请求两条连接）"


@pytest.mark.parametrize(("method", "path"), WRITE_PATHS)
def test_console_writes_are_on_the_async_stack_too(method: str, path: str) -> None:
    """票 #130 之后，控制面的**写入**也走异步栈。

    这条断言的作用从「范围守卫，防止本票顺手改写入」翻成了「收口守卫，防止写入又退回
    同步栈」——两个方向都是同一条纪律：一次请求只许有一条连接，控制面不再有第二种写法。
    """

    from app.db.async_session import get_async_db
    from app.db.session import get_db

    route = _routes()[(method, path)]
    calls = _dependant_calls(route.dependant)

    assert get_async_db in calls, f"{method} {path} 还在从同步栈取会话"
    assert get_db not in calls, f"{method} {path} 退回同步会话了（一次请求两条连接）"


# ───────────────────────────────────────────────────────────── 套餐缓存改异步


class _AsyncKeyValue:
    """``redis.asyncio`` 客户端的替身：套餐缓存用到的那三条命令，全是 ``await``。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.expiries[key] = ttl

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _SyncClientThatMustNotBeUsed:
    """同步客户端替身：谁在事件循环上碰它，这里当场炸。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"套餐缓存不该再用同步 Redis：{name}")


@pytest.fixture(autouse=True)
def _clean_redis_injection():
    """复位 Redis 注入点与**进程级默认套餐缓存**。

    离线夹具（``tests/conftest.py``）默认注入一个内存套餐缓存；这一组用例要验的正是
    「生产默认实现握的是哪个客户端」，所以先把它复位成 ``None``，用完再交还给夹具。
    """

    from app.services.plan_service import set_default_plan_cache

    reset_redis_client()
    reset_sync_redis_client()
    set_default_plan_cache(None)
    try:
        yield
    finally:
        reset_redis_client()
        reset_sync_redis_client()
        set_default_plan_cache(None)


def test_the_plan_cache_is_awaited_and_round_trips_through_the_async_client() -> None:
    """三个方法都是协程：同步 Redis 会在事件循环上按次阻塞（票 #129 的整个理由）。"""

    cache = RedisPlanCache(_AsyncKeyValue(), ttl_seconds=60)

    assert inspect.iscoroutinefunction(cache.get)
    assert inspect.iscoroutinefunction(cache.set)
    assert inspect.iscoroutinefunction(cache.delete)


@pytest.mark.asyncio
async def test_the_plan_cache_round_trips_through_the_async_client() -> None:
    from app.core.plan_cache import cache_key
    from app.models.plan import PLAN_CODES, PLAN_PRESETS

    client = _AsyncKeyValue()
    cache = RedisPlanCache(client, ttl_seconds=60)
    plan = PLAN_PRESETS[PLAN_CODES[0]]

    await cache.set(7, plan)

    assert cache_key(7) in client.values
    assert client.expiries[cache_key(7)] == 60
    assert await cache.get(7) == plan

    await cache.delete(7)
    assert await cache.get(7) is None


@pytest.mark.asyncio
async def test_the_default_plan_cache_uses_the_async_redis_client_only() -> None:
    """生产构造点：握异步客户端；同步客户端被碰一下就算回归。"""

    from app.models.plan import PLAN_CODES, PLAN_PRESETS
    from app.services.plan_service import default_plan_cache

    client = _AsyncKeyValue()
    set_redis_client(client)
    set_sync_redis_client(_SyncClientThatMustNotBeUsed())

    cache = default_plan_cache()
    await cache.set(3, PLAN_PRESETS[PLAN_CODES[0]])

    assert len(client.values) == 1


@pytest.mark.asyncio
async def test_resolve_plan_is_awaitable() -> None:
    from app.models.plan import PLAN_CODES, PLAN_PRESETS, plan_row_of
    from app.services.plan_service import resolve_plan

    row = plan_row_of(PLAN_PRESETS[PLAN_CODES[0]])
    row.id = 5

    class _RowSession:
        # 票 #130 起套餐那次读也走 ``await db.get(...)``，替身必须是协程。
        async def get(self, _model: Any, plan_id: int) -> Any:
            return row if plan_id == 5 else None

    class _Cache:
        def __init__(self) -> None:
            self.items: dict[int, Any] = {}
            self.set_calls: list[int] = []

        async def get(self, plan_id: int) -> Any:
            return self.items.get(plan_id)

        async def set(self, plan_id: int, plan: Any) -> None:
            self.set_calls.append(plan_id)
            self.items[plan_id] = plan

        async def delete(self, plan_id: int) -> None:
            self.items.pop(plan_id, None)

    cache = _Cache()

    assert inspect.iscoroutinefunction(resolve_plan)
    plan = await resolve_plan(_RowSession(), 5, cache=cache)

    assert plan == PLAN_PRESETS[PLAN_CODES[0]]
    assert cache.set_calls == [5]


def test_the_sync_redis_client_has_a_single_remaining_user() -> None:
    """过渡缝收缩：全仓只剩运行时配置缓存引用 ``get_sync_redis_client``。

    套餐缓存改异步之后，同步客户端唯一还说得通的用户是运行时配置覆盖层——它跑在
    Celery 任务与线程池里（见 ``app/core/redis_client`` 的模块文档）。
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    users: list[str] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative == "core/redis_client.py":
            # 构造点自己：那个文件是定义，不算「用户」。
            continue
        text = path.read_text(encoding="utf-8")
        if "get_sync_redis_client(" not in text:
            continue
        users.append(relative)

    assert users == ["core/runtime_settings_cache.py"], users
