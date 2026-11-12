"""异步引擎、异步会话工厂与请求级依赖——第 11 步（票 #126）铺的地基。

## 这个模块是什么

三样东西：一个复用现有连接串的 ``AsyncEngine``、一个 ``expire_on_commit=False`` 的
``async_sessionmaker``、一个请求级依赖 :func:`get_async_db`。票 #126 只把地基铺好并证明
它可用（探针见 ``tests/test_async_probe.py``）；从票 #127 起按垂直切片往上搬——已经用上
它的是**观测与反馈那一片**（票 #128：归属索引的读、反馈的读写、控制台的 trace 读），
检索链路与控制面其余部分仍在同步栈上。

## 为什么驱动还是 psycopg3

``postgresql+psycopg`` 这条连接串同时支持同步与异步：psycopg 3.2.3 原生带异步，SQLAlchemy
只把 ``create_engine`` 换成 ``create_async_engine``，类型映射（JSONB / 数组 / pgvector）与
SQL 一字不改。换 asyncpg 只需要改 dialect 与 URL、业务代码零改动——那留作**翻案条件**：
压测显示 psycopg 异步驱动本身是瓶颈时再换，不预先换。``.env`` 的键与格式因此不变。

## 连接池为什么是这四个数

``pool_size=5`` / ``max_overflow=10``（每进程上限 15）/ ``pool_pre_ping=True`` /
``pool_recycle=1800``。双栈期「同步 + 异步」两套池并存，总连接数的算式写在
``docs/架构.md``「双栈期的数据库连接预算」一节——改这里的数字必须同时改那一节，否则运维
会按一个已经不对的算式去调 ``max_connections``。

``pool_pre_ping`` 与 ``pool_recycle`` 在同步池上都没有显式设（用的是默认值）：异步池要跨
事件循环活得更久，连接被中间层掐掉时**当场**发现比在业务查询里发现便宜。

## 异步版的事务怎么收口

同步栈的提交点只有一个（``app.db.transaction``，结构守卫见 ``tests/test_transaction.py``），
异步版走**同一个收口点**，形状是它的孪生（票 #128 落地）：

* :class:`app.db.transaction.AsyncTransaction` 持有 ``AsyncSession``，``__aenter__`` /
  ``__aexit__`` 对应同步版的 ``__enter__`` / ``__exit__``，落库与撤销都是 ``await``；
  入口是 ``async with async_transaction(session):``；
* ``after_commit`` 的登记点不变（``session.info`` 上的同一笔），副作用仍然在提交之后执行；
* 服务层继续不提交——**不允许**在服务层出现提交调用，异步化不等于把提交散出去；
  结构守卫扫的是全仓的提交与回滚调用，异步写法（前面多个 ``await``）一样会被它抓到。

**请求级依赖 :func:`get_async_transaction`**（第 11 步票 #132 加的）：形状与同步版
``get_transaction`` 逐条同构——写接口在签名上声明它，一个请求恰好一笔，成功出块提交、
异常回滚。数据面上传（``POST /documents/upload``）是第一条用它的写路径；控制面写要等到
S4 那一片。已经用上异步提交点的还有**反馈写入**（自己开会话的旁路短事务），归属索引的
写入在检索路径上，随票 #127 搬。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.db.transaction import AsyncTransaction, async_transaction

#: 每进程的**稳态**连接数（与同步池的默认值一致，双栈期不改变既有容量）。
POOL_SIZE: int = 5

#: 峰值时允许临时多开的连接数。每进程上限 = ``POOL_SIZE + MAX_OVERFLOW`` = 15。
MAX_OVERFLOW: int = 10

#: 连接的最长寿命（秒）：跨事件循环复用时，中间层（PgBouncer / 云厂商网关）的
#: 空闲掐断比数据库自己的 ``idle_session_timeout`` 更早，主动回收比撞上它便宜。
POOL_RECYCLE_SECONDS: int = 1800

#: 每进程的连接上限：双栈期的算式（``docs/架构.md``）用的就是这个数。
MAX_CONNECTIONS_PER_PROCESS: int = POOL_SIZE + MAX_OVERFLOW

#: 引擎在 import 期创建，但**不连库**（SQLAlchemy 惰性连接）。与 ``app.db.session`` 的
#: 同步引擎同一个理由：配置错误应该在建引擎的那一刻暴露，而不是在第一个请求里。
async_engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=POOL_RECYCLE_SECONDS,
)

#: ``expire_on_commit=False``：提交之后对象仍然可读，与现有同步写法语义最接近——迁移时
#: 「响应序列化读到过期属性」不该成为一条新的失败路径。
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：yield 一个 ``AsyncSession``，请求结束后关闭（含异常路径）。

    与同步版 :func:`app.db.session.get_db` 同形：不提交、不回滚，只负责生命周期——
    提交点是 ``app.db.transaction``（异步版见模块文档）。
    """

    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()


async def get_async_transaction(
    db: AsyncSession = Depends(get_async_db),
) -> AsyncGenerator[AsyncTransaction, None]:
    """FastAPI 依赖：把这次请求圈进**一笔**异步事务（异步写接口专用）。

    与同步版 :func:`app.db.session.get_transaction` 逐条同构：出块提交（并执行
    ``after_commit`` 登记的动作），端点抛异常时整笔回滚后再把异常原样抛回去——错误码与
    响应形状仍由异常处理器决定，与事务无关。FastAPI 会缓存同一请求内的依赖结果，所以
    同一请求里多处声明它仍然只有一笔。

    声明它**同时**意味着「这条路径会写库」：读接口不该声明，也不该顺手提交。
    """

    async with async_transaction(db) as opened:
        yield opened


__all__ = [
    "MAX_CONNECTIONS_PER_PROCESS",
    "MAX_OVERFLOW",
    "POOL_RECYCLE_SECONDS",
    "POOL_SIZE",
    "AsyncSessionLocal",
    "async_engine",
    "get_async_db",
    "get_async_transaction",
]
