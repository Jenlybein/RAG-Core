"""卡死文档恢复脚本（F5 的手工兜底）：重新投递租约已过期的 ``PROCESSING`` 文档。

为什么需要它：Worker 崩溃 / 被 kill 会把文档留在 ``status=3``，日志里既没有
「跑完」也没有「失败」，也就是教程里那个最经典的坑——「永远卡在处理中且不报错」。
自动侧由租约放行（``document_repository.DOCUMENT_LEASE_SECONDS``）：超过租约仍
停在 3 的文档允许被重新抢占 / 删除 / 重建；这条脚本给运维一个**主动**手段，
不必干等下一次自然重投。

行为（对应设计契约 §4）：
1. 查 ``status == PROCESSING AND (index_claimed_at IS NULL OR index_claimed_at <
   now - lease)``，可选 ``--kb-id`` 缩小范围，缺省扫全库；
2. 逐篇 ``try_claim_for_indexing``（不带 ``allow_success``）：抢不到记 skipped，
   抢到立刻 ``commit`` 释放行锁；
3. ``enqueue_document`` 重新投递，行自带的 ``tenant_id`` / ``kb_id`` 原样带上；
4. 打印 ``requeued / skipped / failed`` 统计。

**打印的 ``requeued`` 是「已重新投递」数量，不是「已完成」数量。** 异步链路没有
「跑完」的同步感知，要确认终态请用 ``GET /documents?kb_id=<id>`` 或直接查库。

供运维使用：``uv run python scripts/requeue_stuck_documents.py [--kb-id <id>]
[--lease-seconds 600] [--dry-run]``。退出码：有 failed 且一份都没救回时为 1，
否则 0。模块顶层不解析 argv、不连接数据库，导入即安全，测试可以直接 import 函数。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.repositories import document_repository
from app.repositories.document_repository import DOCUMENT_LEASE_SECONDS
from app.services.index_dispatch import enqueue_document

logger = structlog.get_logger(__name__)

REQUEUED_NOT_COMPLETED_NOTE = (
    'NOTE: "requeued" counts documents handed to the Broker, not documents completed. '
    "The async pipeline has no synchronous completion signal; check the final status with "
    "GET /documents?kb_id=<id> (or query the database directly)."
)


@dataclass(frozen=True)
class RequeueStats:
    """一次卡死恢复的投递统计；``requeued`` 是已重新投递数，不是已完成数。"""

    considered: int = 0
    requeued: int = 0
    skipped: int = 0
    failed: int = 0


def exit_code(stats: RequeueStats) -> int:
    """有失败且一份都没救回时为 1，否则 0（部分失败仍算跑过）。"""

    return 1 if stats.failed and not stats.requeued else 0


def requeue_documents(
    db: Session,
    *,
    kb_id: str | None = None,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    dry_run: bool = False,
) -> RequeueStats:
    """重新投递所有「卡死」的文档，返回统计（不打印）。"""

    documents = document_repository.list_stuck(
        db, kb_id=kb_id, lease_seconds=lease_seconds
    )
    considered = len(documents)
    requeued = 0
    skipped = 0
    failed = 0

    for doc in documents:
        if dry_run:
            continue
        try:
            # 扫描窗口与抢占窗口用同一个 lease：否则「扫出来却抢不到」会白白记 skipped。
            claimed = document_repository.try_claim_for_indexing(
                db, doc.id, lease_seconds=lease_seconds, allow_success=False
            )
            if not claimed:
                skipped += 1
                continue
            db.commit()
            enqueue_document(
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
                request_id=None,
                reparse=False,
            )
        except Exception:
            failed += 1
            db.rollback()
            logger.exception(
                "stuck document requeue failed",
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )
            continue
        requeued += 1

    return RequeueStats(
        considered=considered, requeued=requeued, skipped=skipped, failed=failed
    )


def _print_summary(
    *,
    kb_id: str | None,
    lease_seconds: int,
    dry_run: bool,
    stats: RequeueStats,
) -> None:
    mode = "dry-run" if dry_run else "live"
    scope = "all knowledge bases" if kb_id is None else f"kb_id={kb_id}"
    print(f"requeue_stuck_documents ({mode}): {scope} lease_seconds={lease_seconds}")
    print(f"  candidates: {stats.considered}")
    print(f"  requeued: {stats.requeued}")
    print(f"  skipped: {stats.skipped}")
    print(f"  failed: {stats.failed}")
    print(REQUEUED_NOT_COMPLETED_NOTE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-dispatch documents stuck in PROCESSING whose lease has expired "
            "(manual safety net for a crashed worker)."
        )
    )
    parser.add_argument(
        "--kb-id", default=None, help="only scan this knowledge base (default: all)"
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DOCUMENT_LEASE_SECONDS,
        help=f"lease in seconds (default: {DOCUMENT_LEASE_SECONDS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="count candidates only, dispatch nothing"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 统计。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        stats = requeue_documents(
            db,
            kb_id=args.kb_id,
            lease_seconds=args.lease_seconds,
            dry_run=args.dry_run,
        )
    finally:
        db.close()

    _print_summary(
        kb_id=args.kb_id,
        lease_seconds=args.lease_seconds,
        dry_run=args.dry_run,
        stats=stats,
    )
    return exit_code(stats)


if __name__ == "__main__":
    raise SystemExit(main())
