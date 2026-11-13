"""管理端观测聚合：**跨租户**读 ``retrieval_traces`` 与 ``retrieval_feedback``。

为什么单独一个模块而不是塞进 ``retrieval_trace_repository``：那个模块的每个方法都**强制带
``tenant_id``**（ADR-0006 决策 1），而这里要回答的是平台级问题（「全体租户今天一共查了多少、
谁在拖慢」）。把「不带租户」的查询塞进租户仓储里，就等于给那条约定开了一个例外口子——
下一个调用者会以为它也能这么用。分成两个模块，例外就只存在于这一个文件里，且只被管理员
路由使用。

所有函数都返回**纯字典**：这一层只做聚合，不返回 ORM 行——聚合结果没有「实体」可言，
把 Row 交给上层会诱使调用方在 Python 里再算一遍（那就有两个口径了）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.retrieval_feedback import RetrievalFeedback
from app.models.retrieval_trace import RetrievalTrace


async def totals(db: AsyncSession, *, since: datetime) -> dict[str, Any]:
    """窗口内的总量、耗时分位与降级/空结果计数。

    ``percentile_cont`` 直接下推给 Postgres：把行拉回 Python 再排序，在「一屏监控」这个
    量级上会随着表长大而越来越慢，而分位数恰恰是监控最常刷新的那个数。
    """

    statement = select(
        func.count().label("total"),
        func.percentile_cont(0.5).within_group(RetrievalTrace.latency_ms).label("p50"),
        func.percentile_cont(0.95).within_group(RetrievalTrace.latency_ms).label("p95"),
        func.max(RetrievalTrace.latency_ms).label("worst"),
        func.count().filter(RetrievalTrace.degraded_reason.is_not(None)).label("degraded"),
        func.count().filter(RetrievalTrace.empty_reason.is_not(None)).label("empty"),
    ).where(RetrievalTrace.created_at >= since)
    row = (await db.execute(statement)).one()
    mapping = row._mapping
    return {
        "total": int(mapping["total"] or 0),
        "latency": {
            "p50": _as_float(mapping["p50"]),
            "p95": _as_float(mapping["p95"]),
            "worst": _as_float(mapping["worst"]),
        },
        "degraded": int(mapping["degraded"] or 0),
        "empty": int(mapping["empty"] or 0),
    }


async def by_tenant(db: AsyncSession, *, since: datetime, limit: int = 10) -> list[dict[str, Any]]:
    """按调用量排的租户榜，附带各自的降级率与平均耗时。"""

    statement = (
        select(
            RetrievalTrace.tenant_id,
            func.count().label("total"),
            func.count().filter(RetrievalTrace.degraded_reason.is_not(None)).label("degraded"),
            func.avg(RetrievalTrace.latency_ms).label("avg_latency"),
        )
        .where(RetrievalTrace.created_at >= since)
        .group_by(RetrievalTrace.tenant_id)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = (await db.execute(statement)).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping
        total = int(mapping["total"] or 0)
        degraded = int(mapping["degraded"] or 0)
        out.append(
            {
                "tenant_id": mapping["tenant_id"],
                "total": total,
                "degraded": degraded,
                # 分母为 0 时给 None 而不是 0.0：那是「没有数据」，不是「没有问题」。
                "degraded_rate": (degraded / total) if total else None,
                "avg_latency_ms": _as_float(mapping["avg_latency"]),
            }
        )
    return out


async def hourly(db: AsyncSession, *, since: datetime) -> list[dict[str, Any]]:
    """按小时分桶的调用量，用来画趋势线。

    桶里没有数据的小时**不补 0**：补 0 会把「服务没起来那段时间」画成一条掉到底的折线，
    看起来像流量骤降。前端遇到空档就断开折线。
    """

    bucket = func.date_trunc("hour", RetrievalTrace.created_at)
    statement = (
        select(bucket.label("bucket"), func.count().label("total"))
        .where(RetrievalTrace.created_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    )
    return [
        {"bucket": row._mapping["bucket"], "total": int(row._mapping["total"] or 0)}
        for row in (await db.execute(statement)).all()
    ]


async def rating_histogram(db: AsyncSession, *, since: datetime) -> dict[int, int]:
    """1~5 分各有多少条。**未评分的不出现**（与旁证「未评分不进分母」同一口径）。"""

    statement = (
        select(RetrievalFeedback.score, func.count().label("total"))
        .where(RetrievalFeedback.created_at >= since)
        .group_by(RetrievalFeedback.score)
    )
    return {int(row._mapping["score"]): int(row._mapping["total"] or 0) for row in (await db.execute(statement)).all()}


def _as_float(value: Any) -> float | None:
    """``None`` 原样返回：调用方据此区分「没有数据」与「数值就是 0」。"""

    return None if value is None else round(float(value), 2)
