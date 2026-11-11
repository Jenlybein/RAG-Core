"""观测旁路：六段耗时与三种去向（``none`` / ``jsonl`` / ``langfuse``，#102）。

这个包只有一个职责：把一次检索的**六段耗时**记录到某个去处，并且**永远不打扰主链路**。
它不读切片、不读问句，也不知道检索是怎么跑的——组合根把「这一次发生了什么」交进来：

    emit_retrieval_trace(trace_id=..., spans=..., attributes=...)

三条边界（ADR-0021）：

1. **原文不进来**。这里只接受耗时与结构化事实（模式、条数、降级原因、空结果原因），
   不接受问句、改写句与切片正文；``jsonl`` 去向的每一行都要能被「扫一遍，看有没有原文」
   这条测试守住。
2. **任何失败只留 warning**。SDK 连不上、目录不可写、序列化炸了，全部在门面里吞掉；
   检索照常返回，``trace_id`` 照样有值（它由 ``app.services.trace_identity`` 派生，
   与这里无关）。
3. **度量常开**。开关切换的是去向，不是「要不要计时」——收集器
   （:class:`app.observability.stage_timing.StageTimer`）由组合根无条件创建并显式传递。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import structlog

from app.core.config import settings
from app.observability.jsonl_sink import JsonlTraceSink
from app.observability.langfuse_sink import LangfuseTraceSink
from app.observability.sink import NoneTraceSink, TraceSink
from app.observability.stage_timing import StageSpan

logger = structlog.get_logger(__name__)

#: 去向失败时的固定消息。与归属索引那条 ``retrieval trace not recorded`` 刻意分开：
#: 一条说的是「业务库那一行没落下」，这一条说的是「观测栈 / 文件没收到这次计量」。
#: 测试按它筛记录，运维按它告警，所以它是**固定的一句话**，不拼异常文本。
OBSERVABILITY_SINK_FAILED_MESSAGE: Final[str] = "observability trace not recorded"
#: 推分数失败时的固定消息。与上面那条分开，是因为两者的排障含义不同：前者是「这次检索
#: 没留下痕迹」，后者是「这条反馈没进观测栈，但本地已经收下了」。
OBSERVABILITY_SCORE_FAILED_MESSAGE: Final[str] = "observability score not recorded"


def emit_retrieval_trace(
    *,
    trace_id: str,
    spans: Sequence[StageSpan],
    attributes: Mapping[str, object],
    query: str | None = None,
) -> None:
    """把这一次检索的六段耗时交给当前去向；**任何失败都只留一条 warning**。

    这是唯一一处把异常吞掉的地方。为什么只在这里兜：实现里再兜一层，就等于「这个去向
    真的坏了」永远只能从「一条 trace 都没有」这种间接现象里猜出来；而这里吞掉之后至少
    留下了一条能告警、能看出是哪一类去向的 warning。

    参数全是**已经算好的事实**：``trace_id`` 由组合根派生（与响应里那个值是同一个），
    ``spans`` 来自收集器，``attributes`` 是模式与结局。这里不做判定、不补字段——观测层
    一旦开始「顺便算点什么」，它就不再是旁路了。

    日志里只放 id 与错误**类型**：问句原文一个字都不许进日志（``tests/
    test_retrieval_log_hygiene.py`` 守着这条）。
    """

    sink = get_trace_sink()
    try:
        sink.record_trace(trace_id, attributes, query=query)
        for span in spans:
            sink.record_span(trace_id, span.name, span.duration_ms)
        sink.flush(trace_id)
    except Exception as exc:  # noqa: BLE001 - 旁路：任何异常都不许冒到检索返回值上
        logger.warning(
            OBSERVABILITY_SINK_FAILED_MESSAGE,
            trace_id=trace_id,
            sink=type(sink).__name__,
            error=type(exc).__name__,
        )


#: 当前去向。按配置项变化重建：切去向只改 ``.env``，链路代码一行不动（父 spec）。
_SINK: TraceSink | None = None
_SINK_KEY: tuple[object, ...] | None = None
#: 测试 / 嵌入方显式注入的去向（见 ``set_trace_sink``）；非空时压过配置。
_INJECTED: TraceSink | None = None


def get_trace_sink() -> TraceSink:
    """取当前去向；``TRACE_SINK`` 变了就重建，没变就复用（SDK 客户端不该每次请求新建）。

    键里带上 JSONL 的两个配置项：测试把目录指到临时目录、把保留天数改小之后，拿到的
    必须是按新配置造的那个实例，而不是上一次缓存下来的。
    """

    if _INJECTED is not None:
        return _INJECTED

    global _SINK, _SINK_KEY
    key = (settings.TRACE_SINK, settings.TRACE_JSONL_DIR, settings.TRACE_JSONL_RETENTION_DAYS)
    if _SINK is None or _SINK_KEY != key:
        _SINK = _build_sink()
        _SINK_KEY = key
    return _SINK


def emit_feedback_score(*, trace_id: str, score: int, comment: str | None = None) -> None:
    """把一次反馈的分数推给去向；**任何失败都只留一条 warning**（#105 决策 2）。

    它是旁路，而且**排在本地写入之后**：本地 ``retrieval_feedback`` 才是权威，观测栈里
    那条 score 只是方便在栈里按分数筛 trace。这条落差是本轮 code-review 查出来的——
    注释与 ADR 都说「推 score 是旁路」，实现却压根没推；少了这一步，#105 的「观测挂了
    反馈仍成功」就不再是被验证的行为，而是「因为根本没推，所以也没得挂」。

    ``jsonl`` 去向丢弃它：本地文件是「哪一步慢」的记录，不是分数台账（分数在业务库里）。
    """

    sink = get_trace_sink()
    try:
        sink.record_score(trace_id, score, comment=comment)
    except Exception as exc:  # noqa: BLE001 - 旁路：任何异常都不许冒到反馈响应上
        logger.warning(
            OBSERVABILITY_SCORE_FAILED_MESSAGE,
            trace_id=trace_id,
            sink=type(sink).__name__,
            error=type(exc).__name__,
        )


def _build_sink() -> TraceSink:
    """按 ``TRACE_SINK`` 造去向。**三个去向只在这里分叉一次**。

    名字不认识的兜底是 ``none`` 而不是抛：配置项在启动时已经被校验器挡过（配错就起不来，
    见 ``app.core.config``），走到这里还不认识只可能是有人直接改了 ``settings`` 对象；
    那时「静默不记」比「让检索失败」更接近旁路的本意。
    """

    kind = settings.TRACE_SINK
    if kind == "jsonl":
        return JsonlTraceSink(
            settings.TRACE_JSONL_DIR,
            retention_days=settings.TRACE_JSONL_RETENTION_DAYS,
        )
    if kind == "langfuse":
        # 客户端延迟到第一次用时才构造（见 ``langfuse_sink`` 模块文档）。
        return LangfuseTraceSink()
    return NoneTraceSink()


def set_trace_sink(sink: TraceSink) -> None:
    """显式注入一个去向（测试接缝，与 ``session_factory.set_session_factory`` 同一套做法）。"""

    global _INJECTED
    _INJECTED = sink


def reset_trace_sink() -> None:
    """清掉注入与缓存，回到「按配置重新决定」。"""

    global _SINK, _SINK_KEY, _INJECTED
    _INJECTED = None
    _SINK = None
    _SINK_KEY = None
