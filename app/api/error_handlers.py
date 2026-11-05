"""HTTP 入口适配器：把异常翻译成统一响应体（仅覆盖 /api/v1/* 的路由）。

共享的是错误词汇（app.core.exceptions），不是响应壳：MCP 入口（第 11 步）会按
JSON-RPC 协议自己表达工具执行失败，不经过根 FastAPI 的异常处理器。

本模块当前只登记两个处理器：
  * AppError               → HTTP 200 + 对应 code（业务失败）
  * RequestValidationError → HTTP 200 + 20002（参数校验失败）

注册入口 install_handlers(app) 是可追加的：后续票在此函数里追加自己的一行
``app.add_exception_handler(...)`` 即可，不必改动 main.py 的组装块。
"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError, ErrorCode
from app.schemas.common import fail


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """业务失败：HTTP 200 + 非零 code；data 是可选的补充数据。"""

    assert isinstance(exc, AppError)
    envelope = fail(exc.code, exc.msg, data=exc.data)
    return JSONResponse(status_code=200, content=jsonable_encoder(envelope))


async def request_validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """参数校验失败：HTTP 200 + 20002，errors 只留 loc/msg/type，不回显原始输入。

    ``exc.errors()`` 的 ``input`` 可能是 bytes、``ctx`` 可能含异常对象；
    JSONResponse.render 用裸 json.dumps，直接塞进去会抛 TypeError 变成 500。
    因此先只挑三个安全字段，再统一过 jsonable_encoder。
    """

    assert isinstance(exc, RequestValidationError)
    slim_errors = [
        {key: error[key] for key in ("loc", "msg", "type") if key in error}
        for error in exc.errors()
    ]
    envelope = fail(
        ErrorCode.REQUEST_VALIDATION_ERROR,
        data={"errors": jsonable_encoder(slim_errors)},
    )
    return JSONResponse(status_code=200, content=jsonable_encoder(envelope))


def install_handlers(app: FastAPI) -> None:
    """把 HTTP 入口的异常处理器挂到应用上；后续票在此追加一行注册即可。"""

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
