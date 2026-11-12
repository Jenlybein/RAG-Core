"""异步引擎、异步会话工厂与请求级依赖——第 11 步（票 #126）铺的地基。

## 这一票做了什么、没做什么

三样东西：一个复用现有连接串的 ``AsyncEngine``、一个 ``expire_on_commit=False`` 的
``async_sessionmaker``、一个请求级依赖 :func:`get_async_db`。**没有任何生产路由改用它**——
迁移按垂直切片推进（#127 起，一条请求路径一次），这一票只把地基铺好并证明它可用
（探针见 ``tests/test_async_probe.py``）。

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

## 异步版的事务怎么收口（**尚未实现**，写给 #127 之后的人）

同步栈的提交点只有一个（``app.db.transaction``，结构守卫见 ``tests/test_transaction.py``），
异步版走**同一个收口点**，形状是它的孪生：

* 事务对象持有 ``AsyncSession``，``__aenter__`` / ``__aexit__`` 对应现有的
  ``__enter__`` / ``__exit__``，落库与撤销都是 ``await``；
* 请求边界仍然是依赖：写接口声明 ``Depends(get_async_transaction)``，一个请求一笔事务；
* ``after_commit`` 的登记点不变（``session.info`` 上的同一笔），副作用仍然在提交之后执行；
* 服务层继续不提交——**不允许**在服务层出现提交调用，异步化不等于把提交散出去。

在它落地之前，这一票不提供任何提交入口：:func:`get_async_db` 只负责 yield 与关闭，所以
「新栈上有人偷偷提交」在结构上还不可能发生。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

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
    提交点是 ``app.db.transaction``（异步版见模块文档最后一节）。
    """

    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()


__all__ = [
    "MAX_CONNECTIONS_PER_PROCESS",
    "MAX_OVERFLOW",
    "POOL_RECYCLE_SECONDS",
    "POOL_SIZE",
    "AsyncSessionLocal",
    "async_engine",
    "get_async_db",
]
