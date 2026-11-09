"""HTTP 侧薄适配器：把 FastAPI 的请求与数据库会话交进判定内核。

这里**一行判定都没有**。两个入口（HTTP 与 MCP）唯一需要的区别就是「怎么拿到请求」：
HTTP 从依赖注入拿，MCP 从 JSON-RPC 请求上下文拿；除此之外的规则全在
:func:`app.core.auth.resolve_identity` 里，只有一份。

``resolve_dev_key`` 这个名字是占位时代留下的。本票**不**改名：路由上的
``Depends(resolve_dev_key)`` 与 15 个测试文件里的
``dependency_overrides[resolve_dev_key] = ...`` 认的是**可调用对象本身**，
一旦这里换成一个新对象而测试没跟着改，那些 override 会在无提示的情况下失效，
测试于是改成真的走一遍鉴权（有的还会去连库）。重命名与全部调用点一起做，
放在后续那一票。
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.auth import KeyContext, resolve_identity
from app.db.session import get_db


def resolve_dev_key(request: Request, db: Session = Depends(get_db)) -> KeyContext:
    """路由的鉴权依赖：名字未改，行为换成判定内核。"""

    return resolve_identity(request, db)
