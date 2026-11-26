"""按天汇总用量（第 14 步票 #201）：把 ``usage_events`` 压成 ``usage_daily``。

**它是脚本，不是定时任务**（与 #178 的保留策略脚本同一条约定）：仓库没有 Celery beat，
本步也不引入。怎么定时是运维的事（``cron`` / Windows 任务计划调它），代码里只有
「跑一次、退出、报统计」。

行为：

1. ``--days N``（默认 3）从今天往前跑 N 天——**今天算半天**，所以要连跑几天才不会漏；
2. 每一天按 ``created_at`` 游标翻页读明细、在内存里累加（:func:`app.services.usage_rollup
   .rollup_day`），再按五维主键 upsert 进 ``usage_daily``；
3. **幂等**：同一天跑三遍覆盖同一格，不会翻倍，所以运维不用记「我跑到哪了」；
4. ``--day YYYY-MM-DD`` 补跑某一天（对账时用）；``--dry-run`` 只统计、不写库。

退出码沿用共享规则（:func:`_ops_common.exit_code_for`）：有失败且一份都没写成功时为 1。
模块顶层不解析 argv、不连库、不建引擎，导入即安全。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import structlog

from app.core.logging import setup_logging
from app.db.transaction import async_transaction
from app.services.usage_rollup import oldest_day_n_ahead, rollup_day

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)

DEFAULT_DAYS = 3

NOTE = (
    'NOTE: "cells" counts (day, tenant, key, endpoint, code) rows written. '
    "Re-running the same day overwrites the same cells — the upsert is idempotent."
)


@dataclass
class Stats:
    """脚本自己的统计（形状对齐 ``_ops_common.OpsStats``）。"""

    failed: int = 0
    delivered: int = 0
    days: int = 0
    cells: int = 0
    errors: list[str] = field(default_factory=list)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Roll the usage event ledger up into per-day cells (usage_daily), including the "
            "latency histogram. Idempotent: re-running a day overwrites the same cells."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"how many days back to roll up, today included (default {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        default=None,
        help="roll up exactly this day (YYYY-MM-DD, UTC) instead of a range",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be written without touching the database",
    )
    return parser


async def _rollup(args: argparse.Namespace, stats: Stats) -> None:
    """一天一笔事务：写成功就提交，失败记 failed 继续下一天（一天坏了不拖累其它天）。"""

    from app.db.async_session import AsyncSessionLocal

    days = [args.day] if args.day else oldest_day_n_ahead(max(1, args.days))
    for day in days:
        try:
            async with AsyncSessionLocal() as db:
                async with async_transaction(db):
                    rows = await rollup_day(db, day)
                    if args.dry_run:
                        # dry-run 也要走同一条读路径，只是不回写：把事务回滚掉。
                        await db.rollback()
                    stats.cells += len(rows)
                    stats.delivered += 1
                    stats.days += 1
                    logger.info(
                        "usage day rolled up",
                        day=day.isoformat(),
                        cells=len(rows),
                        dry_run=args.dry_run,
                    )
        except Exception as exc:  # noqa: BLE001 - 一天坏了要记下来，继续下一天
            stats.failed += 1
            stats.errors.append(f"{day.isoformat()}: {type(exc).__name__}")
            logger.warning("usage day rollup failed", day=day.isoformat(), error=type(exc).__name__)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己开会话 → 统计与退出码。"""

    args = _build_parser().parse_args(argv)
    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就 InterfaceError）。
    # 运维脚本多半在开发机上被敲，所以这里换成 Selector 循环——与 ``purge_document_versions`` 同一行。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    stats = Stats()
    asyncio.run(_rollup(args, stats))
    print_summary(
        f"rollup_usage_daily ({'dry-run' if args.dry_run else 'apply'}) "
        f"{'day=' + args.day.isoformat() if args.day else f'days={args.days}'}",
        stats=stats,
        counters=(
            ("days rolled up", "days"),
            ("cells written", "cells"),
            ("failed days", "failed"),
        ),
        note=NOTE,
    )
    for error in stats.errors:
        print(f"  error: {error}")
    return exit_code_for(stats)


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
