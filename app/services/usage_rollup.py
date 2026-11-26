"""用量汇总：把一天的明细压成 ``usage_daily``（第 14 步票 #201）。

## 为什么要有这一步

``usage_events`` 一行一次请求，只能撑住最近 90 天（保留策略会清更老的）。而看板要回答
「过去 30 天怎么走」「上个月这个租户多少」。所以按天压一次：一行代表
「某天 · 该租户 · 该 Key · 该端点 · 该业务码」这一格，含三本账与 **7 档延迟直方图**。

**为什么是直方图而不是每天一个 P95**：分位数不能相加（见 :mod:`app.services.usage_stats`）。

## 为什么在 Python 里聚合，而不是一条 ``INSERT ... SELECT``

一条 SQL 更快，但那份「怎么算一格」的逻辑就只存在于 SQL 字符串里——测不了、读不出、
改一处要同时改读侧。这里的批处理是**离线脚本**，一天的量级是「每租户几千行」，
把一天的列读出来、在内存里累加、再批量 upsert，代价可以接受；换来的是
:func:`accumulate` 这个纯函数可以被单测钉死（含边界、含 ``""`` 归一）。

单批上限 :data:`PAGE_SIZE` 只是为了避免一次把几十万行读进内存：按 ``created_at`` 游标翻页，
每页聚合后并入同一个累加器。

## 幂等

``upsert`` 靠 ``usage_daily`` 的五维主键（``day`` + 四个维度）：同一天重跑覆盖同一格，
不会翻倍。所以「先跑昨天的、再跑前天的」「重跑三遍」都安全——运维不需要记住自己跑到哪了。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_daily import LATENCY_BUCKET_COUNT, UsageDaily, bucket_of
from app.models.usage_event import UsageEvent

#: 一页读多少行明细。太大会把内存顶起来，太小会让往返次数变多。
PAGE_SIZE = 5_000

#: 迁移之前的行没有业务码（``NULL``）：汇总里记 -1（「未统计」），**不计入成功**。
UNKNOWN_CODE = -1

#: 标量累加列（桶不在这里：它们要按索引展开，见 :meth:`Cell.as_row`）。
SCALAR_COLUMNS = (
    "requests",
    "ok_requests",
    "meter_r",
    "meter_rr",
    "meter_llm",
    # 第四本账（切块，按 token 计）。**它不进 requests / ok_requests**：索引行不是一次
    # API 调用，混进请求数会让成功率与 P50/P95 一起失真（见 ``_fetch_page`` 的排除规则）。
    "meter_chunk",
    "lat_sum_ms",
)

#: 写进 ``usage_daily`` 的全部指标列 = 标量 + 7 档桶。
METRIC_COLUMNS = (*SCALAR_COLUMNS, *(f"lat_b{index}" for index in range(LATENCY_BUCKET_COUNT)))


@dataclass
class Cell:
    """``usage_daily`` 的一格：四个维度 + 一个业务码。"""

    tenant_id: str
    key_id: str
    endpoint: str
    code: int

    requests: int = 0
    ok_requests: int = 0
    meter_r: int = 0
    meter_rr: int = 0
    meter_llm: int = 0
    #: 切块 token（只有索引行会带它，单位是 token 不是次数）。
    meter_chunk: int = 0
    lat_sum_ms: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * LATENCY_BUCKET_COUNT)

    def add(self, row: Mapping[str, Any]) -> None:
        """把一行明细并进来。P50/P95 不在这里算——只累加桶。"""

        self.requests += 1
        if row.get("code") == 0:
            self.ok_requests += 1
        self.meter_r += int(row.get("meter_r") or 0)
        self.meter_rr += int(row.get("meter_rr") or 0)
        self.meter_llm += int(row.get("meter_llm") or 0)
        self.meter_chunk += int(row.get("meter_chunk") or 0)
        latency = int(row.get("latency_ms") or 0)
        self.lat_sum_ms += latency
        self.buckets[bucket_of(latency)] += 1

    def as_row(self, day: date) -> dict[str, Any]:
        """摊平成 ``usage_daily`` 的一行（列名与模型一致，供 upsert 用）。"""

        row: dict[str, Any] = {
            "day": day,
            "tenant_id": self.tenant_id,
            "key_id": self.key_id,
            "endpoint": self.endpoint,
            "code": self.code,
        }
        row.update({name: getattr(self, name) for name in SCALAR_COLUMNS})
        for index, count in enumerate(self.buckets):
            row[f"lat_b{index}"] = count
        return row


def _dimension(value: Any) -> str:
    """维度归一：``None`` 与空串都是「历史汇总」，统一成 ``""``。

    为什么必须归一：``usage_daily`` 的主键用空串表示「不知道」，而 ``NULL != NULL``
    会让唯一约束形同虚设——同一格被插入两次，汇总就不再幂等。
    """

    return "" if value is None else str(value)


def _code_of(row: Mapping[str, Any]) -> int:
    """业务码归一：迁移之前写下的行没有码（``NULL``），归成 :data:`UNKNOWN_CODE`。

    为什么不归成 0（成功）：那些行**没有说过自己成功**，它们只是没记。把它们算进分子，
    成功率会虚高，而且没人看得出来。归成 -1 之后它们仍然计入「调用次数」（它们确实是调用），
    只是不进「成功」——这正是「未统计 ≠ 0」在汇总表里的落法。
    """

    code = row.get("code")
    return UNKNOWN_CODE if code is None else int(code)


def accumulate(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str, int], Cell]:
    """把明细行累加成「一格一份」的字典。**纯函数**：不碰库、不看时钟。"""

    cells: dict[tuple[str, str, str, int], Cell] = {}
    for row in rows:
        identity = (
            _dimension(row.get("tenant_id")),
            _dimension(row.get("key_id")),
            _dimension(row.get("endpoint")),
            _code_of(row),
        )
        cell = cells.get(identity)
        if cell is None:
            cell = Cell(
                tenant_id=identity[0],
                key_id=identity[1],
                endpoint=identity[2],
                code=identity[3],
            )
            cells[identity] = cell
        cell.add(row)
    return cells


def _local_naive(moment: datetime) -> datetime:
    """把「带时区的时刻」换成本地裸时间——**库里存的就是这个形状**。

    ``app.db.base.now()`` 返回的是 ``datetime.now()``（本地裸时间），所以 ``usage_events
    .created_at`` 一直是本地时间。日界仍然是 **UTC 零点**（与配额窗口同一个口径），
    只是要换算成列里那个形状：东八区的一天从本地 08:00 开始。直接拿 UTC 裸时间去比，
    整张图会平移八小时，而且没有任何报错。
    """

    return moment.astimezone().replace(tzinfo=None)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """一天的 UTC 区间 ``[起, 止)``，换算成列里存的本地裸时间。"""

    start_utc = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return _local_naive(start_utc), _local_naive(start_utc + timedelta(days=1))


async def _fetch_page(
    db: AsyncSession, *, start: datetime, end: datetime, after: str | None
) -> list[Mapping[str, Any]]:
    """读一页明细。用 ``id`` 做游标：``created_at`` 有并列，``id`` 唯一。"""

    statement = (
        select(
            UsageEvent.id,
            UsageEvent.tenant_id,
            UsageEvent.key_id,
            UsageEvent.endpoint,
            UsageEvent.code,
            UsageEvent.meter_r,
            UsageEvent.meter_rr,
            UsageEvent.meter_llm,
            UsageEvent.meter_chunk,
            UsageEvent.latency_ms,
        )
        .where(UsageEvent.created_at >= start, UsageEvent.created_at < end)
        .order_by(UsageEvent.id)
        .limit(PAGE_SIZE)
    )
    if after is not None:
        statement = statement.where(UsageEvent.id > after)
    result = await db.execute(statement)
    return [dict(row._mapping) for row in result]


async def rollup_day(db: AsyncSession, day: date) -> list[dict[str, Any]]:
    """把 ``day`` 这一天的明细压成若干格并 upsert；返回写入的那些行（便于打印统计）。"""

    start, end = day_bounds(day)
    cells: dict[tuple[str, str, str, int], Cell] = {}
    after: str | None = None
    while True:
        page = await _fetch_page(db, start=start, end=end, after=after)
        if not page:
            break
        for identity, partial in accumulate(page).items():
            target = cells.get(identity)
            if target is None:
                cells[identity] = partial
            else:
                target.requests += partial.requests
                target.ok_requests += partial.ok_requests
                target.meter_r += partial.meter_r
                target.meter_rr += partial.meter_rr
                target.meter_llm += partial.meter_llm
                target.lat_sum_ms += partial.lat_sum_ms
                for index, count in enumerate(partial.buckets):
                    target.buckets[index] += count
        if len(page) < PAGE_SIZE:
            break
        after = str(page[-1]["id"])

    rows = [cell.as_row(day) for cell in cells.values()]
    # 先删后插（同一笔事务）：这样「跑一遍」等于「这一天现在是这个真相」。
    # 只做 upsert 会留下**算不出来的旧格**——口径改过之后，旧的那一格还在，而新的计算
    # 不再产出它；那种残留没有任何迹象，看板上就是多出来的一个数。
    await db.execute(delete(UsageDaily).where(UsageDaily.day == day))
    if rows:
        # 唯一键仍用 ON CONFLICT 兜底：两个人同时跑同一天时不会撞主键。
        statement = pg_insert(UsageDaily).values(rows)
        update = {name: getattr(statement.excluded, name) for name in (*METRIC_COLUMNS,)}
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    UsageDaily.day,
                    UsageDaily.tenant_id,
                    UsageDaily.key_id,
                    UsageDaily.endpoint,
                    UsageDaily.code,
                ],
                set_=update,
            )
        )
    return rows


async def purge_events(db: AsyncSession, *, before: datetime) -> int:
    """删 ``before`` 之前的明细，返回删除行数。"""

    from sqlalchemy import delete

    result = await db.execute(delete(UsageEvent).where(UsageEvent.created_at < before))
    return int(result.rowcount or 0)


async def purge_daily(db: AsyncSession, *, before: date) -> int:
    """删 ``before`` 之前的日汇总（保留期比明细长得多：13 个月对 90 天）。"""

    from sqlalchemy import delete

    result = await db.execute(delete(UsageDaily).where(UsageDaily.day < before))
    return int(result.rowcount or 0)


def oldest_day_n_ahead(days: int, *, today: date | None = None) -> list[date]:
    """``--days N`` 要跑哪几天：**今天算半天**，所以从 ``N-1`` 天前跑到今天。"""

    base = today or datetime.now(timezone.utc).date()
    return [base - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
