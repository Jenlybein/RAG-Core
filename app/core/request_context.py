"""请求关联标识：基于 Starlette ``BaseHTTPMiddleware`` 的中间件。

本项目所有响应都是一次性返回，没有流式响应（MCP 入口已配 ``json_response=True``，
POST 回普通 JSON；GET 那条 SSE 端点服务端不往里推消息）。因此可以用框架自带的
省事写法，不必按 ASGI 的 ``scope/receive/send`` 手写一个不缓冲响应体的中间件。

标识只进日志字段（``structlog.contextvars`` 绑定，由 T2 的
``merge_contextvars`` 带到每条记录）与响应头，绝不进响应体。
"""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

REQUEST_ID_SCOPE_KEY = "request_id"
_BASE_HTTP_REQUEST_ID_HEADER = "x-request-id"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _request_id_from(headers) -> str:
    """沿用合法的客户端标识，否则生成 UUID；非法值绝不写进日志。"""

    candidate = headers.get(_BASE_HTTP_REQUEST_ID_HEADER)
    if isinstance(candidate, str) and _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return str(uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求净化/生成关联标识，绑定上下文并回传响应头。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _request_id_from(request.headers)
        bind_contextvars(request_id=request_id)
        # 把同一个 id 存进 scope，供最外层 500 handler 读取：
        # 本中间件在 finally 里 clear_contextvars()，早于 ServerErrorMiddleware
        # 触发 Exception handler，届时 contextvar 已空。
        request.scope.setdefault("state", {})[REQUEST_ID_SCOPE_KEY] = request_id
        try:
            response = await call_next(request)
            response.headers[_BASE_HTTP_REQUEST_ID_HEADER] = request_id
            return response
        finally:
            clear_contextvars()
