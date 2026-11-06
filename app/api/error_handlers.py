"""HTTP 入口适配器：把异常翻译成统一响应体（仅覆盖 /api/v1/* 的路由）。

共享的是错误词汇（app.core.exceptions），不是响应壳：MCP 入口（第 11 步）会按
JSON-RPC 协议自己表达工具执行失败，不经过根 FastAPI 的异常处理器。

本模块登记三个处理器：
  * AppError               → HTTP 200 + 对应 code（业务失败）
  * RequestValidationError → HTTP 200 + 20002（参数校验失败）
  * Exception              → HTTP 500 + 50000（服务异常，堆栈只进日志）

注册入口 install_handlers(app) 集中挂这三个处理器，main.py 只调用它。
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError, ErrorCode
from app.core.request_context import REQUEST_ID_SCOPE_KEY, _request_id_from
from app.schemas.common import fail

logger = structlog.get_logger(__name__)


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


def _request_id_for_500(request: Request) -> str:
    """优先取中间件存进 scope 的同一个 id；取不到才按同一套规则从入站头净化。

    中间件在 finally 里 clear_contextvars()，早于最外层 ServerErrorMiddleware
    触发本 handler，所以 contextvar 此时是空的，只能读 scope。
    """

    stored = request.scope.get("state", {}).get(REQUEST_ID_SCOPE_KEY)
    if isinstance(stored, str):
        return stored
    return _request_id_from(request.headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """服务异常：HTTP 500 + 50000，堆栈只进日志，绝不进响应体。

    ServerErrorMiddleware 是最外层，用原始 send 构造 500 响应，绕过所有内层
    send 包装；因此这里必须显式把 x-request-id 写进响应头。
    """

    request_id = _request_id_for_500(request)
    logger.exception("unhandled exception", request_id=request_id, path=request.url.path)
    envelope = fail(ErrorCode.SERVER_ERROR)
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(envelope),
        headers={"x-request-id": request_id},
    )


def install_handlers(app: FastAPI) -> None:
    """把 HTTP 入口的异常处理器挂到应用上；后续票在此追加一行注册即可。"""

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    # ── T5: 服务异常兜底 ──
    app.add_exception_handler(Exception, unhandled_exception_handler)
    # ── T5 结束 ──
