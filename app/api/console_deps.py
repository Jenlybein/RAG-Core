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
- ``require_console_tenant`` / ``require_console_tenant_write``：**用户面板类接口**
  （**知识库 / 文档 / 密钥**；模型配置是平台级资源，走管理员那条门）的租户门禁，返回
  已校验的 ``ConsoleIdentity``。管理员（``tenant_id IS NULL``）能登录、能看 ``/console/me``，
  但它不属于任何租户，这些接口对它一律 ``20011`` ——写成共用依赖而不是每条各判一遍，
  是因为「少写一条」的结果是管理员悄悄拿到一个空租户的写权限。服务层**只接收**这个已
  校验的身份，不再自己判一遍租户（那是同一判定的第二套实现，正是 #82 修掉的东西）；
- **平台级接口**（账号 / 套餐 / 审计 / 配置中台 ``settings``）用
  ``get_console_admin`` / ``require_console_admin_write``，**不套**上面那对租户依赖。
  ``runtime_settings`` 不属于任何租户：``RERANK_ENABLED`` 一关，所有租户的 ``quality``
  档位一起退化，所以它恰恰只能给管理员；给「任意租户」开写口就是跨租户越权；
- ``require_console_write_header``：登出这种「不需要先认出是谁、但仍然是写操作」的
  接口用它——没有会话也要能清掉 cookie，否则共用电脑上永远退不干净；
- ``get_plan_cache``：套餐缓存的构造点。测试注入内存假实现，于是「改完缓存里没有了」
  可以被直接断言，而生产走 Redis；
- ``get_runtime_settings_cache``：运行时配置覆盖层的缓存构造点。它与进程级注入点是
  **同一个对象**（见 ``app.core.runtime_settings_cache``）：刷新发生在请求中间件里，
  中间件拿不到 ``Depends``，两条构造点分叉会让测试里的中间件永远去连真 Redis；
- ``get_langfuse_reader``：控制台瀑布「本地没命中时去问观测服务」那一步的构造点
  （#103）。测试注入一个假客户端，于是这条路径能在离线套件里被逐项断言；生产实现是
  ``app.observability.langfuse_reader.read_stage_spans``。

**只有一个面，而且它在异步栈上**（第 11 步票 #129 → #130）：控制面的读与写都已经换成
``AsyncSession``，所以这里不再有 ``_async`` 后缀的孪生——判定内核 ``resolve_console_identity``
只剩一份协程实现，五个门禁也各自只有一份。后缀是过渡标记，不是设计（约定见
``app/db/async_session.py`` 的模块文档）。
"""

from __future__ import annotations

from typing import Final

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.db.async_session import get_async_db
from app.observability.langfuse_reader import read_stage_spans
from app.services import plan_service
from app.services.retrieval_waterfall import RemoteStageReader

#: 用户面板接口对管理员（无租户）的拒绝文案；错误码统一 ``20011``。
MSG_TENANT_ACCOUNT_REQUIRED: Final[str] = "a tenant account is required for this console endpoint"


async def get_console_account(
    request: Request, db: AsyncSession = Depends(get_async_db)
) -> ConsoleIdentity:
    """面板接口的鉴权依赖：读会话 cookie，原样返回判定结论。"""

    return await resolve_console_identity(request, db)


async def get_console_admin(
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """管理员依赖：先认出是谁，再判角色（``20011``）。"""

    require_admin(identity)
    return identity


async def require_console_write(
    request: Request,
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """带 cookie 的写操作：额外要求防跨站头（``20011``）。"""

    require_write_header(request)
    return identity


async def require_console_admin_write(
    request: Request,
    identity: ConsoleIdentity = Depends(get_console_admin),
) -> ConsoleIdentity:
    """管理员写操作：角色与防跨站头都要过。"""

    require_write_header(request)
    return identity


def require_console_write_header(request: Request) -> None:
    """只查防跨站头，不查会话：登出要能在「会话已经不在」时仍然清 cookie。"""

    require_write_header(request)


async def require_console_tenant(
    identity: ConsoleIdentity = Depends(get_console_account),
) -> ConsoleIdentity:
    """用户面板的读接口：先认出是谁，再要求它属于某个租户（管理员一律 ``20011``）。

    返回**身份对象**而不是裸 ``tenant_id``：路由与它调用的服务层都要 ``account_id`` 去写
    审计，拆成两半会让「读接口多声明一个 ``Depends(get_console_account)``」变成惯例，
    而那条惯例一旦漏掉，审计的 actor 就悄悄变成 ``None``。FastAPI 的依赖缓存保证同一个
    请求里身份只解析一次，多一层依赖不多读一次会话行。
    """

    _ensure_tenant_account(identity)
    return identity


async def require_console_tenant_write(
    identity: ConsoleIdentity = Depends(require_console_write),
) -> ConsoleIdentity:
    """用户面板的写接口：防跨站头通过后，再要求它是租户账号。

    顺序是刻意的：``require_console_write`` 内部先解析会话、再查头，于是——
    未登录的写请求仍是 ``20010``（前端该去登录），登录了但缺头的仍是 ``20011``
    （不该被当成掉线）。管理员带着头进来时才走到这里的租户判定，同样 ``20011``。
    """

    _ensure_tenant_account(identity)
    return identity


def tenant_id_of(identity: ConsoleIdentity) -> str:
    """已过租户门禁的身份 → 裸 ``tenant_id``（**只做类型收窄**，不重复判定）。

    判定已经在 :func:`require_console_tenant` / :func:`require_console_tenant_write` 里做完，
    所以这里没有第二种错误分支；写成函数只是为了让 mypy 在一个地方收窄
    ``str | None``，而不是让每个路由各写一次 ``assert``。
    """

    assert identity.tenant_id is not None, "租户门禁应已拒绝无租户的管理员"
    return identity.tenant_id


def _ensure_tenant_account(identity: ConsoleIdentity) -> None:
    """租户门禁的**唯一**实现：管理员（``tenant_id is None``）不是「无权限」，是「用错了门」。"""

    if identity.tenant_id is None:
        raise AppError(ErrorCode.FORBIDDEN, MSG_TENANT_ACCOUNT_REQUIRED)


def get_plan_cache() -> PlanCache:
    """套餐缓存的生产构造点；测试用 ``dependency_overrides`` 换成内存假实现。"""

    return plan_service.default_plan_cache()


def get_runtime_settings_cache() -> RuntimeSettingsCache:
    """运行时配置覆盖层的缓存构造点（与中间件看到的是同一个进程级实例）。"""

    return get_settings_cache()


def get_langfuse_reader() -> RemoteStageReader:
    """观测服务读回的生产构造点（#103）：返回「按 trace_id 取段」的那个函数本身。

    为什么返回**函数**而不是客户端：读回只需要「给我 trace_id，还我段」。把一个 SDK 客户端
    交到路由层，路由就有了「顺手多做一次查询」的可能，而这一层的职责只有一句「问不到就降级」。
    测试用 ``dependency_overrides`` 换掉它，于是「本地命中时到底有没有去打网络」可以被直接断言
    （替身在那种情况下会抛 ``AssertionError``）。
    """

    return read_stage_spans
