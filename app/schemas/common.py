"""统一响应：成功与失败同形，业务失败也返回 HTTP 200。

    { "code": 0, "msg": "success", "data": ... }
    { "code": 20008, "msg": "knowledge base not found", "data": null }

依据 ADR-0007：HTTP 状态码只表达「服务在不在」，业务结果由 body 的 code 表达。
所以业务失败不是抛 HTTPException——HTTP 入口适配器把 AppError 翻译成 fail(...)，
FastAPI 默认 200，不改框架的状态码语义。

fail() 只被 HTTP 入口适配器调用；业务层抛 AppError，路由只写 ok(...)。
错误码收在 app.core.exceptions.ErrorCode（与协议无关），全表一次性落地，3xxxx 仅预留。
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.core.exceptions import ErrorCode, default_message

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """所有接口的返回体。code=0 成功，非 0 失败；失败时 data 为 null。"""

    code: int = 0
    msg: str = "success"
    data: T | None = None


def ok(data: T | None = None, *, msg: str = "success") -> ApiResponse[T]:
    """成功响应。"""

    return ApiResponse(code=0, msg=msg, data=data)


def fail(code: ErrorCode, msg: str | None = None, *, data: Any = None) -> ApiResponse[Any]:
    """业务失败响应。只由入口适配器调用；缺省提示由枚举名生成。"""

    return ApiResponse(
        code=int(code), msg=msg if msg is not None else default_message(code), data=data
    )
