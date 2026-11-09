"""HTTP 侧薄适配器：把 FastAPI 的请求与数据库会话交进判定内核。

这里**一行判定都没有**。两个入口（HTTP 与 MCP）唯一需要的区别就是「怎么拿到请求」：
HTTP 从依赖注入拿，MCP 从 JSON-RPC 请求上下文拿；除此之外的规则全在
:func:`app.core.auth.resolve_identity` 里，只有一份。

``get_current_key`` 就是这份适配器对路由暴露的唯一名字，返回判定结论
:class:`app.core.auth.KeyContext`。名字里的 ``key`` 是「这次调用用的是哪把 Key」，
不是「去查一把 Key」——查 Key 与四种未授权的判定都在内核里。

改名与全部调用点一起收口（占位时代的旧名字已彻底消失，仓库里 `grep` 不到）：``Depends(...)``
与 ``dependency_overrides[...]`` 认的是**可调用对象本身**，只改一半会让那些 override 在
无提示的情况下失效，测试于是悄悄改成真的走一遍鉴权。
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.auth import KeyContext, resolve_identity
from app.db.session import get_db


def get_current_key(request: Request, db: Session = Depends(get_db)) -> KeyContext:
    """路由的鉴权依赖：把请求与本次会话交给判定内核，原样返回它的结论。"""

    return resolve_identity(request, db)
