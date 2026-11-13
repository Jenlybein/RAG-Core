"""管理端「观测总览」：本地事实层 + 观测栈层。

分两层是有意的，也是这个模块最重要的设计决定：

- **本地事实层**（:mod:`app.repositories.observability_repository`）：`retrieval_traces` 与
  `retrieval_feedback` 里的结构化事实，**零外部依赖**。观测栈挂了、langfuse 没配、容器没起，
  这一层照样有数。
- **观测栈层**（Langfuse）：六段耗时的聚合。它有外部依赖，且只在 `TRACE_SINK=langfuse`
  且凭据齐全时才有意义，所以**整块可降级**，并如实标注 `source`。

为什么不做成「一个 iframe 把 Langfuse 嵌进来」：Langfuse 有自己的会话与
`X-Frame-Options`，跨域 cookie 也不通用；更要紧的是——**监控页面不该依赖被监控的系统活着**。
栈挂的那一刻，管理员恰恰最需要看到「最后一次成功的请求是几点」。

三条口径提醒，写在这里免得读数字的人误会：

1. 分位数的分母是**窗口内有记录的检索**，不是「发出去的请求」：如果写入归属索引本身失败，
   这一次不会出现在这里（那一层是旁路，见 ADR-0021）。
2. `degraded_rate` 在 `total=0` 时是 `None` 而不是 `0.0`——「没有数据」不等于「没有问题」。
3. 观测栈层与本地层**口径不同**（前者是 SDK 上报的 span，后者是我们自己量的毫秒），所以
   两边的数字不会严格相等；页面上分开呈现，不合并成一个数。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.observability.stage_timing import STAGE_ORDER
from app.repositories import observability_repository

#: 允许的窗口（小时）。做成白名单而不是任意整数：这是给「一屏看板」用的，
#: 放宽到任意值只会让慢查询有机会被随手构造出来。
WINDOW_CHOICES: tuple[int, ...] = (1, 6, 24, 72, 168)

STAGE_SOURCE_LOCAL = "local"
STAGE_SOURCE_LANGFUSE = "langfuse"
STAGE_SOURCE_NONE = "none"


def window_start(hours: int) -> datetime:
    """把「最近 N 小时」折成起点。非法值收敛到 24：**看板不该因为一个 query 参数报错。**"""

    safe = hours if hours in WINDOW_CHOICES else 24
    return datetime.now(timezone.utc) - timedelta(hours=safe)


async def overview(db: AsyncSession, *, hours: int) -> dict[str, Any]:
    """本地事实层的总览。观测栈是否可用**不影响**这个函数的返回。"""

    since = window_start(hours)
    totals = await observability_repository.totals(db, since=since)
    ratings = await observability_repository.rating_histogram(db, since=since)
    rated = sum(ratings.values())
    weighted = sum(score * count for score, count in ratings.items())
    return {
        "window_hours": hours if hours in WINDOW_CHOICES else 24,
        "since": since.isoformat(),
        **totals,
        "ratings": {
            "histogram": {str(score): ratings.get(score, 0) for score in range(1, 6)},
            "count": rated,
            # 分母是「打过分的条数」；没有评分时是 None，不是 0。
            "average": round(weighted / rated, 2) if rated else None,
        },
        "by_tenant": await observability_repository.by_tenant(db, since=since),
        "hourly": [
            {"bucket": item["bucket"].isoformat(), "total": item["total"]}
            for item in await observability_repository.hourly(db, since=since)
        ],
    }


def stack_status() -> dict[str, Any]:
    """观测栈现在是什么状态——**只回布尔与地址，绝不回密钥**。

    ``reachable`` 由路由层探测（这里不做网络调用，保持本模块可离线测）。
    """

    sink = settings.TRACE_SINK
    has_keys = bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)
    return {
        "sink": sink,
        "has_keys": has_keys,
        "langfuse_host": (
            (settings.LANGFUSE_PUBLIC_URL or settings.LANGFUSE_HOST).rstrip("/")
            if sink == "langfuse"
            else None
        ),
        # 能不能看到「观测栈层」的数据，是这两件事的交集。
        "stack_layer_available": sink == "langfuse" and has_keys,
    }


def stage_stats(*, hours: int, reader: Any | None = None) -> dict[str, Any]:
    """六段耗时的聚合。

    ``source`` 如实标注数据从哪来：``langfuse`` 表示来自观测栈，``none`` 表示这次取不到
    （没接观测栈 / 凭据不全 / 栈不可达）。**不把「取不到」画成 0** —— 一屏 0 毫秒的瀑布图
    会让人以为检索变快了。

    ``reader`` 是测试接缝：给一个返回预置 observation 列表的假读端，这条路径就能在离线
    套件里被逐项断言。
    """

    status = stack_status()
    if not status["stack_layer_available"]:
        return {"source": STAGE_SOURCE_NONE, "reason": "stack_not_configured", "stages": []}

    since = window_start(hours)
    try:
        observations = (
            reader(since)
            if reader is not None
            else _read_observations_since(since)
        )
    except Exception:  # noqa: BLE001 - 观测栈是旁路：取不到就降级，不冒到页面上
        return {"source": STAGE_SOURCE_NONE, "reason": "stack_unreachable", "stages": []}

    buckets: dict[str, list[float]] = {}
    for observation in observations:
        name = getattr(observation, "name", None)
        if name not in STAGE_ORDER:
            continue
        duration = _duration_of(observation)
        if duration is None:
            continue
        buckets.setdefault(name, []).append(duration)

    total_sum = sum(sum(values) for values in buckets.values())
    return {
        "source": STAGE_SOURCE_LANGFUSE,
        "reason": None,
        "stages": [
            {
                "name": name,
                "samples": len(buckets.get(name, [])),
                "p50": _percentile(buckets.get(name, []), 0.5),
                "p95": _percentile(buckets.get(name, []), 0.95),
                # 占「六段之和」的比例：一眼看出瓶颈在哪一段。
                "share": (sum(buckets.get(name, [])) / total_sum) if total_sum else None,
            }
            for name in STAGE_ORDER
        ],
    }


def _read_observations_since(since: datetime) -> list[Any]:
    """从观测栈按时间窗取 observation。延迟 import：没接观测栈时不加载 SDK。"""

    from app.observability.langfuse_reader import fetch_window_observations

    return fetch_window_observations(since=since)


def _duration_of(observation: Any) -> float | None:
    """耗时优先读 ``metadata.duration_ms``（我们自己写进去的那份，见 langfuse_sink）。

    SDK 自带的 ``latency`` 是秒、且对我们事后补记的 span 恒为 0，所以不用它。
    """

    metadata = getattr(observation, "metadata", None) or {}
    raw = metadata.get("duration_ms") if isinstance(metadata, dict) else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], q: float) -> float | None:
    """样本分位数（线性插值）。空样本回 ``None``——补 0 会把「没数据」说成「很快」。"""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)
