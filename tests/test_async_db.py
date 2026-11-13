"""异步引擎、异步会话工厂与请求级依赖（``app.db.async_session``）。

票 #126 写这份文件时是 expand（新旧并存），#134 之后异步栈已经是**唯一**的请求路径写法，
所以这里断言的三件事仍然成立、且不再有对照物：

1. **引擎复用现有连接串**：``.env`` 的键与格式不变，驱动仍是 psycopg3（不引入 asyncpg），
   于是类型映射与 SQL 一字不改；
2. **连接池上限是显式的**：``pool_size=5`` / ``max_overflow=10`` / ``pool_pre_ping=True`` /
   ``pool_recycle=1800``——每进程的总连接数算式就建立在这四个数上（见 ``docs/架构.md``
   「数据库连接预算」）；
3. **请求级依赖的收尾是确定的**：``get_async_db`` yield 一个会话，请求结束（含异常）一定
   关闭它，并且一个请求一个会话。

「引擎能连真库」不在断言范围：默认套件离线（硬约束 2），连库这件事由集成测试与压测负责。
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.async_session import AsyncSessionLocal, async_engine, get_async_db
from tests.fake_db import AsyncRecordingSession


def test_the_async_engine_reuses_the_configured_connection_string() -> None:
    """``.env`` 的键与格式不变：同一个 URL，只是换成了异步引擎。

    同步引擎（``app.db.session``）仍然在，但它已经不在请求路径上（只剩 Provider 装配期的
    角色指针、配置热刷新与运维脚本，见 ADR-0024）；两者读的是同一个 ``DATABASE_URL``，
    这条对等断言守的是「换引擎没换连接串」。
    """

    from app.db.session import engine as sync_engine

    assert async_engine.url == make_url(settings.DATABASE_URL)
    assert async_engine.url == sync_engine.url
    # 驱动不换：psycopg3 的异步方言，不是 asyncpg。
    assert async_engine.url.drivername == "postgresql+psycopg"


def test_the_pool_ceiling_is_explicit() -> None:
    """每进程的连接上限必须能一眼数清：``5 + 10 = 15``。

    ``max_overflow`` / ``recycle`` / ``pre_ping`` 在不上连接的情况下没有公开读法，所以这里
    读的是池的配置属性。这四个数**本身就是这张票的交付物**（算式写在 ``docs/架构.md``），
    不是我们自己的实现细节——改它们要同时改那一节文档。
    """

    pool = async_engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._recycle == 1800
    assert pool._pre_ping is True


def test_the_session_factory_keeps_objects_usable_after_commit() -> None:
    """``expire_on_commit=False``：与现有同步写法语义最接近（提交后对象仍可读）。"""

    assert AsyncSessionLocal.class_ is AsyncSession
    assert AsyncSessionLocal.kw["expire_on_commit"] is False


@pytest.mark.asyncio
async def test_the_dependency_yields_a_session_and_closes_it(monkeypatch) -> None:
    session = AsyncRecordingSession()
    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", lambda: session)

    opened = get_async_db()
    assert await anext(opened) is session
    assert session.closed is False

    with pytest.raises(StopAsyncIteration):
        await anext(opened)

    assert session.closed is True


@pytest.mark.asyncio
async def test_the_dependency_closes_the_session_when_the_request_fails(monkeypatch) -> None:
    """异常路径也要关：``finally`` 而不是「成功之后顺手关一下」。"""

    session = AsyncRecordingSession()
    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", lambda: session)

    opened = get_async_db()
    await anext(opened)

    with pytest.raises(RuntimeError, match="boom"):
        await opened.athrow(RuntimeError("boom"))

    assert session.closed is True
