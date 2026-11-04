"""统一响应：成功与失败同形，业务失败也返回 HTTP 200。

    { "code": 0, "msg": "success", "data": ... }
    { "code": 20008, "msg": "knowledge base not found", "data": null }

依据 ADR-0007：HTTP 状态码只表达「服务在不在」，业务结果由 body 的 code 表达。
所以业务失败是「返回 fail(...)」而不是抛 HTTPException——FastAPI 默认 200，
不需要异常处理器就能满足约定。全局异常处理器与参数校验翻译是阶段 2 的活。

本步骤不定义 ErrorCode 枚举（那是阶段 2 的产出）；调用方直接用最终编号
（如 20008），阶段 2 再把字面量收进枚举。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """所有接口的返回体。code=0 成功，非 0 失败；失败时 data 为 null。"""

    code: int = 0
    msg: str = "success"
    data: T | None = None


def ok(data: T | None = None, *, msg: str = "success") -> ApiResponse[T]:
    """成功响应。"""

    return ApiResponse(code=0, msg=msg, data=data)


def fail(code: int, msg: str) -> ApiResponse[None]:
    """业务失败响应。code 用最终分段编号（2xxxx / 4xxxx / 5xxxx）。"""

    return ApiResponse(code=code, msg=msg, data=None)
