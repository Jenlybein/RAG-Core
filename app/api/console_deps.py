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
- ``require_console_tenant`` / ``require_console_tenant_write``：**用户面板类接口**（keys /
  kbs / documents / models）用这两个——管理员不属于任何租户，它的 ``tenant_id`` 是
  ``None``，这类接口对它一律 ``20011``；
- ``get_console_admin`` / ``require_console_admin_write``：**平台级接口**（账号 / 套餐 /
  审计 / 配置中台 settings）用这两个。配置中台是平台级、无租户归属的配置，一次修改横跨
  所有租户，所以它恰恰只能给管理员，**不能**套用上面那对租户依赖；
- ``require_console_write_header``：登出这种「不需要先认出是谁、但仍然是写操作」的
  接口用它——没有会话也要能清掉 cookie，否则共用电脑上永远退不干净；
- ``get_plan_cache``：套餐缓存的构造点。测试注入内存假实现，于是「改完缓存里没有了」
  可以被直接断言，而生产走 Redis；
- ``get_runtime_settings_cache``：运行时配置覆盖层的缓存构造点。它与进程级注入点是
  **同一个对象**（见 ``app.core.runtime_settings_cache``）：刷新发生在请求中间件里，
  中间件拿不到 ``Depends``，两条构造点分叉会让测试里的中间件永远去连真 Redis。
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
from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache
from app.core.runtime_settings_cache import (
    RuntimeSettingsCache,
    get_settings_cache,
)
from app.db.session import get_db
from app.services import plan_service

#: 「管理员不属于任何租户」时的提示。与管理员的角色判定分开写：两者都出 ``20011``
#: 但原因不同（一个是「你不是管理员」，一个是「这类接口要租户身份」），排障时看得出。
MSG_TENANT_REQUIRED = "tenant identity required (platform admins belong to no tenant)"


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


def require_console_tenant(
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """用户面板类读接口的门禁：先认出是谁，再要求它有租户身份（``20011``）。

    只写一处而不是每票各写一遍：`keys / kbs / documents / settings / models` 都对
    「管理员空租户」同一条答案，五个地方各判一次的结果迟早有一个漏掉。

    顺序是「先认人、后判租户」：没有会话时正确的码是 ``20010``（去登录），不是
    ``20011``——先判租户会把一个未登录的请求报成「你没有权限」，前端就不会跳登录页了。
    """

    if identity.tenant_id is None:
        raise AppError(ErrorCode.FORBIDDEN, MSG_TENANT_REQUIRED)
    return identity


def require_console_tenant_write(
    request: Request,
    identity: ConsoleIdentity = Depends(require_console_write),
) -> ConsoleIdentity:
    """用户面板类写接口的门禁：防跨站头 +（先认人后判租户）。

    复用 :func:`require_console_write` 而不是自己再查一次头：``WRITE_HEADER_NAME`` 与
    固定值只该有一个出处，写两遍就有一个会先漂移。
    """

    if identity.tenant_id is None:
        raise AppError(ErrorCode.FORBIDDEN, MSG_TENANT_REQUIRED)
    return identity


def get_plan_cache() -> PlanCache:
    """套餐缓存的生产构造点；测试用 ``dependency_overrides`` 换成内存假实现。"""

    return plan_service.default_plan_cache()


def get_runtime_settings_cache() -> RuntimeSettingsCache:
    """运行时配置覆盖层的缓存构造点（与中间件看到的是同一个进程级实例）。"""

    return get_settings_cache()
