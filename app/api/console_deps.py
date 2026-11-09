"""控制面 HTTP 侧薄适配器：把请求与数据库会话交进控制面内核。

与数据面的 ``app/api/deps.py`` **刻意分成两个文件、两套函数**：``Depends(...)`` 与
``dependency_overrides[...]`` 认的是可调用对象本身，两边混用一个名字就意味着「拿数据面
的 Key 也能进面板」——那正是本票要消灭的东西。这里也一行判定都没有，判定全在
:mod:`app.core.console_auth`。

几个入口名字各回答一个问题：

- ``get_console_account``：这次是谁（读会话 cookie）；
- ``get_console_admin``：同上，但必须是管理员；
- ``require_console_write`` / ``require_console_admin_write``：同上，并且这个请求带了
  防跨站的显式头。**写操作**用这两个，读操作用前两个；
- ``require_console_write_header``：登出这种「不需要先认出是谁、但仍然是写操作」的
  接口用它——没有会话也要能清掉 cookie，否则共用电脑上永远退不干净；
- ``get_plan_cache``：套餐缓存的构造点。测试注入内存假实现，于是「改完缓存里没有了」
  可以被直接断言，而生产走 Redis。
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.console_auth import (
    ConsoleIdentity,
    require_admin,
    require_write_header,
    resolve_console_identity,
)
from app.core.plan_cache import PlanCache
from app.db.session import get_db
from app.services import plan_service


def get_console_account(
    request: Request, db: Session = Depends(get_db)
) -> ConsoleIdentity:
    """面板接口的鉴权依赖：只读会话 cookie，原样返回判定结论。"""

    return resolve_console_identity(request, db)


def get_console_admin(
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """管理员依赖：先认出是谁，再判角色（``20011``）。"""

    require_admin(identity)
    return identity


def require_console_write(
    request: Request,
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """带 cookie 的写操作：额外要求防跨站头（``20011``）。"""

    require_write_header(request)
    return identity


def require_console_admin_write(
    request: Request,
    identity: ConsoleIdentity = Depends(get_console_admin),
) -> ConsoleIdentity:
    """管理员写操作：角色与防跨站头都要过。"""

    require_write_header(request)
    return identity


def require_console_write_header(request: Request) -> None:
    """只查防跨站头，不查会话：登出要能在「会话已经不在」时仍然清 cookie。"""

    require_write_header(request)


def get_plan_cache() -> PlanCache:
    """套餐缓存的生产构造点；测试用 ``dependency_overrides`` 换成内存假实现。"""

    return plan_service.default_plan_cache()
