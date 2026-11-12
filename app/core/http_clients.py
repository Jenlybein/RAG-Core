"""按事件循环的 HTTP 客户端注册表：模型类流量共用连接池，收尾是确定性的。

## 为什么需要它

改造前每个 Provider 都自己新建 ``httpx.AsyncClient``：一次检索要建 3~4 个（嵌入、
改写、重排各一套），每一题都重做一次 TLS 握手，进程结束还刷一串
``Unclosed client session`` / ``RuntimeError: Event loop is closed``。复用的收益全部
来自「同一个连接池被连续请求命中」，所以客户端必须活得比一次请求长。

## 为什么按 loop 而不是按进程

``httpx.AsyncClient`` 的连接池绑在**创建它的**事件循环上。ES Provider 已经为这条
付过一次真实代价：Celery 每个任务 ``asyncio.run`` 一次（每个任务一个 loop，跑完即
关闭），进程级单例的客户端在第二个任务里发请求会立刻

    RuntimeError: Event loop is closed

所以注册表的键是「当前正在运行的 loop」。不同 loop 各拿各的客户端、各自隔离；loop
被回收后条目由弱引用自动消失，不会把死循环留在表里。

## 超时为什么不在客户端上写死

一个 loop 一个客户端，而用途各有各的预算（大模型 60s、嵌入 60s、重排 15s）。超时
因此由**调用方按用途传**（``client.post(..., timeout=...)``；OpenAI SDK 自己每请求
带一个超时）。客户端上的默认值只兜「调用方忘了传」的情况，故意给得宽。

## 同步客户端为什么也在

LangChain / OpenAI SDK 的包装对象（``ChatOpenAI`` / ``OpenAIEmbeddings``）在构造时
**即使拿到了异步客户端**也会顺手建一个同步 ``httpx.Client``。实测这台机器上
``httpx.Client()`` 一次约 43 ms（Windows 的代理环境探测），而包装对象是每次调用
现装的——不共享一个同步客户端，光这一项就够把「复用」的收益吃掉。同步客户端不绑
事件循环，所以进程级一个就够。

## 三种收尾

* :func:`aclose_current_loop_clients` —— 当前 loop 名下的那一个（API 的 lifespan、
  Celery 任务体的 ``finally`` 用它）；
* :func:`aclose_all_clients` —— 尽力逐个 loop 关（进程收尾、测试用），关不掉的只记
  warning 不抛：收尾不该被一条噪音打断；
* :func:`reset_http_clients` —— 测试接缝，只清表，不联网、不关闭。

**不要把 API 进程的客户端带进 Worker**：Worker 是另一个进程，注册表在本进程内是空的，
任务体在自己的 loop 里建、也在自己的 loop 里关。
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: 模型类流量共用的连接池参数（这一票的规格，写在一处）。100 条上限远大于一次请求里
#: 可能并发的端点数；20 条 keepalive 足够覆盖「改写 → 重排 → 下一次检索」的间隔。
MODEL_TRAFFIC_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

#: 客户端上的兜底超时。真实预算由调用方按用途传（见模块文档），这里只是「忘了传」时
#: 不落到 httpx 那个 5 秒默认值上。
DEFAULT_TIMEOUT_SECONDS = 60.0

#: 一个 loop 一个异步客户端。弱引用键：loop 被回收后条目自动消失。
_CLIENTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)

#: 进程级同步客户端（不绑事件循环，见模块文档「同步客户端为什么也在」）。
_SYNC_CLIENT: httpx.Client | None = None


def get_http_client() -> httpx.AsyncClient:
    """取**当前正在运行的** loop 上的异步客户端；同一 loop 多次取得到同一实例。

    没有运行中的 loop 时抛 ``RuntimeError``（``asyncio.get_running_loop`` 的原话）：
    那说明调用点不在任何请求 / 任务里，此时发出去的连接池没人负责收尾。
    """

    loop = asyncio.get_running_loop()
    client = _CLIENTS.get(loop)
    if client is None:
        client = _build_async_client()
        _CLIENTS[loop] = client
    return client


def get_sync_http_client() -> httpx.Client:
    """进程级同步客户端：满足框架包装对象的构造，不承载异步流量。"""

    global _SYNC_CLIENT
    client = _SYNC_CLIENT
    if client is None or client.is_closed:
        client = httpx.Client(
            limits=MODEL_TRAFFIC_LIMITS,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
        )
        _SYNC_CLIENT = client
    return client


def model_traffic_client_options() -> dict[str, Any]:
    """给框架包装对象（``ChatOpenAI`` / ``OpenAIEmbeddings``）的连接参数。

    **只适用于 OpenAI 兼容的那几家**：另外两家的 SDK 不接受调用方提供的异步传输
    （``langchain_anthropic`` / ``langchain_google_genai`` 的模型字段里没有这两个名字），
    传进去会直接构造失败。调用点按供应商判断后再合并这组参数。

    没有运行中的 loop 时（同步装配、排障、单测）只给同步那一个：异步侧交给框架自己，
    那种对象本来也不承载请求。
    """

    options: dict[str, Any] = {"http_client": get_sync_http_client()}
    try:
        options["http_async_client"] = get_http_client()
    except RuntimeError:
        return options
    return options


def _build_async_client() -> httpx.AsyncClient:
    """造一个绑定当前 loop 的客户端（注册表的唯一构造点）。"""

    return httpx.AsyncClient(
        limits=MODEL_TRAFFIC_LIMITS,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


async def aclose_current_loop_clients() -> None:
    """关掉**当前 loop** 名下的客户端并把它从注册表摘掉；没有就是安静的 no-op。

    幂等：lifespan 退出与进程收尾各调一次也不会双重关闭（第二次表里已经没有了）。
    """

    loop = asyncio.get_running_loop()
    client = _CLIENTS.pop(loop, None)
    if client is not None:
        await _aclose_quietly(client, loop=loop)


async def aclose_all_clients() -> None:
    """尽力关掉注册表里**每一个** loop 的客户端（进程收尾 / 测试）。

    关不掉的（loop 已经关闭）只记 warning：进程收尾的唯一职责是「把还能关的关掉」，
    因为一个已经死掉的 loop 抛出来而中断，等于放弃剩下那些还活着的客户端。
    """

    current = asyncio.get_running_loop()
    for loop, client in list(_CLIENTS.items()):
        _CLIENTS.pop(loop, None)
        if loop is current:
            await _aclose_quietly(client, loop=loop)
        elif loop.is_closed():
            logger.warning(
                "cannot close http client of a closed event loop",
                loop_closed=True,
            )
        elif loop.is_running():
            # 另一个线程里跑着的 loop：把关闭动作投过去再等结果。
            await _aclose_from_another_loop(loop, client)
        else:
            await _aclose_through_idle_loop(loop, client)

    _close_sync_client()


async def _aclose_quietly(client: Any, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """关一个客户端；失败只记 warning——收尾阶段抛异常会盖掉真正的退出原因。"""

    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001 - 收尾不许抛
        logger.warning(
            "failed to close http client",
            error=type(exc).__name__,
            loop_closed=bool(loop is not None and loop.is_closed()),
        )


async def _aclose_from_another_loop(loop: asyncio.AbstractEventLoop, client: Any) -> None:
    """把 ``aclose()`` 投到另一个正在运行的 loop 上执行，等它完成（有界等待）。"""

    try:
        future = asyncio.run_coroutine_threadsafe(_aclose_quietly(client, loop=loop), loop)
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - 收尾不许抛
        logger.warning(
            "failed to close http client on a foreign running loop",
            error=type(exc).__name__,
        )


async def _aclose_through_idle_loop(loop: asyncio.AbstractEventLoop, client: Any) -> None:
    """在一个**空闲** loop 上跑一次 ``aclose()``。

    必须丢到线程里跑：当前线程已经有一个 loop 在跑，而同线程的 ``run_until_complete``
    会以「Cannot run the event loop while another loop is running」失败。协程也在那个
    线程里现造，这样即使 ``run_until_complete`` 立刻失败也不会留下「从未 await 的协程」。
    """

    def _close() -> None:
        async def _run() -> None:
            await _aclose_quietly(client, loop=loop)

        loop.run_until_complete(_run())

    try:
        await asyncio.to_thread(_close)
    except Exception as exc:  # noqa: BLE001 - 收尾不许抛
        logger.warning(
            "failed to close http client through its idle event loop",
            error=type(exc).__name__,
        )


def _close_sync_client() -> None:
    """关掉进程级同步客户端（本地资源，不发网络）。"""

    global _SYNC_CLIENT
    client = _SYNC_CLIENT
    _SYNC_CLIENT = None
    if client is None or client.is_closed:
        return
    try:
        client.close()
    except Exception as exc:  # noqa: BLE001 - 收尾不许抛
        logger.warning(
            "failed to close the process-wide sync http client", error=type(exc).__name__
        )


def reset_http_clients() -> None:
    """清空注册表（**只给测试用**），不联网、不尝试关闭异步客户端。

    异步客户端没法在同步函数里 await；测试的每个用例都有自己的 loop，条目会随 loop
    一起被回收。同步客户端是进程级的、关闭也不需要 await，这里顺手关掉，避免它在
    用例之间把连接留着。
    """

    _CLIENTS.clear()
    _close_sync_client()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MODEL_TRAFFIC_LIMITS",
    "aclose_all_clients",
    "aclose_current_loop_clients",
    "get_http_client",
    "get_sync_http_client",
    "model_traffic_client_options",
    "reset_http_clients",
]
