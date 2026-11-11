"""``langfuse`` 的**读回**：按 ``trace_id`` 取回六段耗时（#103）。

写侧在 :mod:`app.observability.langfuse_sink`（一次检索 → 六段 span）；这一侧回答的是反方向
的问题：「这条 trace 在栈里到底记了哪几段、各多久」。控制台瀑布图在本地 JSONL 没有命中时
才走这里。

四条边界，每一条都对应一个踩过或必然踩到的坑：

1. **凭据不出这个模块**。``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` 只用来构造客户端；
   返回给上层的只有「段名 + 毫秒数」。异常也只带一个 :data:`REASON_*` 里的短码，**不拼异常
   文本**——SDK 的报错里有 URL 与认证头，拼进响应就等于把凭据形状下发到浏览器。
2. **用的是 REST 客户端，不是 tracer**（``langfuse.api.LangfuseAPI``，而不是
   ``langfuse.Langfuse``）：读一条历史 trace 不需要建 exporter、不需要后台线程，也不该因为
   「这次读」往栈里写任何东西。写侧仍然用完整客户端（它要发 span）。
3. **只认六段的名字**（:data:`app.observability.stage_timing.STAGE_ORDER`）：同一个 trace 上
   可能有别的 instrumentation 挂的 observation，把它们一起画进瀑布，等于让「六段」这个
   契约随部署漂移。不认识的段名在这里被丢掉，而**在文件里**（自己写的 JSONL）一个都不丢。
4. **失败一律抛** :class:`LangfuseReadError`，由调用方降级成「只有本地结果 + 一句说明」。
   这一层不吞异常：吞掉之后上层就分不清「栈里没有这条」与「根本连不上」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from app.core.config import settings
from app.observability.stage_timing import STAGE_ORDER, StageSpan, order_spans

#: 失败原因的三个短码。它们会进响应里的 ``note``，所以是**固定词表**，不带异常文本。
REASON_NOT_CONFIGURED: Final[str] = "not_configured"
REASON_UNREACHABLE: Final[str] = "unreachable"
REASON_BAD_RESPONSE: Final[str] = "bad_response"

#: 六段之外不会有我们的 span；给一个够用的上限，避免一条畸形 trace 拖回几千条。
_OBSERVATION_LIMIT: Final[int] = 100


class LangfuseReadError(RuntimeError):
    """读观测服务失败：没配凭据、连不上、返回体不认识。

    ``reason`` 是给人和机器看的短码（:data:`REASON_NOT_CONFIGURED` 等），**不是** SDK 的异常
    文本：那条文本里可能带着 host 与认证头，而它会一路走到浏览器的 ``note`` 里。
    """

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        #: 只进服务端日志（例如给排障用），**不进响应**。
        self.detail = detail


def _new_client() -> Any:
    """按配置构造 REST 客户端；import 延迟到真正要读的时候。

    与写侧同一个理由（``langfuse_sink`` 模块文档）：``none`` / ``jsonl`` 去向与整个离线套件
    因此既不加载 langfuse，也不读它那几个配置项。
    """

    from langfuse.api import LangfuseAPI

    return LangfuseAPI(
        base_url=settings.LANGFUSE_HOST,
        # ``LangfuseAPI`` 的 basic auth 就是「公钥 + 私钥」这一对；私钥只在这一行之后存在。
        username=settings.LANGFUSE_PUBLIC_KEY,
        password=settings.LANGFUSE_SECRET_KEY,
    )


def read_stage_spans(trace_id: str, *, client: Any | None = None) -> tuple[StageSpan, ...]:
    """按 ``trace_id`` 取回六段耗时，按链路顺序排好；一段都没有时返回空元组。

    ``client`` 是测试接缝：给一个记下参数、返回预置 observation 的假客户端，这条路径就能在
    不发网络、不起栈的情况下被逐项断言。生产路径传 ``None``，每次现构造一个 REST 客户端
    ——它只是 httpx 的一层壳，没有后台线程，比缓存一个实例更不容易在长期运行里带着过期凭据。

    耗时优先读 ``metadata.duration_ms``：那是**我们自己量出来**并写进去的数（见
    ``langfuse_sink`` 模块文档「事后补记」）。``metadata`` 里没有时退到
    ``end_time - start_time``——这个差是库里的两个时刻，不需要猜单位；而 SDK 自己的
    ``latency`` 单位是秒，猜错就静默差 1000 倍，所以不用它。
    """

    if client is None:
        client = _resolve_client()

    observations = fetch_observations(trace_id, client=client)
    if observations is None:
        raise LangfuseReadError(REASON_BAD_RESPONSE, detail="no data")

    stages: list[StageSpan] = []
    for observation in observations:
        name = getattr(observation, "name", None)
        if name not in STAGE_ORDER:
            continue
        duration_ms = _duration_of(observation)
        if duration_ms is None:
            continue
        stages.append(StageSpan(name=name, duration_ms=duration_ms))
    return order_spans(stages)


def fetch_observations(trace_id: str, *, client: Any | None = None) -> list[Any] | None:
    """取一条 trace 的全部 observation，**带上 ``name`` 与 ``metadata``**。

    这是本模块里唯一该发 ``observations.get_many`` 的地方：``fields`` 的两个坑
    （漏了它 metadata 永远是 ``None``；只写 ``metadata`` 会把 ``name`` 白名单滤掉）
    在别处重演过一次——``scripts/export_eval_cases.py`` 当初自己写了一份取数逻辑，
    于是 #110 的低分导出永远报「观测服务里没有问句原文」。收在这里，别人就不必再记。

    失败一律抛 :class:`LangfuseReadError`（与 ``read_stage_spans`` 同一套语义）：
    「栈里没有」与「连不上」必须分得开。
    """

    if client is None:
        client = _resolve_client()

    try:
        # ``fields`` 不能省，而且**两个都要点名**：
        #
        # - 不传 ``fields`` 时 v2 的 observations 接口**默认不返回 metadata**，于是
        #   ``metadata`` 永远是 ``None``，每一段都退回 ``end_time - start_time``——而那对
        #   时刻在「事后补记」里几乎相同，结果是瀑布图**全部显示 0.0 ms**，看起来像
        #   「观测栈里没有耗时数据」。
        # - 传了 ``fields`` 它就是**白名单**：只写 ``["metadata"]`` 会把 ``name`` 一起滤掉，
        #   于是每一段都因为「名字不认识」被丢弃，瀑布图变成空数组。
        #
        # ``basic`` 带 ``name``（也带 type / level），``metadata`` 带我们自己写进去的
        # ``duration_ms`` 与 ``query``。这条组合是实机对着自托管 v4 栈试出来的（2026-11-11）：
        # 同一个 trace 带上它之后六段立刻回真值
        # （0.154 / 12.246 / 8483.89 / 474.568 / 9372.298 / 0.09 ms）。
        #
        # ``expand_metadata=True`` 也不能省：**metadata 默认按 200 字符截断**（实机量过：
        # 482 字的 ``query`` 只回 200 字）。对瀑布图无所谓——``duration_ms`` 只有几个字符；
        # 但 #110 的低分导出要拿 ``metadata.query`` 当评测题的问句原文，截断就等于静默
        # 产出一道残缺的题。加上它之后 482 字完整回来。
        response = client.observations.get_many(
            trace_id=trace_id,
            limit=_OBSERVATION_LIMIT,
            fields=["basic", "metadata"],
            expand_metadata=True,
        )
    except LangfuseReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何网络 / 认证失败都归为「读不到」
        raise LangfuseReadError(REASON_UNREACHABLE, detail=type(exc).__name__) from exc

    return getattr(response, "data", None)


def _resolve_client() -> Any:
    """构造客户端；凭据没配时**不发请求**，直接报「没配」。"""

    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        raise LangfuseReadError(REASON_NOT_CONFIGURED)
    try:
        return _new_client()
    except Exception as exc:  # noqa: BLE001 - SDK 装不上 / 参数不认都归为「读不到」
        raise LangfuseReadError(REASON_UNREACHABLE, detail=type(exc).__name__) from exc


def _duration_of(observation: Any) -> float | None:
    """一段的毫秒数：先读我们自己写进去的 ``metadata.duration_ms``，再退回两个时刻之差。"""

    metadata = getattr(observation, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("duration_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)

    started = getattr(observation, "start_time", None)
    ended = getattr(observation, "end_time", None)
    if isinstance(started, datetime) and isinstance(ended, datetime):
        return round((ended - started).total_seconds() * 1000, 3)
    return None
