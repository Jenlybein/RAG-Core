"""进程级会话工厂：给**拿不到 ``Depends``**的路径开的那个口子。

中间件（每请求一次配置刷新）、Celery 入口、以及 Provider 装配（它不在请求依赖树里）
都必须自己开一个会话。它们各写一份 ``SqlalchemySessionLocal`` 并不难，难的是**测试**：
注入点如果分叉，测试里就会有一条路径仍然连着真 Postgres，而套件在你的机器上绿、在 CI
上红（或者反过来）。

所以这里只有一处：``session_factory()`` 返回「当前该用的工厂」，``set_session_factory``
是它唯一的写入口。``app.services.runtime_settings.set_session_factory`` 就是转发到这里的
（#93 的公开注入点保持原样，于是既有测试一行不改）。

**为什么不在模块顶层 import ``SessionLocal``**：``app.db.session`` 在 import 期就按
``settings.DATABASE_URL`` 建引擎；如果每个想拿会话的模块都顶层 import 它，配置的错误
会以「import 顺序」的形式在不同入口上表现不同。局部 import 让「建引擎」只发生在真正
要用会话的那一刻，而测试可以在那之前注入假实现。
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

_override: Callable[[], Session] | None = None


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


__all__ = ["session_factory", "set_session_factory"]
