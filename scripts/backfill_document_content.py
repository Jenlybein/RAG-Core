"""正文补齐脚本（第 12 步票 #170）：给「正文从来没落过库」的历史文档补上正文。

为什么需要它：在 #170 之前，文件上传的文档 ``documents.content`` 一直是空串——Worker
解析完只切块、不回写。正文回写的代码上线之后，**新**上传的文档有了正文，但**存量**
文档还是空的；而后续的版本行、对账脚本都以这份正文为真值，空正文会让它们全部失效。
所以上线顺序是「回写代码 → 本脚本 → 迁移」。

行为：
1. 用仓储的 :func:`app.repositories.document_repository.list_missing_content` 扫出
   ``content`` 为空串 / ``NULL`` 的文档，可选 ``--kb-id`` 缩小范围、``--limit`` 收敛条数；
2. 逐篇读**源文件**并解析（与 Worker 用同一个入口 ``prepare_document_content``，
   显式 ``reparse=True``：这里的意思就是「不看库内正文、重读磁盘」）；
3. **默认 dry-run**：只统计、只列出补不了的，一个字都不写；``--apply`` 才把正文写回并
   逐篇提交（一篇失败不影响其余，与两个既有运维脚本同形）；
4. 读不到源文件的（没有 ``source_file_path``、落盘文件已丢、解析不出文字）**跳过**，
   连文档 id 与原因一起列出来——这些文档要么找回原文件，要么只能重新上传，运维需要
   知道具体是哪几篇；写库失败才算 ``failed``。

**没有自动回退手段**：写进去的是解析后的全文，改写前库里本来就是空串，所以
``--apply`` 不会覆盖任何已有正文（候选查询已经把非空正文排除在外）。

供运维使用：``uv run python scripts/backfill_document_content.py [--kb-id <id>]
[--limit N] [--apply]``。退出码沿用共享规则：有 failed 且一篇都没补成时为 1，否则 0
（读不到源文件的跳过不算失败）。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.models.document import Document
from app.repositories import document_repository
from app.services.document_ingestion import prepare_document_content

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)

NO_SOURCE_FILE_REASON = "no source file"

APPLIED_NOTE = (
    'NOTE: "backfilled" means the parsed text is now in documents.content and committed. '
    "Run the documents/versions migration only AFTER this script reports failed: 0 — "
    "the migration stops on empty content for documents that have chunks."
)

DRY_RUN_NOTE = (
    'NOTE: dry-run only — nothing was written. Re-run with "--apply" to write the parsed '
    "text back into documents.content."
)


@dataclass(frozen=True)
class BackfillStats:
    """一次正文补齐的统计。

    ``ready`` 是「源文件读得到、正文算得出来」的篇数（dry-run 下就是「将会补上的篇数」），
    ``backfilled`` 是**已经写回并提交**的篇数（dry-run 恒为 0）。
    ``skipped_documents`` 是跳过的那些文档与原因，按扫描顺序排列——「跳过并列出来」里的
    那份清单，CLI 会原样打印。
    """

    considered: int = 0
    ready: int = 0
    backfilled: int = 0
    skipped: int = 0
    failed: int = 0
    skipped_documents: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已处理」口径（补成几篇）。"""

        return self.backfilled


def _read_source_text(document: Document) -> str:
    """读源文件并解析出正文；语义与 Worker 第一次索引时完全一致（唯一解析入口）。

    ``reparse=True`` 是这一步的**意思本身**：候选是正文为空的文档，补齐就等于「重解析
    磁盘上的文件」。走同一个入口而不是自己按扩展名分流，解析口径才不会两处漂移。
    """

    return prepare_document_content(document, reparse=True).content


def backfill_documents(
    db: Session,
    *,
    kb_id: str | None = None,
    limit: int | None = None,
    apply: bool = False,
) -> BackfillStats:
    """给正文为空的文档补正文，返回统计（不打印）；``apply=False`` 是 dry-run。"""

    documents = document_repository.list_missing_content(db, kb_id=kb_id, limit=limit)

    considered = len(documents)
    ready = 0
    backfilled = 0
    skipped = 0
    failed = 0
    skipped_documents: list[tuple[str, str]] = []

    for doc in documents:
        if not doc.source_file_path:
            skipped += 1
            skipped_documents.append((doc.id, NO_SOURCE_FILE_REASON))
            continue
        try:
            text = _read_source_text(doc)
        except AppError as exc:
            # 「读不到源文件」的两种形态（文件丢了 / 解析不出文字）都是**跳过**：
            # 它们不是脚本坏了，而是这篇文档没有可补的来源。
            skipped += 1
            skipped_documents.append((doc.id, exc.msg))
            continue
        ready += 1
        if not apply:
            continue
        try:
            doc.content = text
            db.commit()
        except Exception:
            failed += 1
            db.rollback()
            logger.exception(
                "document content backfill failed",
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )
            continue
        backfilled += 1

    return BackfillStats(
        considered=considered,
        ready=ready,
        backfilled=backfilled,
        skipped=skipped,
        failed=failed,
        skipped_documents=tuple(skipped_documents),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill documents.content for documents whose body is still empty "
            "(re-parse the source file once, write it back)."
        )
    )
    parser.add_argument(
        "--kb-id", default=None, help="only scan this knowledge base (default: all)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process at most N documents (debugging)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the parsed text back (default: dry-run, nothing is written)",
    )
    return parser


def _print_skipped(skipped_documents: Sequence[tuple[str, str]]) -> None:
    """把跳过的文档逐条打出来：``<document_id>  <原因>``。"""

    if not skipped_documents:
        return
    print("  skipped documents:")
    for document_id, reason in skipped_documents:
        print(f"    {document_id}  {reason}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 统计。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        stats = backfill_documents(
            db, kb_id=args.kb_id, limit=args.limit, apply=args.apply
        )
    finally:
        db.close()

    mode = "live" if args.apply else "dry-run"
    scope = "all knowledge bases" if args.kb_id is None else f"kb_id={args.kb_id}"
    print_summary(
        f"backfill_document_content ({mode}): {scope}",
        stats=stats,
        counters=(
            ("considered", "considered"),
            ("ready", "ready"),
            ("backfilled", "backfilled"),
            ("skipped", "skipped"),
            ("failed", "failed"),
        ),
        note=APPLIED_NOTE if args.apply else DRY_RUN_NOTE,
    )
    _print_skipped(stats.skipped_documents)
    return exit_code_for(stats)


if __name__ == "__main__":
    raise SystemExit(main())
