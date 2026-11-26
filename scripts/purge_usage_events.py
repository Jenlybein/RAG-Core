"""用量保留策略（第 14 步票 #201）：明细留 90 天，按天汇总留 13 个月。

**为什么两个保留期不一样长**：``usage_events`` 一行一次请求，量随调用量线性长；
``usage_daily`` 一行一格（天 × 租户 × Key × 端点 × 码），一天最多几十行。看板要的是
「过去 30 天 / 上个月」，13 个月够覆盖同比与季度对账；明细只服务「最近哪天出过事」，
90 天足够，再久的数据只能靠汇总看——**它们回答的是两个不同的问题**，不需要一样长。

**它是脚本，不是定时任务**（仓库没有 Celery beat，本步也不引入）：``cron`` / 任务计划
调它，代码里只有「跑一次、退出、报统计」。

行为：

1. 明细：``--keep-days 90``（默认）之前的 ``usage_events`` 整行删除；
2. 汇总：``--keep-days-daily 395``（默认，约 13 个月）之前的 ``usage_daily`` 整行删除；
3. ``--dry-run``（**默认**）：只数「会删多少行」，一个字都不删；
4. ``--apply`` 才真删，且**两张表各一笔事务**——明细删成功、汇总删失败不会留下半个状态。

删之前请确认那天的汇总已经跑过（``scripts/rollup_usage_daily.py``）：先汇总后删明细，
顺序反了就等于把那段历史丢掉了。这条顺序写在这里，也写在 ADR-0033 里。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import structlog
from sqlalchemy import func, select

from app.core.logging import setup_logging
from app.db.base import now
from app.db.transaction import async_transaction
from app.models.usage_daily import UsageDaily
from app.models.usage_event import UsageEvent

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)

#: 明细保留多少天。
DEFAULT_KEEP_DAYS = 90
#: 按天汇总保留多少天（约 13 个月）。
DEFAULT_KEEP_DAYS_DAILY = 395

NOTE = (
    "NOTE: run rollup_usage_daily.py BEFORE this script. Deleting detail first would lose the "
    "history that was never rolled up. --dry-run is the default; --apply actually deletes."
)


@dataclass
class Stats:
    failed: int = 0
    delivered: int = 0
    events_seen: int = 0
    events_deleted: int = 0
    daily_seen: int = 0
    daily_deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Drop usage detail older than N days and per-day cells older than M days. "
            "Dry-run by default."
        )
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=DEFAULT_KEEP_DAYS,
        help=f"keep this many days of usage_events (default {DEFAULT_KEEP_DAYS})",
    )
    parser.add_argument(
        "--keep-days-daily",
        type=int,
        default=DEFAULT_KEEP_DAYS_DAILY,
        help=f"keep this many days of usage_daily (default {DEFAULT_KEEP_DAYS_DAILY})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (without this flag the script only counts)",
    )
    return parser


async def _count_events(db, before: datetime) -> int:
    statement = select(func.count()).select_from(UsageEvent).where(UsageEvent.created_at < before)
    return int((await db.execute(statement)).scalar_one())


async def _count_daily(db, before) -> int:
    statement = select(func.count()).select_from(UsageDaily).where(UsageDaily.day < before)
    return int((await db.execute(statement)).scalar_one())


def cutoffs(*, keep_days: int, keep_days_daily: int) -> tuple[datetime, date]:
    """两个截止点：明细用**本地裸时间**，日汇总用**UTC 日历天**。

    为什么是两个时钟而不是一个，见 ``_purge`` 里的注释；抽成纯函数是因为这是**整条保留
    策略里唯一会多删数据的地方**（删错了不可逆），必须有单测钉着它的时区口径。
    """

    event_cutoff = now() - timedelta(days=keep_days)
    daily_cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days_daily)).date()
    return event_cutoff, daily_cutoff


async def _purge(args: argparse.Namespace, stats: Stats) -> None:
    from app.db.async_session import AsyncSessionLocal
    from app.services.usage_rollup import purge_daily, purge_events

    # 两个截止点来自**两个不同的时钟**，这不是笔误：
    #
    # * ``usage_events.created_at`` 存的是**本地裸时间**（``app.db.base.now()``），所以明细的
    #   截止点必须用同一个时钟算——拿 UTC 算完抹掉 tzinfo，在东八区会多删 8 小时明细，
    #   而且删掉就没了（ADR-0033 §5 说的就是这件事）；
    # * ``usage_daily.day`` 是**UTC 日历天**的标签（``usage_rollup.day_bounds`` 那么写的），
    #   所以日汇总的截止点必须按 UTC 的今天算。
    event_cutoff, daily_cutoff = cutoffs(
        keep_days=args.keep_days, keep_days_daily=args.keep_days_daily
    )

    try:
        async with AsyncSessionLocal() as db:
            stats.events_seen = await _count_events(db, event_cutoff)
            if args.apply and stats.events_seen:
                async with async_transaction(db):
                    stats.events_deleted = await purge_events(db, before=event_cutoff)
            stats.delivered += 1
    except Exception as exc:  # noqa: BLE001 - 一张表失败要记下来，另一张照跑
        stats.failed += 1
        stats.errors.append(f"usage_events: {type(exc).__name__}")
        logger.warning("usage detail purge failed", error=type(exc).__name__)

    try:
        async with AsyncSessionLocal() as db:
            stats.daily_seen = await _count_daily(db, daily_cutoff)
            if args.apply and stats.daily_seen:
                async with async_transaction(db):
                    stats.daily_deleted = await purge_daily(db, before=daily_cutoff)
            stats.delivered += 1
    except Exception as exc:  # noqa: BLE001
        stats.failed += 1
        stats.errors.append(f"usage_daily: {type(exc).__name__}")
        logger.warning("usage daily purge failed", error=type(exc).__name__)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就 InterfaceError）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    stats = Stats()
    asyncio.run(_purge(args, stats))
    print_summary(
        f"purge_usage_events ({'apply' if args.apply else 'dry-run'}) "
        f"keep_days={args.keep_days} keep_days_daily={args.keep_days_daily}",
        stats=stats,
        counters=(
            ("detail rows in window out", "events_seen"),
            ("detail rows deleted", "events_deleted"),
            ("daily cells in window out", "daily_seen"),
            ("daily cells deleted", "daily_deleted"),
            ("tables failed", "failed"),
        ),
        note=NOTE,
    )
    for error in stats.errors:
        print(f"  error: {error}")
    return exit_code_for(stats)


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
