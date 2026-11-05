"""请求关联标识：纯 ASGI 中间件，横跨根应用与挂载的子应用。

不继承 ``BaseHTTPMiddleware``：它用 anyio 内存流缓冲响应体，会拖坏流式传输。
这里只在 ``http.response.start`` 上追加响应头，body 消息原样放行。

标识只进日志字段（``structlog.contextvars`` 绑定，由 T2 的
``merge_contextvars`` 带到每条记录）与响应头，绝不进响应体。
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, MutableMapping
from uuid import uuid4

from structlog.contextvars import bind_contextvars, clear_contextvars

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_NAME = b"x-request-id"


class RequestContextMiddleware:
    """为每个 HTTP 请求净化/生成关联标识，绑定上下文并回传响应头。"""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        bind_contextvars(request_id=request_id)
        # ── T5: 把同一个 id 存进 scope，供最外层 500 handler 读取 ──
        # 本中间件在 finally 里 clear_contextvars()，早于 ServerErrorMiddleware
        # 触发 Exception handler，届时 contextvar 已空；scope 会原样传到那个 handler。
        scope.setdefault("state", {})["request_id"] = request_id
        # ── T5 结束 ──
        try:

            async def send_with_request_id(message: Message) -> None:
                if message["type"] == "http.response.start":
                    # 生成新列表而不是改下游的 raw_headers：同一个 Response 对象
                    # 若被复用，原地追加会让响应头逐次累积。
                    message["headers"] = [
                        *message.get("headers", []),
                        (_HEADER_NAME, request_id.encode("ascii")),
                    ]
                await send(message)

            await self.app(scope, receive, send_with_request_id)
        finally:
            clear_contextvars()


def _request_id_from(scope: Scope) -> str:
    """沿用合法的客户端标识，否则生成 UUID；非法值绝不写进日志。"""

    for name, value in scope.get("headers", []):
        if name.lower() == _HEADER_NAME:
            # latin-1 逐字节还原，非 ASCII 字节随后一定被正则拒绝，
            # 且原始值从不进入日志（防控制字符/日志注入）。
            candidate = value.decode("latin-1")
            return candidate if _REQUEST_ID_RE.fullmatch(candidate) else str(uuid4())
    return str(uuid4())
