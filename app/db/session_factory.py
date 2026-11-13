"""进程级会话工厂：给**拿不到 ``Depends``**的路径开的那个口子。

中间件（每请求一次配置刷新）与运维脚本都必须自己开一个会话。它们各写一份
``SqlalchemySessionLocal`` 并不难，难的是**测试**：注入点如果分叉，测试里就会有一条
路径仍然连着真 Postgres，而套件在你的机器上绿、在 CI 上红（或者反过来）。

所以这里只有一处：``session_factory()`` 返回「当前该用的工厂」，``set_session_factory``
是它唯一的写入口。``app.services.runtime_settings.set_session_factory`` 就是转发到这里的
（#93 的公开注入点保持原样，于是既有测试一行不改）。

**Provider 装配用的是异步那一路**（票 #137）：``get_llm_provider`` 是协程，角色指针走
:func:`async_session_factory`——它不再是同步工厂的读者。

**为什么不在模块顶层 import ``SessionLocal``**：``app.db.session`` 在 import 期就按
``settings.DATABASE_URL`` 建引擎；如果每个想拿会话的模块都顶层 import 它，配置的错误
会以「import 顺序」的形式在不同入口上表现不同。局部 import 让「建引擎」只发生在真正
要用会话的那一刻，而测试可以在那之前注入假实现。

**异步那一路是同一套做法**（第 11 步票 #128）：:func:`async_session_factory` 与
:func:`set_async_session_factory` 是上面两个函数的孪生，指向 ``AsyncSessionLocal``。
两个注入点**刻意分开**，而不是合成一个「看会话类型返回」的函数：一个进程在迁移期同时
有同步与异步两条路径，合成一个之后「这条路径到底拿到了哪一套」只能靠运行期类型去猜，
而猜错的形态是「离线套件悄悄连了真库」。
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

_override: Callable[[], Session] | None = None
_async_override: Callable[[], AsyncSession] | None = None


def session_factory() -> Callable[[], Session]:
    """当前该用的会话工厂：测试注入的假实现优先，否则生产 ``SessionLocal``。"""

    if _override is not None:
        return _override
    from app.db.session import SessionLocal

    return SessionLocal


def set_session_factory(factory: Callable[[], Session] | None) -> None:
    """注入会话工厂（测试用假会话；生产复位成 ``None`` 即回到 ``SessionLocal``）。"""

    global _override
    _override = factory


def async_session_factory() -> Callable[[], AsyncSession]:
    """当前该用的**异步**会话工厂：注入的假实现优先，否则 ``AsyncSessionLocal``。"""

    if _async_override is not None:
        return _async_override
    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal


def set_async_session_factory(factory: Callable[[], AsyncSession] | None) -> None:
    """注入异步会话工厂（测试用假会话；生产复位成 ``None`` 即回到 ``AsyncSessionLocal``）。"""

    global _async_override
    _async_override = factory


async def close_quietly_async(
    session: AsyncSession | None, *, on_failure: Callable[[str], None]
) -> None:
    """:func:`close_quietly` 的异步孪生：关一个异步的**独立短事务**会话。

    同样的三条：没有单独 ``rollback``（未提交的事务随 ``close`` 结束）、``close`` 自己
    失败也只报给 ``on_failure``、传出去的是异常**类型名**而不是异常对象。写成两个函数
    而不是一个「看类型再 await」的函数：后者在「同步会话被传进异步收尾」时会静默跳过关闭，
    而那正是连接泄漏的形状。
    """

    if session is None:
        return
    try:
        await session.close()
    except Exception as exc:  # noqa: BLE001 - 旁路：关会话失败只许降级
        on_failure(type(exc).__name__)


__all__ = [
    "async_session_factory",
    "close_quietly_async",
    "session_factory",
    "set_async_session_factory",
    "set_session_factory",
]
