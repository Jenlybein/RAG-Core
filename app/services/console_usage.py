"""租户与平台侧的**用量看板**接口（第 14 步票 #202）。

四条路径，两个身份：

| 路径 | 谁 | 回答什么 |
| --- | --- | --- |
| ``GET /console/usage`` | 租户（可分 Key） | 我这个租户（或这把 Key）在这段时间用了多少 |
| ``GET /console/admin/usage/site`` | 管理员 | 全站多少、成功多少、哪条告警该看 |
| ``GET /console/admin/usage/tenants`` | 管理员 | 哪个租户用得最多、谁在出问题 |
| ``GET /console/admin/usage/tenants/{tenant_id}`` | 管理员 | 下钻到某一个租户 |

三条边界与观测页同一套：

- **读接口**：没有任何写操作；阈值在运行时配置里，不在这里改。
- **租户那条先按租户过滤**：`tenant_id` 只来自会话身份，**不接受** query 参数覆盖——
  否则它就成了一条「查别人的用量」的后门。
- **没数据不是 0**：``success_rate`` / ``p50_ms`` / ``p95_ms`` 在窗口内没有样本时返回 ``None``，
  ``source`` 会写明数据来自明细（``events``）还是日汇总（``daily``）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.api_key import ApiKey
from app.models.usage_daily import LATENCY_BUCKET_COUNT, UsageDaily
from app.models.usage_event import UsageEvent
from app.services.usage_rollup import UNKNOWN_CODE, day_bounds
from app.services.usage_stats import percentile_from_buckets

#: 窗口名 → 说明。白名单之外的取值一律收敛到 ``1d``（与观测页 ``hours`` 同一条做法）。
WindowName = Literal["1h", "1d", "7d", "30d"]

WINDOWS: dict[str, int] = {"1h": 60, "1d": 24, "7d": 7, "30d": 30}
DEFAULT_WINDOW = "1d"

#: 1 小时窗：6 个 10 分钟桶；1 天窗：24 个 1 小时桶。
TEN_MINUTES = 600
ONE_HOUR = 3_600


@dataclass(frozen=True)
class Window:
    """一个**已经对齐到网格**的窗口。

    为什么锚点要对齐：不对齐的话，同一分钟内连刷两次，「1 小时」的起点会跟着漂，
    图上的柱子会左右抖——用户看到的是「数据在变」，其实是窗口在动。
    """

    name: str
    since: datetime
    until: datetime
    bucket_seconds: int
    bucket_label: str
    count: int

    @property
    def uses_daily(self) -> bool:
        """7 / 30 天走日汇总（明细只留 90 天，但日汇总留 13 个月）。"""

        return self.bucket_seconds >= 86_400


def _failed_of(*, requests: int, ok: int, unknown: int) -> int:
    """「失败」只算**结局已知**的那些请求。

    迁移之前的行没有业务码：它们既不是成功也不是失败（ADR-0033 §3）。算进失败，
    页面上就会凭空长出一批「失败」，而真相是「那时候还没记账」。分母同一条纪律：
    成功率除以 ``requests - unknown``，不是 ``requests``。
    """

    return max(0, requests - ok - unknown)

def _now_local_naive() -> datetime:
    """列里存的形状：本地裸时间（``app.db.base.now``）。"""

    return datetime.now()


def parse_window(raw: str | None, *, now: datetime | None = None) -> Window:
    """把 ``?window=`` 解析成对齐好的窗口；非法值收敛到 ``1d``，不报 400。"""

    name = (raw or DEFAULT_WINDOW).strip()
    if name not in WINDOWS:
        name = DEFAULT_WINDOW
    moment = now or _now_local_naive()

    if name == "1h":
        # 6 个 10 分钟桶，最后一个就是「现在这一桶」（未走完）。
        floored = moment.replace(minute=moment.minute // 10 * 10, second=0, microsecond=0)
        since = floored - timedelta(minutes=50)
        return Window(name, since, floored + timedelta(minutes=10), TEN_MINUTES, "10m", 6)

    if name == "1d":
        # 当前这一小时 + 往前 23 小时。
        floored = moment.replace(minute=0, second=0, microsecond=0)
        since = floored - timedelta(hours=23)
        return Window(name, since, floored + timedelta(hours=1), ONE_HOUR, "1h", 24)

    days = WINDOWS[name]
    today = datetime.now(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    since, _ = day_bounds(first)
    _, until = day_bounds(today)
    return Window(name, since, until, 86_400, "1d", days)


# ───────────────────────────────────────────────────────── 明细窗口


def _bucket_index(column: Any, window: Window) -> Any:
    """桶序号：从 ``since`` 起按秒切。用相对秒数而不是 ``date_trunc``，桶就不会跟时区漂。"""

    elapsed = func.extract("epoch", column - window.since)
    return func.floor(elapsed / window.bucket_seconds)


def _label_of(index: int, window: Window) -> str:
    moment = window.since + timedelta(seconds=index * window.bucket_seconds)
    if window.bucket_seconds >= 86_400:
        return moment.strftime("%m-%d")
    if window.bucket_seconds >= ONE_HOUR:
        return moment.strftime("%H:00")
    return moment.strftime("%H:%M")


#: 「索引行」的端点标记（``chunk_ledger.INDEX_ENDPOINT``）。
#:
#: 为什么要把它们挡在请求统计之外（ADR-0035 决策三）：索引不是一次 API 调用——它由后台
#: Worker 触发、没有「哪把 Key 发起的」（``key_id`` 恒为 NULL）、也没有「成功/失败」的一半
#: 口径（失败那篇甚至不写明细）。把它们混进来，症状是三处同时失真：
#:
#: * ``requests`` 平白多出一批「一次请求」；
#: * ``success_rate`` 的分母被撑大，成功率看上去莫名其妙地掉；
#: * ``p50/p95`` 被一个几分钟的批次一起拉爆（而那次「请求」的耗时根本无从比较）。
#:
#: 但它**贡献 ``meter_chunk``**：第四本账的消耗只存在于这类行上，挡掉它就等于把要看的
#: 东西一起挡掉了。所以规则是「排除出请求统计，保留计量」——不是「排除整行」。
INDEX_ENDPOINT: Final[str] = "index"


def _exclude_index_rows(statement: Select[Any]) -> Select[Any]:
    """把索引行从**请求统计**里排除（``_totals_from_events`` / ``_buckets_from_events``）。"""

    return statement.where(
        or_(UsageEvent.endpoint.is_(None), UsageEvent.endpoint != INDEX_ENDPOINT)
    )


async def _chunk_tokens(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> int:
    """第四本账在窗口内的合计（token）。

    ## 为什么单独一条查询，而不是塞进 ``_totals_from_*``

    那两条合计查询**把索引行整行排除**（不对，它们排除的是索引行出请求统计——见
    ``_exclude_index_rows``）。而第四本账的消耗**只存在于索引行上**。两者互补：
    「请求统计不含索引」与「切块计量只来自索引」是同一条排除规则的两面。

    硬塞进同一条查询，只能二选一：要么把索引行读回来（请求数、成功率、分位一起失真），
    要么把它们排除（第四本账永远 0）。所以分成两条，语义各自干净。

    ``key_id`` 这里**没有意义**（索引行不带 Key），传进来的话查询仍是安全的（只会得到 0），
    但调用方不该指望它能按 Key 细分——索引不是某个 Key 发起的动作。
    """

    if window.uses_daily:
        statement = select(
            func.coalesce(func.sum(UsageDaily.meter_chunk), 0)
        ).where(
            UsageDaily.day >= window.since.date(),
            UsageDaily.day < window.until.date(),
            UsageDaily.endpoint == INDEX_ENDPOINT,
        )
        if tenant_id is not None:
            statement = statement.where(UsageDaily.tenant_id == tenant_id)
    else:
        statement = select(
            func.coalesce(func.sum(UsageEvent.meter_chunk), 0)
        ).where(
            UsageEvent.created_at >= window.since,
            UsageEvent.created_at < window.until,
            UsageEvent.endpoint == INDEX_ENDPOINT,
        )
        if tenant_id is not None:
            statement = statement.where(UsageEvent.tenant_id == tenant_id)
    return int((await db.execute(statement)).scalar_one_or_none() or 0)


async def _totals_from_events(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> dict[str, Any]:
    """窗口内合计：分位数下推给 Postgres（``percentile_cont``），别拉回 Python 排序。"""

    statement = select(
        func.count().label("requests"),
        func.count().filter(UsageEvent.code == 0).label("ok"),
        # 「失败」不单读一栏：它等于 ``requests - ok - unknown``（下面用 ``_failed_of`` 算）。
        # 多读一栏就等于给同一个数留两个来源，而它们迟早会对不上。
        func.count().filter(UsageEvent.code.is_(None)).label("unknown"),
        func.percentile_cont(0.5).within_group(UsageEvent.latency_ms).label("p50"),
        func.percentile_cont(0.95).within_group(UsageEvent.latency_ms).label("p95"),
        func.coalesce(func.sum(UsageEvent.meter_r), 0).label("meter_r"),
        func.coalesce(func.sum(UsageEvent.meter_rr), 0).label("meter_rr"),
        func.coalesce(func.sum(UsageEvent.meter_llm), 0).label("meter_llm"),
    ).where(UsageEvent.created_at >= window.since, UsageEvent.created_at < window.until)
    statement = _scope(statement, tenant_id=tenant_id, key_id=key_id)
    statement = _exclude_index_rows(statement)
    mapping = (await db.execute(statement)).one()._mapping
    requests = int(mapping["requests"] or 0)
    ok = int(mapping["ok"] or 0)
    unknown = int(mapping["unknown"] or 0)
    # 分母是**结局已知**的请求数：迁移之前的行没有码，既不算成功也不算失败。
    # 把 `unknown` 留在分子分母外面，成功率才不会因为「没人记过」而变成 0%。
    known = requests - unknown
    return {
        "requests": requests,
        "ok": ok,
        # 迁移前的行既不是成功也不是失败，所以「失败」不含它们（ADR-0033 §3）。
        "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
        "unknown_code": unknown,
        "success_rate": (ok / known) if known else None,
        "p50_ms": _as_float(mapping["p50"]),
        "p95_ms": _as_float(mapping["p95"]),
        "meter_r": int(mapping["meter_r"] or 0),
        "meter_rr": int(mapping["meter_rr"] or 0),
        "meter_llm": int(mapping["meter_llm"] or 0),
        # 第四本账另读一次（见 ``_chunk_tokens``）：索引行被排除出请求统计，所以这一栏
        # 不能从同一条查询里取。先给 0，``series`` 会用专用查询覆盖它。
        "meter_chunk": 0,
    }


def _scope(statement: Select[Any], *, tenant_id: str | None, key_id: str | None) -> Select[Any]:
    """租户/Key 过滤。管理员那条路两个都是 ``None``（跨租户）。"""

    if tenant_id is not None:
        statement = statement.where(UsageEvent.tenant_id == tenant_id)
    if key_id is not None:
        statement = statement.where(UsageEvent.key_id == key_id)
    return statement


async def _buckets_from_events(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> list[dict[str, Any]]:
    """按桶聚合明细：每桶的请求数、成功数、三本账与**精确**分位。"""

    index = _bucket_index(UsageEvent.created_at, window).label("bucket")
    statement = (
        select(
            index,
            func.count().label("requests"),
            func.count().filter(UsageEvent.code == 0).label("ok"),
            func.count().filter(UsageEvent.code.is_(None)).label("unknown"),
            func.coalesce(func.sum(UsageEvent.meter_r), 0).label("meter_r"),
            func.coalesce(func.sum(UsageEvent.meter_rr), 0).label("meter_rr"),
            func.coalesce(func.sum(UsageEvent.meter_llm), 0).label("meter_llm"),
            func.percentile_cont(0.95).within_group(UsageEvent.latency_ms).label("p95"),
        )
        .where(UsageEvent.created_at >= window.since, UsageEvent.created_at < window.until)
        .group_by(index)
        .order_by(index)
    )
    statement = _scope(statement, tenant_id=tenant_id, key_id=key_id)
    # 时间桶也排除索引行：否则某个 10 分钟里「请求数」会突然多出一百条，而它们全是索引
    statement = _exclude_index_rows(statement)
    rows = (await db.execute(statement)).all()

    found = {int(row._mapping["bucket"]): row._mapping for row in rows}
    out: list[dict[str, Any]] = []
    for position in range(window.count):
        mapping = found.get(position)
        requests = int(mapping["requests"] or 0) if mapping else 0
        ok = int(mapping["ok"] or 0) if mapping else 0
        unknown = int(mapping["unknown"] or 0) if mapping else 0
        out.append(
            {
                "bucket": _label_of(position, window),
                "requests": requests,
                "ok": ok,
                "unknown": unknown,
                "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
                "meter_r": int(mapping["meter_r"] or 0) if mapping else 0,
                "meter_rr": int(mapping["meter_rr"] or 0) if mapping else 0,
                "meter_llm": int(mapping["meter_llm"] or 0) if mapping else 0,
                "p95_ms": _as_float(mapping["p95"]) if mapping else None,
            }
        )
    return out


# ───────────────────────────────────────────────────────── 日汇总窗口


async def _daily_buckets(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> list[dict[str, Any]]:
    """从 ``usage_daily`` 读：每格相加，P95 由**相加后的桶**反解（误差约 13%）。"""

    statement = (
        select(
            UsageDaily.day,
            func.coalesce(func.sum(UsageDaily.requests), 0).label("requests"),
            func.coalesce(func.sum(UsageDaily.ok_requests), 0).label("ok"),
            func.coalesce(
                func.sum(UsageDaily.requests).filter(UsageDaily.code == UNKNOWN_CODE), 0
            ).label("unknown"),
            func.coalesce(func.sum(UsageDaily.meter_r), 0).label("meter_r"),
            func.coalesce(func.sum(UsageDaily.meter_rr), 0).label("meter_rr"),
            func.coalesce(func.sum(UsageDaily.meter_llm), 0).label("meter_llm"),
            *(
                func.coalesce(func.sum(getattr(UsageDaily, f"lat_b{index}")), 0).label(
                    f"lat_b{index}"
                )
                for index in range(LATENCY_BUCKET_COUNT)
            ),
        )
        .where(
            UsageDaily.day >= window.since.date(),
            UsageDaily.day < window.until.date(),
            # 与两条合计路径同一条排除规则：索引行不进请求统计。
            UsageDaily.endpoint != INDEX_ENDPOINT,
        )
        .group_by(UsageDaily.day)
        .order_by(UsageDaily.day)
    )
    if tenant_id is not None:
        statement = statement.where(UsageDaily.tenant_id == tenant_id)
    if key_id is not None:
        statement = statement.where(UsageDaily.key_id == key_id)
    rows = (await db.execute(statement)).all()

    found = {row._mapping["day"]: row._mapping for row in rows}
    out: list[dict[str, Any]] = []
    for position in range(window.count):
        day: date = (window.since + timedelta(days=position)).date()
        mapping = found.get(day)
        if mapping is None:
            out.append(
                {
                    "bucket": day.strftime("%m-%d"),
                    "requests": 0,
                    "ok": 0,
                    "unknown": 0,
                    "failed": 0,
                    "meter_r": 0,
                    "meter_rr": 0,
                    "meter_llm": 0,
                    "p95_ms": None,
                }
            )
            continue
        requests = int(mapping["requests"] or 0)
        ok = int(mapping["ok"] or 0)
        unknown = int(mapping["unknown"] or 0)
        buckets = [int(mapping[f"lat_b{index}"] or 0) for index in range(LATENCY_BUCKET_COUNT)]
        out.append(
            {
                "bucket": day.strftime("%m-%d"),
                "requests": requests,
                "ok": ok,
                "unknown": unknown,
                "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
                "meter_r": int(mapping["meter_r"] or 0),
                "meter_rr": int(mapping["meter_rr"] or 0),
                "meter_llm": int(mapping["meter_llm"] or 0),
                "p95_ms": percentile_from_buckets(buckets, 95),
            }
        )
    return out


async def _totals_from_daily(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> dict[str, Any]:
    """长窗口的合计：**桶相加**再反解分位（不是「把每天的 P95 再平均」）。"""

    statement = select(
        func.coalesce(func.sum(UsageDaily.requests), 0).label("requests"),
        func.coalesce(func.sum(UsageDaily.ok_requests), 0).label("ok"),
        # 「未统计」那一格在同一次聚合里读出来：迁移之前的行没有业务码，它们进分母但不进分子。
        func.coalesce(
            func.sum(UsageDaily.requests).filter(UsageDaily.code == UNKNOWN_CODE), 0
        ).label("unknown"),
        func.coalesce(func.sum(UsageDaily.meter_r), 0).label("meter_r"),
        func.coalesce(func.sum(UsageDaily.meter_rr), 0).label("meter_rr"),
        func.coalesce(func.sum(UsageDaily.meter_llm), 0).label("meter_llm"),
        *(
            func.coalesce(func.sum(getattr(UsageDaily, f"lat_b{index}")), 0).label(f"lat_b{index}")
            for index in range(LATENCY_BUCKET_COUNT)
        ),
    ).where(UsageDaily.day >= window.since.date(), UsageDaily.day < window.until.date())
    if tenant_id is not None:
        statement = statement.where(UsageDaily.tenant_id == tenant_id)
    if key_id is not None:
        statement = statement.where(UsageDaily.key_id == key_id)
    # 与明细路径同一条排除规则：索引行不进请求统计。
    statement = statement.where(
        UsageDaily.endpoint != INDEX_ENDPOINT
    )
    mapping = (await db.execute(statement)).one()._mapping
    requests = int(mapping["requests"] or 0)
    ok = int(mapping["ok"] or 0)
    buckets = [int(mapping[f"lat_b{index}"] or 0) for index in range(LATENCY_BUCKET_COUNT)]
    unknown = int(mapping["unknown"] or 0)
    known = requests - unknown
    return {
        "requests": requests,
        "ok": ok,
        "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
        "unknown_code": unknown,
        # 分母只算**结局已知**的请求：整窗都是老数据时它必须是 ``None``，
        # 而不是 0%——那会被读成「全挂了」。
        "success_rate": (ok / known) if known else None,
        "p50_ms": percentile_from_buckets(buckets, 50),
        "p95_ms": percentile_from_buckets(buckets, 95),
        "meter_r": int(mapping["meter_r"] or 0),
        "meter_rr": int(mapping["meter_rr"] or 0),
        "meter_llm": int(mapping["meter_llm"] or 0),
        # 第四本账另读一次（见 ``_chunk_tokens``）：索引行被排除出请求统计，所以这一栏
        # 不能从同一条查询里取。先给 0，``series`` 会用专用查询覆盖它。
        "meter_chunk": 0,
    }


# ───────────────────────────────────────────────────────────── 组装


async def series(
    db: AsyncSession,
    *,
    window: Window,
    tenant_id: str | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    """一条时间序列 + 合计。``source`` 说明数据从哪来，``p95_source`` 说明它有多准。"""

    if window.uses_daily:
        buckets = await _daily_buckets(db, window=window, tenant_id=tenant_id, key_id=key_id)
        totals = await _totals_from_daily(db, window=window, tenant_id=tenant_id, key_id=key_id)
        source, p95_source = "daily", "histogram"
        if totals["requests"] == 0 and await _has_events(
            db, window=window, tenant_id=tenant_id, key_id=key_id
        ):
            # 日汇总里一格都没有，可明细里这段时间**有**调用：那是 rollup 还没跑，
            # 不是「没有数据」。两者都得说清楚——「0 毫秒」与「算不出来」在监控里是两件事。
            p95_source = "histogram_missing"
    else:
        buckets = await _buckets_from_events(db, window=window, tenant_id=tenant_id, key_id=key_id)
        totals = await _totals_from_events(db, window=window, tenant_id=tenant_id, key_id=key_id)
        source, p95_source = "events", "exact"

    # 第四本账单独读（理由见 ``_chunk_tokens``）：覆盖掉 totals 里那个占位的 0。
    totals["meter_chunk"] = await _chunk_tokens(
        db, window=window, tenant_id=tenant_id, key_id=key_id
    )

    return {
        "window": window.name,
        "bucket": window.bucket_label,
        "since": window.since.isoformat(),
        "until": window.until.isoformat(),
        # 最后一个桶永远是「还没走完」的：今天的、这个小时的、这十分钟的。
        "partial_last_bucket": True,
        "source": source,
        "p95_source": p95_source,
        "totals": totals,
        "buckets": buckets,
    }


async def keys_of_tenant(
    db: AsyncSession, *, tenant_id: str, window: Window
) -> list[dict[str, Any]]:
    """租户下的 Key 逐个列出来：名字、掩码、调用、成功率、是否已删除。

    为什么 Key 那一边是**另读一次** ``api_keys`` 而不是 JOIN：历史行里的 ``key_id``
    可能指向一把已经删掉的 Key（没有外键约束，删 Key 也不删账）。INNER JOIN 会把那些行
    整个漏掉，而它们恰恰是「这个租户以前用过多少」的一部分；LEFT JOIN 又要处理 NULL
    名字。两边各读、在 Python 里合，规则只有一条：**对不上的就是历史 Key**。
    """

    statement: Select[Any]
    if window.uses_daily:
        statement = (
            select(
                UsageDaily.key_id,
                func.coalesce(func.sum(UsageDaily.requests), 0).label("requests"),
                func.coalesce(func.sum(UsageDaily.ok_requests), 0).label("ok"),
                func.coalesce(
                    func.sum(UsageDaily.requests).filter(UsageDaily.code == UNKNOWN_CODE), 0
                ).label("unknown"),
            )
            .where(
                UsageDaily.tenant_id == tenant_id,
                UsageDaily.day >= window.since.date(),
                UsageDaily.day < window.until.date(),
            )
            .group_by(UsageDaily.key_id)
        )
    else:
        statement = (
            select(
                UsageEvent.key_id,
                func.count().label("requests"),
                func.count().filter(UsageEvent.code == 0).label("ok"),
                func.count().filter(UsageEvent.code.is_(None)).label("unknown"),
            )
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.created_at >= window.since,
                UsageEvent.created_at < window.until,
            )
            .group_by(UsageEvent.key_id)
        )
    rows = (await db.execute(statement)).all()

    keys = {
        row.id: row
        for row in (await db.execute(select(ApiKey).where(ApiKey.tenant_id == tenant_id)))
        .scalars()
        .all()
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping
        key = keys.get(mapping["key_id"] or "")
        requests = int(mapping["requests"] or 0)
        ok = int(mapping["ok"] or 0)
        unknown = int(mapping["unknown"] or 0)
        known = requests - unknown
        out.append(
            {
                "key_id": mapping["key_id"] or "",
                # 对不上的就是「历史」：名字与掩码留空，页面显示「已删除」。
                "label": key.label if key is not None else None,
                "masked": f"rc_live_…{key.key_tail}" if key is not None and key.key_tail else None,
                "deleted": key is None,
                "requests": requests,
                "ok": ok,
                "unknown": unknown,
                "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
                "success_rate": (ok / known) if known else None,
            }
        )
    out.sort(key=lambda item: item["requests"], reverse=True)
    return out


async def tenants(db: AsyncSession, *, window: Window, limit: int = 20) -> list[dict[str, Any]]:
    """平台侧租户榜：按调用量倒序。只读日汇总（长窗口）或明细（短窗口），两条路同一形状。"""

    if window.uses_daily:
        statement = (
            select(
                UsageDaily.tenant_id,
                func.coalesce(func.sum(UsageDaily.requests), 0).label("requests"),
                func.coalesce(func.sum(UsageDaily.ok_requests), 0).label("ok"),
                # 「结局未知」的那一格：老数据没有业务码，不进成功率的分母。
                func.coalesce(
                    func.sum(UsageDaily.requests).filter(UsageDaily.code == UNKNOWN_CODE), 0
                ).label("unknown"),
                *(
                    func.coalesce(func.sum(getattr(UsageDaily, f"lat_b{index}")), 0).label(
                        f"lat_b{index}"
                    )
                    for index in range(LATENCY_BUCKET_COUNT)
                ),
            )
            .where(UsageDaily.day >= window.since.date(), UsageDaily.day < window.until.date())
            .group_by(UsageDaily.tenant_id)
            .order_by(func.sum(UsageDaily.requests).desc())
            .limit(limit)
        )
    else:
        statement = (
            select(
                UsageEvent.tenant_id,
                func.count().label("requests"),
                func.count().filter(UsageEvent.code == 0).label("ok"),
                func.count().filter(UsageEvent.code.is_(None)).label("unknown"),
                func.percentile_cont(0.95).within_group(UsageEvent.latency_ms).label("p95"),
            )
            .where(UsageEvent.created_at >= window.since, UsageEvent.created_at < window.until)
            .group_by(UsageEvent.tenant_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
    rows = (await db.execute(statement)).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping
        requests = int(mapping["requests"] or 0)
        ok = int(mapping["ok"] or 0)
        unknown = int(mapping["unknown"] or 0)
        known = requests - unknown
        p95: float | None
        if window.uses_daily:
            buckets = [int(mapping[f"lat_b{index}"] or 0) for index in range(LATENCY_BUCKET_COUNT)]
            p95 = percentile_from_buckets(buckets, 95)
        else:
            p95 = _as_float(mapping["p95"])
        out.append(
            {
                "tenant_id": mapping["tenant_id"],
                "requests": requests,
                "ok": ok,
                "failed": _failed_of(requests=requests, ok=ok, unknown=unknown),
                # 分母只算结局已知的那些：整列都是老数据时必须是 ``None``。
                "success_rate": (ok / known) if known else None,
                "p95_ms": p95,
            }
        )
    return out


async def alerts(
    db: AsyncSession, *, window: Window, site: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """「需要看一眼」那三条提示。**只提示，不处置**：阈值在运行时配置里，人能改。

    三条各管一件事，都用同一份站内合计去判：

    * ``quota_full``：窗口内有 ``20014``（额度用尽）——客户在被打回，先看是谁；
    * ``success_drop``：成功率比上一个同长窗口低了超过阈值（默认 3 个百分点）；
    * ``no_traffic``：这个窗口一次调用都没有——不是「一切正常」，是「可能没人用/没接上」。
    """

    data = site or await series(db, window=window)
    totals = data["totals"]
    found: list[dict[str, str]] = []

    quota_failed = await _count_code(db, window=window, code=20014)
    if quota_failed:
        found.append(
            {
                "kind": "quota_full",
                "level": "error",
                "message": f"窗口内 {quota_failed} 次因额度用尽被拒（20014）",
            }
        )

    rate = totals.get("success_rate")
    if rate is not None:
        previous = await _previous_window_rate(db, window=window)
        if previous is not None:
            drop = (previous - rate) * 100
            if drop >= float(settings.USAGE_ALERT_SUCCESS_DROP_PP):
                found.append(
                    {
                        "kind": "success_drop",
                        "level": "warn",
                        "message": (
                            f"成功率 {rate:.1%}，比上一个窗口（{previous:.1%}）低 {drop:.1f} 个百分点"
                        ),
                    }
                )

    if totals.get("requests", 0) == 0:
        found.append(
            {
                "kind": "no_traffic",
                "level": "info",
                "message": "这个窗口内没有任何调用记录",
            }
        )
        return found

    used = await _today_quota_usage(db)
    limit_hit = [
        item
        for item in used
        if item["percent"] is not None and item["percent"] * 100 >= settings.USAGE_ALERT_QUOTA_PCT
    ]
    for item in limit_hit[:3]:
        found.append(
            {
                "kind": "quota_pct",
                "level": "warn",
                "message": (
                    f"{item['tenant_id']} 的{item['meter_label']}账已用到 {item['percent']:.0%}"
                    f"（阈值 {settings.USAGE_ALERT_QUOTA_PCT}%）"
                ),
            }
        )
    return found


async def _count_code(
    db: AsyncSession,
    *,
    window: Window,
    code: int,
    tenant_id: str | None = None,
    key_id: str | None = None,
) -> int:
    """数某个业务码的行数。``UNKNOWN_CODE`` 在两张表里含义不同：

    明细里「不知道」是 ``NULL``（迁移之前的行），日汇总里是 ``-1``（rollup 归一的）。
    同一个常量两种形状是刻意保留的——明细表不该凭空多出一个哨兵值，它当初就是 NULL。
    """

    if window.uses_daily:
        statement = select(func.coalesce(func.sum(UsageDaily.requests), 0)).where(
            UsageDaily.day >= window.since.date(),
            UsageDaily.day < window.until.date(),
            UsageDaily.code == code,
        )
        if tenant_id is not None:
            statement = statement.where(UsageDaily.tenant_id == tenant_id)
        if key_id is not None:
            statement = statement.where(UsageDaily.key_id == key_id)
    else:
        code_filter = UsageEvent.code.is_(None) if code == UNKNOWN_CODE else UsageEvent.code == code
        statement = select(func.count()).where(
            UsageEvent.created_at >= window.since,
            UsageEvent.created_at < window.until,
            code_filter,
        )
        if tenant_id is not None:
            statement = statement.where(UsageEvent.tenant_id == tenant_id)
        if key_id is not None:
            statement = statement.where(UsageEvent.key_id == key_id)
    return int((await db.execute(statement)).scalar_one_or_none() or 0)


async def _has_events(
    db: AsyncSession, *, window: Window, tenant_id: str | None, key_id: str | None
) -> bool:
    """这个窗口里**明细**还有没有行（只问「有没有」，不数）。

    用途只有一个：长窗从日汇总读到 0 时，判断那是「真的没人调用」还是「rollup 还没跑」。
    两者在页面上必须分开——前者 P95 是「没有样本」，后者是「算不出来」。
    """

    statement = (
        select(UsageEvent.id)
        .where(UsageEvent.created_at >= window.since, UsageEvent.created_at < window.until)
        .limit(1)
    )
    statement = _scope(statement, tenant_id=tenant_id, key_id=key_id)
    return (await db.execute(statement)).first() is not None


async def _previous_window_rate(db: AsyncSession, *, window: Window) -> float | None:
    """上一个同长窗口的成功率。数据不够（明细没覆盖到）时返回 ``None``，不硬算。"""

    span = window.until - window.since
    start, end = window.since - span, window.since
    statement = select(
        func.count().label("requests"),
        func.count().filter(UsageEvent.code == 0).label("ok"),
        func.count().filter(UsageEvent.code.is_(None)).label("unknown"),
    ).where(UsageEvent.created_at >= start, UsageEvent.created_at < end)
    mapping = (await db.execute(statement)).one()._mapping
    requests = int(mapping["requests"] or 0)
    unknown = int(mapping["unknown"] or 0)
    # 同一个分母口径：老数据不进这个比值，否则「成功率掉了」会被一批没有码的行造出来。
    known = requests - unknown
    if not known:
        return None
    return int(mapping["ok"] or 0) / known


async def _today_quota_usage(db: AsyncSession) -> list[dict[str, Any]]:
    """今天（UTC）各租户三本账用掉多少 —— 与配额窗口同一个口径。

    额度取该租户**最宽松**那份密钥配置的日额度（``max``）：一个租户可以有多份配置，
    看板只是提示「快满了」，用一个从严的口径会天天误报。``percent`` 为 ``None``
    表示「这个租户没配额度」，不是 0%。
    """

    from app.models.key_policy import KeyPolicy

    today = datetime.now(timezone.utc).date()
    since, until = day_bounds(today)
    statement = (
        select(
            UsageEvent.tenant_id,
            func.coalesce(func.sum(UsageEvent.meter_r), 0).label("r"),
            func.coalesce(func.sum(UsageEvent.meter_rr), 0).label("rr"),
            func.coalesce(func.sum(UsageEvent.meter_llm), 0).label("llm"),
        )
        .where(UsageEvent.created_at >= since, UsageEvent.created_at < until)
        .group_by(UsageEvent.tenant_id)
    )
    usage = {row._mapping["tenant_id"]: row._mapping for row in (await db.execute(statement)).all()}
    limit_statement = select(
        KeyPolicy.tenant_id,
        func.max(KeyPolicy.retrieve_daily).label("r"),
        func.max(KeyPolicy.rerank_daily).label("rr"),
        func.max(KeyPolicy.llm_daily).label("llm"),
    ).group_by(KeyPolicy.tenant_id)
    limits = {
        row._mapping["tenant_id"]: row._mapping for row in (await db.execute(limit_statement)).all()
    }

    out: list[dict[str, Any]] = []
    for tenant_id, mapping in usage.items():
        limits_of_tenant: MappingABC[str, Any] = dict(limits.get(tenant_id) or {})
        for meter, column, label in (
            ("retrieve", "r", "检索"),
            ("rerank", "rr", "重排"),
            ("llm", "llm", "大模型"),
        ):
            limit = int(limits_of_tenant.get(column) or 0)
            used = int(mapping[column] or 0)
            out.append(
                {
                    "tenant_id": tenant_id,
                    "meter": meter,
                    "meter_label": label,
                    "used": used,
                    "limit": limit,
                    "percent": (used / limit) if limit else None,
                }
            )
    return out


def _as_float(value: Any) -> float | None:
    """``None`` 与 ``NaN`` 都归 ``None``：空窗口不能变成 0.0（那会被画成「很快」）。"""

    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def window_choices() -> Sequence[str]:
    """前端 segmented 的取值（顺序即显示顺序）。"""

    return tuple(WINDOWS)


#: 供给序列化层的一个别名：``usage_daily`` 里「未知码」的意思与明细侧同一个。
UNKNOWN: int = UNKNOWN_CODE
