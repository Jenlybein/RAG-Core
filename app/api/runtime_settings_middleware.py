"""运行时配置的热更新中间件：**每个请求**进业务之前刷新一次覆盖层。

为什么是中间件而不是后台线程 / 定时器：一个常驻定时器会多出一个没人管生命周期（谁在
应用关闭时停它？测试里怎么保证它不跨用例？），而这个刷新本来就是 PER-REQUEST 的语义——
「下一个请求按新值跑」只需要在请求入口做一次时间比较。代价是每个请求一次比较（几十
纳秒），收益是**不存在**一个「值什么时候生效」与请求无关的第三态。

为什么放进线程池：真正到期时它会读一次库（阻塞 I/O）。直接在这个 ``async`` 函数里做，
会把整个事件循环卡住一次；放线程池里则只让这一个请求多等一次读库。

顺序：中间件只碰配置，不碰身份。放在 ``RequestContextMiddleware`` **内层**，于是刷新时
的告警日志能带上 ``request_id``（那个中间件负责绑定它）。
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.services import runtime_settings


class RuntimeSettingsRefreshMiddleware(BaseHTTPMiddleware):
    """每次请求前调用一次 :func:`app.services.runtime_settings.ensure_runtime_overrides`。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        # 刷新永不带病阻塞请求：ensure 内部吞掉一切异常并保留上一份好值。
        await run_in_threadpool(runtime_settings.ensure_runtime_overrides)
        return await call_next(request)
