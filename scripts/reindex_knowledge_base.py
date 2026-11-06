"""全库重建脚本：把一个知识库下所有 ``SUCCESS`` 文档重新投递索引任务。

用途：换了切块算法 / 解析器之后，不需要重新上传就能让新逻辑生效——文档正文
（文件模式落盘的原文，或 JSON 模式落库的 ``content``）还在，Worker 重新解析 /
切块 / 向量化即可，``chunks.metadata`` 会反映新逻辑。

行为（对应设计契约 §3）：
1. 查该 kb 下 ``status == SUCCESS`` 的文档（强制带租户：缺省取该 kb 的
   ``tenant_id``，也可用 ``--tenant-id`` 显式指定）；
2. 逐篇 ``try_claim_for_indexing(allow_success=True)``：抢不到（另一个 Worker /
   另一个脚本正在跑）记 skipped，抢到立刻 ``commit`` 释放行锁；
3. ``enqueue_document(reparse=False)`` 投递；投递抛错记 failed 并继续下一篇。

**打印的 ``submitted`` 是「已投递」数量，不是「已完成」数量。** 异步链路没有
「跑完」的同步感知，要确认终态请用 ``GET /documents?kb_id=<id>`` 或直接查库。

脚本直连数据库 + Celery，不需要 API Key，供运维使用；``uv run python
scripts/reindex_knowledge_base.py --kb-id <id>``。退出码：有 failed 且一份都没
投递成功时为 1，否则 0（部分失败仍算跑过）。模块顶层不解析 argv、不连接数据库，
导入即安全，测试可以直接 import 函数。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.repositories import document_repository, knowledge_base_repository
from app.services.index_dispatch import enqueue_document
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

SUBMITTED_NOT_COMPLETED_NOTE = (
    'NOTE: "submitted" counts documents handed to the Broker, not documents completed. '
    "The async pipeline has no synchronous completion signal; check the final status with "
    "GET /documents?kb_id=<id> (or query the database directly)."
)


@dataclass(frozen=True)
class ReindexStats:
    """一次全库重建的投递统计；``submitted`` 是已投递数，不是已完成数。"""

    considered: int = 0
    submitted: int = 0
    skipped: int = 0
    failed: int = 0


def exit_code(stats: ReindexStats) -> int:
    """有失败且一份都没投递成功时为 1，否则 0（部分失败仍算跑过）。"""

    return 1 if stats.failed and not stats.submitted else 0


def resolve_tenant_id(db: Session, *, kb_id: str, tenant_id: str | None) -> str:
    """``--tenant-id`` 缺省时取该 kb 的 tenant_id；kb 不存在则抛 LookupError。

    显式传入优先，便于运维在确认归属后手工指定；脚本是本地运维工具，不存在
    「跨租户泄露」的数据面入口问题，但查询仍逐条带租户过滤。
    """

    if tenant_id is not None:
        return tenant_id
    kb = knowledge_base_repository.get_by_id(db, kb_id)
    if kb is None:
        raise LookupError(f"knowledge base not found: {kb_id}")
    return str(kb.tenant_id)


def reindex_documents(
    db: Session,
    *,
    kb_id: str,
    tenant_id: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> ReindexStats:
    """把该库下的 SUCCESS 文档逐篇重新投递，返回统计（不打印）。"""

    documents = document_repository.list_by_kb_and_status(
        db, kb_id, tenant_id=tenant_id, status=DocumentStatus.SUCCESS
    )
    if limit is not None:
        documents = documents[:limit]

    considered = len(documents)
    submitted = 0
    skipped = 0
    failed = 0

    for doc in documents:
        if dry_run:
            continue
        try:
            claimed = document_repository.try_claim_for_indexing(
                db, doc.id, allow_success=True
            )
            if not claimed:
                # 另一个 Worker / 脚本已经抢走这份文档；CAS 是真正的去重手段。
                skipped += 1
                continue
            db.commit()
            enqueue_document(
                document_id=doc.id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                request_id=None,
                reparse=False,
            )
        except Exception:
            failed += 1
            db.rollback()
            logger.exception(
                "reindex enqueue failed",
                document_id=doc.id,
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            continue
        submitted += 1

    return ReindexStats(
        considered=considered, submitted=submitted, skipped=skipped, failed=failed
    )


def _print_summary(
    *, kb_id: str, tenant_id: str, dry_run: bool, stats: ReindexStats
) -> None:
    mode = "dry-run" if dry_run else "live"
    print(f"reindex_knowledge_base ({mode}): kb_id={kb_id} tenant_id={tenant_id}")
    print(f"  candidates: {stats.considered}")
    print(f"  submitted: {stats.submitted}")
    print(f"  skipped: {stats.skipped}")
    print(f"  failed: {stats.failed}")
    print(SUBMITTED_NOT_COMPLETED_NOTE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reindex every SUCCESS document in one knowledge base "
            "(after changing the splitter / parser)."
        )
    )
    parser.add_argument("--kb-id", required=True, help="knowledge base id to rebuild")
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="tenant id; defaults to the knowledge base owner's tenant",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="count candidates only, dispatch nothing"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process at most N documents (debugging)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 统计。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        tenant_id = resolve_tenant_id(db, kb_id=args.kb_id, tenant_id=args.tenant_id)
        stats = reindex_documents(
            db,
            kb_id=args.kb_id,
            tenant_id=tenant_id,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    _print_summary(
        kb_id=args.kb_id, tenant_id=tenant_id, dry_run=args.dry_run, stats=stats
    )
    return exit_code(stats)


if __name__ == "__main__":
    raise SystemExit(main())
