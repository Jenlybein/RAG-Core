"""控制台瀑布图的**唯一判定点**：按 ``log_id`` 读回六段耗时（#103）。

一次请求要走三步，顺序不能换：

1. **归属先判**（:func:`waterfall_of` 第一段）。先用归属索引 ``retrieval_traces`` 按
   ``log_id`` 取一行、并校验它属于当前会话的租户；不属于就当**不存在**（``20016``），
   与仓库既有的越权口径一致（ADR-0006 / ``console_kb_service.read_document_content``）。
   这一步必须在读任何耗时之前：观测数据里没有租户，先读后判等于先把别人的耗时读进内存、
   再指望自己记得别返回。
2. **先本地、后观测栈**。本地 JSONL 是「没有观测栈」时也存在的记录（ADR-0021 决策 4），
   它命中就不再去问观测服务——控制台最常被打开的场合正是「本机起了一半」，
   为一个已知答案再打一次网络只会把排障变成等超时。反过来，``TRACE_SINK=none`` 时
   **一次都不问**：那套栈可能根本没在这个部署里，而「关闭观测」也不该在背后悄悄发起
   对外请求。
3. **两处都没有时给结构化结果，不是 404、也不是空数组**。``enabled`` 反映的是**当前**
   ``TRACE_SINK`` 配置，``reason`` 说明为什么没有段：``sink_disabled``（观测没开）与
   ``no_record``（开了，但这条没记上）是**两件事**，界面上的文案也因此不同——把「没配」
   显示成「没有耗时」是这条票要消灭的误读。

一条不变式：``reason`` 为 ``None`` **当且仅当** ``stages`` 非空。前端因此不需要自己拼
「什么时候该显示空白」的规则。

**读回不是旁路**：它失败会让这次请求失败一半（拿不到段），所以不吞异常；**唯一**被降级的是
观测服务读不到——那时返回本地结果（可能为空）并附一句 ``note``，绝不 500（票面明文）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.retrieval_trace import RetrievalTrace
from app.observability.jsonl_sink import read_recent_records
from app.observability.stage_timing import StageSpan, order_spans
from app.repositories import retrieval_trace_repository

logger = structlog.get_logger(__name__)

#: 段从哪来。``None`` 表示一段都没读到。
SOURCE_JSONL: Final[str] = "jsonl"
SOURCE_LANGFUSE: Final[str] = "langfuse"

#: 没有段时的两个原因（票面枚举，前端按它选文案）。
REASON_SINK_DISABLED: Final[str] = "sink_disabled"
REASON_NO_RECORD: Final[str] = "no_record"

#: 观测服务读不到时的固定说明。``{reason}`` 只填短码（``unreachable`` 等），
#: 不拼 SDK 的异常文本——那里可能有 host 与认证头。
NOTE_REMOTE_UNAVAILABLE: Final[str] = "观测服务读不到这条记录（{reason}）：本次只读了本地记录。"

#: 读观测服务的一等接缝：入参 ``trace_id``，返回段（按任意顺序，本模块统一排序）。
#: 生产实现是 :func:`app.observability.langfuse_reader.read_stage_spans`；测试注入假客户端。
RemoteStageReader = Callable[[str], Sequence[StageSpan]]


@dataclass(frozen=True)
class RetrievalWaterfall:
    """一次检索的瀑布：身份 + 六段 + 「为什么没有段」。

    ``latency_ms`` 来自归属索引（这次多慢），``stages`` 是六段各自多久。两者**不保证相等**：
    总耗时还包含链路之外的开销，而没发生的段不会出现（例如单模式下没有 ``fusion``）。
    把「总耗时」按段等比摊开是编数据，这里不干。
    """

    log_id: str
    trace_id: str
    enabled: bool
    source: str | None
    reason: str | None
    note: str | None
    latency_ms: int
    created_at: datetime | None
    stages: tuple[StageSpan, ...]


async def waterfall_of(
    db: AsyncSession,
    *,
    log_id: str,
    tenant_id: str,
    remote_reader: RemoteStageReader,
) -> RetrievalWaterfall:
    """按 ``log_id`` 取瀑布；归属不符 / 不存在一律 ``NOT_FOUND``（20016）。

    ``remote_reader`` 是**必填参数**而不是模块级默认值：读观测服务的那一步必须由组合根
    （路由的 ``Depends``）显式交进来，测试才有地方换掉它；写成默认值会让「这次到底去没去
    问观测栈」在运行时不可见。

    归属那一行读在**异步会话**上（票 #129），而本地 JSONL 与观测服务两处仍是同步 I/O：
    它们各自被 :func:`asyncio.to_thread` 挪出事件循环。文件读与网络读都属于「会等很久、
    但不等数据库」的那一类，留在循环上就是拿一个慢盘换掉整台进程的并发。
    """

    row = await retrieval_trace_repository.get_by_id_for_tenant_async(
        db, log_id, tenant_id=tenant_id
    )
    if row is None:
        # 跨租户与不存在同码：可区分的拒绝会泄露「这个 log_id 在别人那儿存在」。
        raise AppError(ErrorCode.NOT_FOUND)

    enabled = settings.TRACE_SINK != "none"
    trace_id = row.trace_id

    local = await asyncio.to_thread(_local_stages, trace_id)
    if local:
        return _waterfall(
            row, enabled=enabled, source=SOURCE_JSONL, reason=None, note=None, stages=local
        )

    note: str | None = None
    if enabled:
        remote, note = await asyncio.to_thread(_remote_stages, trace_id, reader=remote_reader)
        if remote:
            return _waterfall(
                row,
                enabled=enabled,
                source=SOURCE_LANGFUSE,
                reason=None,
                note=None,
                stages=remote,
            )

    return _waterfall(
        row,
        enabled=enabled,
        source=None,
        # 观测没开时说「未启用」，开了却空着时说「无记录」——两者在界面上的下一句话完全不同。
        reason=REASON_NO_RECORD if enabled else REASON_SINK_DISABLED,
        note=note,
        stages=(),
    )


def _waterfall(
    row: RetrievalTrace,
    *,
    enabled: bool,
    source: str | None,
    reason: str | None,
    note: str | None,
    stages: Sequence[StageSpan],
) -> RetrievalWaterfall:
    """把归属索引那一行与段拼成一次响应；字段来源只有这两处，不做二次计算。"""

    return RetrievalWaterfall(
        log_id=row.id,
        trace_id=row.trace_id,
        enabled=enabled,
        source=source,
        reason=reason,
        note=note,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
        stages=tuple(stages),
    )


def _local_stages(trace_id: str) -> tuple[StageSpan, ...]:
    """在本地 JSONL 的保留窗口里找这条 trace；没有（或一段都没有）返回空元组。

    倒着找：同一个 ``trace_id`` 只该有一行，真有重复时后写的那行才是「最近这次事实」。
    读整个窗口而不是按天猜文件：文件名里的日期是**写入那天**，而一次检索可能跨零点；
    把「今天是几号」的判断留在 ``read_recent_records`` 里一处，别在这里复制一遍。
    """

    records = read_recent_records(
        settings.TRACE_JSONL_DIR,
        days=settings.TRACE_JSONL_RETENTION_DAYS,
    )
    for record in reversed(records):
        if record.get("trace_id") != trace_id:
            continue
        return order_spans(_stages_of(record))
    return ()


def _stages_of(record: Mapping[str, Any]) -> list[StageSpan]:
    """把一行里的 ``spans`` 收成段列表；坏行只少几段，不让整个读取失败。

    这里**不按六段白名单过滤**：文件是我们自己写的，多出来的段名只可能是拼错了——那种时候
    原样显示比静默丢掉更能让人发现问题（观测栈那一侧反过来，见 ``langfuse_reader``）。
    """

    stages: list[StageSpan] = []
    for entry in record.get("spans") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        duration_ms = entry.get("duration_ms")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
            continue
        stages.append(StageSpan(name=name, duration_ms=float(duration_ms)))
    return stages


def _remote_stages(
    trace_id: str, *, reader: RemoteStageReader
) -> tuple[tuple[StageSpan, ...], str | None]:
    """问观测服务；读不到时返回空段 + 一句说明（**绝不抛**：票面要求这时不 500）。

    ``except Exception`` 而不是只接 :class:`LangfuseReadError`：这里本来就允许替身实现
    （测试、将来的另一家观测厂商），而「读回失败」的表现不取决于异常来自哪一层。范围只有
    这一句调用，我们自己的代码不在里面。
    """

    try:
        stages = reader(trace_id)
    except Exception as exc:  # noqa: BLE001 - 观测读不到只降级，不冒到响应上
        reason = getattr(exc, "reason", None) or type(exc).__name__
        # 日志里只有 id 与原因短码：观测读失败的原因可能夹带地址，问句一个字都不进来。
        logger.warning(
            "retrieval waterfall could not read the observation service",
            trace_id=trace_id,
            reason=reason,
        )
        return (), NOTE_REMOTE_UNAVAILABLE.format(reason=reason)
    return order_spans(stages), None
