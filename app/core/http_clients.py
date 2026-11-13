"""进程级 HTTP 客户端：模型类流量共用连接池，收尾是确定性的。

## 为什么需要它

改造前每个 Provider 都自己新建 ``httpx.AsyncClient``：一次检索要建 3~4 个（嵌入、
改写、重排各一套），每一题都重做一次 TLS 握手，进程结束还刷一串
``Unclosed client session`` / ``RuntimeError: Event loop is closed``。复用的收益全部
来自「同一个连接池被连续请求命中」，所以客户端必须活得比一次请求长。

## 为什么现在只剩「一个槽」（ADR-0024）

第 10 步的形状是**按事件循环注册表 + 检索执行池**：池里 4 个 worker 各持一个循环，
Celery 又每个任务 ``asyncio.run`` 一次——同一个进程里同时活着好几个循环，客户端因此
必须按 loop 分家，收尾也得分「当前的那个 / 别的活着的 / 已经死掉的」三种去关。

第 11 步把这两件事都撤了：执行池删除（票 #127），Worker 换成一个**常驻循环**
（ADR-0023）。剩下的形状是「**一个进程一个循环**」（API 与 Worker 各自一个进程），
按 loop 的那张表就成了一张永远只有一行的表。于是这里压成一个槽：客户端本身，加上
「它属于哪个循环」。

**那条硬约束还在**：``httpx.AsyncClient`` 的连接池绑在创建它的循环上，跨循环复用的
下场是 ``RuntimeError: Event loop is closed``（ES Provider 付过一次真实代价）。所以
取客户端时先比对「槽里那个属于当前循环吗」，不是就重建、把旧的丢掉。生产里这个比对
恒为真（一个进程一循环），它只服务两种场合：测试（一个用例一个循环）与「脚本 / 异常
退出留下的旧客户端」。

**随注册表一起删掉的三段复杂度**：弱引用键（loop 回收即消失）、「逐 loop 关闭」、
「往另一个活着的循环投协程再等结果」。它们全是「同一进程里多个长活循环」的解法，
而那个前提已经不存在（进程内只有一个循环；跨进程本来就不共享内存）。

## 超时为什么不在客户端上写死

一个进程一个客户端，而用途各有各的预算（大模型 60s、嵌入 60s、重排 15s）。超时
因此由**调用方按用途传**（``client.post(..., timeout=...)``；OpenAI SDK 自己每请求
带一个超时）。客户端上的默认值只兜「调用方忘了传」的情况，故意给得宽。

## 同步客户端为什么也在

LangChain / OpenAI SDK 的包装对象（``ChatOpenAI`` / ``OpenAIEmbeddings``）在构造时
**即使拿到了异步客户端**也会顺手建一个同步 ``httpx.Client``。实测这台机器上
``httpx.Client()`` 一次约 43 ms（Windows 的代理环境探测），而包装对象是每次调用
现装的——不共享一个同步客户端，光这一项就够把「复用」的收益吃掉。同步客户端不绑
事件循环，所以进程级一个就够。

## 两种收尾

* :func:`aclose_http_client` —— 关掉异步那一个与进程级同步那一个（API 的 lifespan、
  ``WorkerLoop.shutdown`` 用它）；幂等，没有就是安静的 no-op；
* :func:`reset_http_clients` —— 测试接缝，只丢引用，不联网、不关闭。

**不要把 API 进程的客户端带进 Worker**：Worker 是另一个进程，这个槽在本进程内是空的，
任务体在自己的（常驻）循环里建、在 Worker 退出时关。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: 模型类流量共用的连接池参数（第 10 步的规格，写在一处）。100 条上限远大于一次请求里
#: 可能并发的端点数；20 条 keepalive 足够覆盖「改写 → 重排 → 下一次检索」的间隔。
MODEL_TRAFFIC_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

#: 客户端上的兜底超时。真实预算由调用方按用途传（见模块文档），这里只是「忘了传」时
#: 不落到 httpx 那个 5 秒默认值上。
DEFAULT_TIMEOUT_SECONDS = 60.0

#: 本进程的异步客户端，以及它属于哪个循环（跨循环复用会当场炸，见模块文档）。
_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None

#: 进程级同步客户端（不绑事件循环，见模块文档「同步客户端为什么也在」）。
_SYNC_CLIENT: httpx.Client | None = None


def get_http_client() -> httpx.AsyncClient:
    """取本进程的异步客户端；同一循环里多次取得到同一实例。

    没有运行中的循环时抛 ``RuntimeError``（``asyncio.get_running_loop`` 的原话）：
    那说明调用点不在任何请求 / 任务里，此时发出去的连接池没人负责收尾。
    """

    global _CLIENT, _CLIENT_LOOP

    loop = asyncio.get_running_loop()
    client = _CLIENT
    if client is not None and _CLIENT_LOOP is loop and not client.is_closed:
        return client

    if client is not None:
        # 生产里不会走到：一个进程一个循环。走到这里说明是测试换了一个循环，或者某个
        # 脚本 / 异常退出把上一个循环的客户端留在了槽里。旧的那个已经没人能 await 它
        # 的 aclose（循环关了），只能丢掉——这里留一条 warning 让「谁没关」可查。
        logger.warning(
            "rebuilding http client for a new event loop; the previous one was not closed here",
            previous_loop_closed=bool(_CLIENT_LOOP is not None and _CLIENT_LOOP.is_closed()),
        )

    _CLIENT = _build_async_client()
    _CLIENT_LOOP = loop
    # 一条 debug：**建了几个客户端**是「连接到底复用没复用」唯一可观察的事实。
    # 验收时数它：修好之后应当是一个进程一个，而不是每请求一个。
    logger.debug("http_client_created", loop=id(loop))
    return _CLIENT


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

    没有运行中的循环时（同步装配、排障、单测）只给同步那一个：异步侧交给框架自己，
    那种对象本来也不承载请求。
    """

    options: dict[str, Any] = {"http_client": get_sync_http_client()}
    try:
        options["http_async_client"] = get_http_client()
    except RuntimeError:
        return options
    return options


def _build_async_client() -> httpx.AsyncClient:
    """造一个绑定当前循环的客户端（唯一的构造点）。"""

    return httpx.AsyncClient(
        limits=MODEL_TRAFFIC_LIMITS,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


async def aclose_http_client() -> None:
    """关掉本进程那一个异步客户端与进程级同步客户端；幂等，没有就是安静的 no-op。

    API 的 lifespan 与 ``WorkerLoop.shutdown`` 各调一次：前者在请求都停了之后，
    后者在常驻循环**还活着**的时候（连接池必须在这个循环上关闭）。
    """

    global _CLIENT, _CLIENT_LOOP

    client = _CLIENT
    _CLIENT = None
    _CLIENT_LOOP = None
    if client is not None:
        await _aclose_quietly(client)
    _close_sync_client()


async def _aclose_quietly(client: Any) -> None:
    """关一个客户端；失败只记 warning——收尾阶段抛异常会盖掉真正的退出原因。"""

    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001 - 收尾不许抛
        logger.warning("failed to close http client", error=type(exc).__name__)


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
    """丢掉那一槽（**只给测试用**），不联网、不尝试关闭异步客户端。

    异步客户端没法在同步函数里 await；测试的每个用例都有自己的循环，槽里的旧客户端会
    随那个循环一起被丢掉（``get_http_client`` 在循环变化时会重建）。同步客户端是进程级的、
    关闭也不需要 await，这里顺手关掉，避免它在用例之间把连接留着。
    """

    global _CLIENT, _CLIENT_LOOP

    _CLIENT = None
    _CLIENT_LOOP = None
    _close_sync_client()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MODEL_TRAFFIC_LIMITS",
    "aclose_http_client",
    "get_http_client",
    "get_sync_http_client",
    "model_traffic_client_options",
    "reset_http_clients",
]
