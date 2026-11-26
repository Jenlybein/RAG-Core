"""用量统计的**纯函数**：分位估算与桶加法（第 14 步 #201）。

## 为什么分位数要单独一个文件

它被两个方向用到，而两个方向必须算出**同一个数**：

* 写侧（``app/services/usage_rollup``）把一天的明细压成 7 档直方图；
* 读侧（``app/api/routes/console_usage`` 之类）把若干天的直方图**相加**再反解分位。

一旦两边各写一份插值，长窗口的 P95 就会与短窗口对不上，而且没人看得出来。所以插值只有这一份。

## 为什么不是「把每天的 P95 平均」

那是**最常见的一种错**：算出来的数看起来像分位数、叫得出名字、还能画在图上，但它不是。
「每天 P95 的中位数」回答不了「这 30 天的 P95 是多少」——两个分布形状不同时，它连近似都不是。
所以汇总只存桶，分位永远从桶里现算。

## 误差是已知的、要写出来的

7 档（``<200 / 200-400 / 400-600 / 600-800 / 800-1000 / 1000-1500 / >1500``）之间按线性插值取，
最大误差是半个档宽——最宽的一档（``>1500``）没有上界，所以那一档里只能取 1500（下界）。
也就是说：**真值越大，估得越低**。响应里带 ``p95_source = "histogram"``，页面据此标注「近似」。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.usage_daily import LATENCY_BUCKETS_MS, LATENCY_BUCKET_COUNT

#: 每档的「代表值」：前六档取**区间中点**（下界与上界的平均），最后一档没有上界，
#: 取下界（宁可低估，不虚报慢）。
BUCKET_VALUES_MS: tuple[int, ...] = (
    *((lower + upper) // 2 for lower, upper in zip((0, *LATENCY_BUCKETS_MS), LATENCY_BUCKETS_MS)),
    LATENCY_BUCKETS_MS[-1],
)

assert len(BUCKET_VALUES_MS) == LATENCY_BUCKET_COUNT


def total_of(buckets: Sequence[int]) -> int:
    """样本总数。为 0 意味着「没有数据」，不是「0 毫秒」。"""

    return sum(buckets)


def percentile_from_buckets(buckets: Sequence[int], percentile: float) -> int | None:
    """从 7 档计数里反解分位（毫秒）；没有样本时返回 ``None``。

    ``None`` 与 ``0`` 的区别是这个模块唯一容易搞错的地方：没有样本必须返回 ``None``，
    因为调用方要据此显示「未统计」。返回 0 会让页面上出现一条落在 0 ms 的漂亮直线。
    """

    if len(buckets) != LATENCY_BUCKET_COUNT:
        raise ValueError(f"expected {LATENCY_BUCKET_COUNT} buckets, got {len(buckets)}")
    samples = total_of(buckets)
    if samples == 0:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")

    # 目标名次（1 起）：第 p 百分位落在第 ceil(p/100 * N) 个样本上。
    target = max(1, min(samples, -(-int(percentile * samples) // 100)))
    seen = 0
    for index, count in enumerate(buckets):
        seen += count
        if seen >= target:
            return BUCKET_VALUES_MS[index]
    return BUCKET_VALUES_MS[-1]


def add_buckets(*series: Sequence[int]) -> list[int]:
    """把若干天的直方图**逐档相加**。这正是「分位不能相加、桶可以」的那一半。"""

    total = [0] * LATENCY_BUCKET_COUNT
    for buckets in series:
        if len(buckets) != LATENCY_BUCKET_COUNT:
            raise ValueError(f"expected {LATENCY_BUCKET_COUNT} buckets, got {len(buckets)}")
        for index, count in enumerate(buckets):
            total[index] += count
    return total
