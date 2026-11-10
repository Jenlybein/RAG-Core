"""控制面「按 ``log_id`` 读回瀑布」的响应模型（#103）。

形状只回答三件事，多一个字段都会让界面开始猜：

- **这一次是谁**：``log_id`` 与 ``trace_id``（两者一一对应，``trace_id`` 也出现在检索响应
  的 ``metadata`` 里，所以调用方与服务端说的是同一根线）；
- **六段各多久**：``stages``（段名 + 毫秒数，按链路顺序；**没发生的段不补 0**）；
- **为什么没有段**：``enabled`` 是当前观测去向的开关状态，``reason`` 是
  ``sink_disabled`` / ``no_record`` 之一，``note`` 是给人看的一句说明（观测服务读不到时才有）。

``reason`` 为 ``null`` 当且仅当 ``stages`` 非空，这是服务层保证的不变式
（``app.services.retrieval_waterfall``）；前端据此决定显示瀑布还是显示「未启用 / 无记录」。

**这里不会出现任何凭据**：观测服务的 host 与两个 key 只存在于服务端的配置对象里，
本模型没有对应的字段——「没地方可写」比「记得不要写」更可靠。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.services.retrieval_waterfall import RetrievalWaterfall


class StageTimingData(BaseModel):
    """一段：名字与毫秒数。名字取自 ``stage_timing`` 的六段词表。"""

    name: str
    duration_ms: float


class RetrievalWaterfallData(BaseModel):
    """一次检索的瀑布（控制面只读）。"""

    log_id: str
    trace_id: str
    #: 当前 ``TRACE_SINK`` 是否不等于 ``none``。它说的是**现在**的配置，不是这条记录写下时
    #: 的配置——所以「关闭观测 + 本地有记录」会是 ``enabled=false`` 且 ``stages`` 非空。
    enabled: bool
    #: 段从哪读来的：``jsonl`` / ``langfuse``；一段都没有时是 ``null``。
    source: str | None
    #: 没有段的原因：``sink_disabled``（观测没开）/ ``no_record``（开了但没记上）；有段时为 ``null``。
    reason: str | None
    #: 一句给人看的说明（观测服务读不到时才有）；机器判据用 ``reason``，不要解析这句话。
    note: str | None
    #: 归属索引里记的总耗时。它与 ``stages`` 之和**不保证相等**：总耗时还包含链路之外的开销。
    latency_ms: int
    created_at: datetime | None
    stages: list[StageTimingData]


def waterfall_data(waterfall: RetrievalWaterfall) -> RetrievalWaterfallData:
    """服务层结论 → 响应模型（唯一一次转换，字段一一对应，不做二次计算）。"""

    return RetrievalWaterfallData(
        log_id=waterfall.log_id,
        trace_id=waterfall.trace_id,
        enabled=waterfall.enabled,
        source=waterfall.source,
        reason=waterfall.reason,
        note=waterfall.note,
        latency_ms=waterfall.latency_ms,
        created_at=waterfall.created_at,
        stages=[
            StageTimingData(name=stage.name, duration_ms=stage.duration_ms)
            for stage in waterfall.stages
        ],
    )
