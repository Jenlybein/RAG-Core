"""HTTP 侧薄适配器：把 FastAPI 的请求与数据库会话交进判定内核。

这里**一行判定都没有**。两个入口（HTTP 与 MCP）唯一需要的区别就是「怎么拿到请求」：
HTTP 从依赖注入拿，MCP 从 JSON-RPC 请求上下文拿；除此之外的规则全在
:func:`app.core.auth.resolve_identity` 里，只有一份。

``get_current_key`` 就是这份适配器对路由暴露的唯一名字，返回判定结论
:class:`app.core.auth.KeyContext`。名字里的 ``key`` 是「这次调用用的是哪把 Key」，
不是「去查一把 Key」——查 Key 与四种未授权的判定都在内核里。

**它读异步会话**（第 11 步票 #130）：判定内核要解析套餐，而套餐缓存是
``redis.asyncio``、套餐行读的是 ``AsyncSession``。于是这条依赖也从同步栈搬到了异步栈，
与检索链路共用同一个 ``get_async_db``（FastAPI 的依赖缓存保证一个请求只开一个会话）。

改名与全部调用点一起收口（占位时代的旧名字已彻底消失，仓库里 `grep` 不到）：``Depends(...)``
与 ``dependency_overrides[...]`` 认的是**可调用对象本身**，只改一半会让那些 override 在
无提示的情况下失效，测试于是悄悄改成真的走一遍鉴权。
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import KeyContext, resolve_identity
from app.db.async_session import get_async_db


async def get_current_key(
    request: Request, db: AsyncSession = Depends(get_async_db)
) -> KeyContext:
    """路由的鉴权依赖：把请求与本次会话交给判定内核，原样返回它的结论。"""

    return await resolve_identity(request, db)
