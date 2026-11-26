"""正文 token 回填脚本（第 17 步 #220 / #221）：给存量文档补上「正文 token 数」。

## 为什么需要它

``documents.token_count`` 是单库容量判据的**唯一**输入（``Σ 其他文档 + 本篇 ≤ 上限``）。
它是本步的新列，而迁移只能给存量行填一个**下界**——按字符数估的那个数（中文一个字通常
一到两个 token，取「按字算」是最保守的下界）。下界偏小只会「多放一点」，不会「误拒」，
所以判据的方向是安全的；但运维需要真值来做两件事：

1. **报出已经超限的租户 / 知识库名单**——spec 要求「上线前跑一遍，把已经超过新上限的
   租户名单报出来」。这些库在上线后不能再往里加东西，运营得先知道是哪些；
2. 把 ``token_count`` 换成词表算的真值，让容量进度条显示的是真数字。

## 真值的口径（与 Worker 完全一致）

读**正文**（不是检索文本），用 ``app.utils.tokenizer.token_count`` 算——与
``IndexingService`` 里那一次计算是同一个函数、同一份词表。两个口径必须一致，否则
「进度条说 2.9 万」与「硬拦说 3.1 万」会同时出现在一个按钮上。

正文的来源按**版本行优先**、文档行兜底：

* 版本行正文（``document_versions.content``）是现行真值（第 12 步起），Worker 索引时读
  的就是它；
* ``documents.content`` 是「当前稿正文」的快照，只在版本行缺失时兜底（老数据）。

顺序反了的话，历史文档会按一份过期的正文算容量——而那正是第 12 步那条迁移要消灭的分叉。

## 默认 dry-run

与两个既有运维脚本同形：不带 ``--apply`` 只统计与列出**超限名单**，一个字都不写。
``--apply`` 才逐篇提交。一条失败不影响其余（失败只计入 ``failed``，不中断全批）。

## 为什么要先报超限名单

``--apply`` 把 ``token_count`` 换成真值之后，那些库**立刻**会进入「超限」状态（容量判据
在每次上传与重建时判）。这不是脚本造成的，是它们本来就超了——脚本只是把它显出来。所以
**先看一眼 dry-run 的名单，再决定要不要 --apply**，是这条脚本设计上鼓励的用法。

供运维使用：``uv run python scripts/backfill_document_tokens.py [--kb-id <id>] [--tenant-id <id>]
[--limit N] [--apply]``。退出码：有 failed 且一篇都没补成时 1，否则 0。
模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import PLAN_PRESETS, PLAN_FREE, Plan
from app.models.tenant import Tenant
from app.utils import tokenizer

logger = structlog.get_logger(__name__)

#: 一次取多少篇。回填要读全文并跑分词，一篇几万 token 的文档很占内存——分批不只是为了
#: 内存，也为了「跑到一半被打断时，已经写的那些是已提交的、可观测的」。
BATCH_SIZE = 200


@dataclass
class Report:
    """这一趟的账。字段只放运维要判断的东西，不放进度条。"""

    scanned: int = 0
    filled: int = 0
    skipped: int = 0
    failed: int = 0
    #: ``(tenant_id, kb_id, 已用 token, 上限 token)``——dry-run 与 --apply 都会收集，
    #: 因为「谁超了」是这一趟最重要的产出。
    over_limit: list[tuple[str, str, int, int]] = field(default_factory=list)
    #: 读不到正文的文档（既没有版本行正文，文档行也是空/NULL）。它们只能重建或重传。
    no_content: list[str] = field(default_factory=list)


def _content_of(session: Session, document: Document) -> str | None:
    """取这一篇的**正文**：版本行优先（现行真值），文档行兜底。"""

    version_row = session.execute(
        select(DocumentVersion.content)
        .where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == document.tenant_id,
            DocumentVersion.version == document.current_version,
        )
        .limit(1)
    ).scalar_one_or_none()
    if version_row:
        return str(version_row)
    if document.content:
        return str(document.content)
    return None


def _iter_documents(
    session: Session, *, kb_id: str | None, tenant_id: str | None, limit: int | None
) -> Sequence[Document]:
    statement = select(Document).where(Document.deleted_at.is_(None))
    if kb_id is not None:
        statement = statement.where(Document.kb_id == kb_id)
    if tenant_id is not None:
        statement = statement.where(Document.tenant_id == tenant_id)
    statement = statement.order_by(Document.created_at)
    if limit is not None:
        statement = statement.limit(max(limit, 1))
    return list(session.execute(statement).scalars().all())


def _kb_usage(session: Session, *, tenant_id: str, kb_id: str) -> int:
    """库里**已经落库**的 token 合计（读 ``token_count``，不是现算）。"""

    total = session.execute(
        select(func.coalesce(func.sum(Document.token_count), 0)).where(
            Document.tenant_id == tenant_id,
            Document.kb_id == kb_id,
            Document.deleted_at.is_(None),
        )
    ).scalar_one()
    return int(total or 0)


def run(
    session: Session,
    *,
    apply: bool = False,
    kb_id: str | None = None,
    tenant_id: str | None = None,
    limit: int | None = None,
) -> Report:
    """跑一趟。``apply`` 为假时**一个字都不写**。"""

    report = Report()
    plans: dict[str, int] = {}

    def _limit_of(tenant: str) -> int:
        if tenant not in plans:
            row = session.execute(
                select(Plan.max_tokens_per_kb)
                .join(Tenant, Tenant.plan_id == Plan.id)
                .where(Tenant.id == tenant)
                .limit(1)
            ).scalar_one_or_none()
            plans[tenant] = (
                int(row) if row is not None else int(PLAN_PRESETS[PLAN_FREE].max_tokens_per_kb)
            )
        return plans[tenant]

    documents = _iter_documents(session, kb_id=kb_id, tenant_id=tenant_id, limit=limit)
    for document in documents:
        report.scanned += 1
        try:
            content = _content_of(session, document)
        except Exception:  # noqa: BLE001 - 单篇读失败不该中断整批
            report.failed += 1
            logger.warning(
                "backfill: could not read the document text",
                document_id=document.id,
                tenant_id=document.tenant_id,
                kb_id=document.kb_id,
            )
            continue
        if not content:
            report.skipped += 1
            report.no_content.append(document.id)
            continue

        tokens = tokenizer.token_count(content)
        if tokens == document.token_count and not apply:
            # dry-run 下「已经是真值」的不算写入——运维看的是「这一趟会改多少」。
            report.skipped += 1
            continue

        if apply:
            written = session.execute(
                update(Document)
                .where(
                    Document.id == document.id,
                    Document.tenant_id == document.tenant_id,
                )
                .values(token_count=tokens)
            ).rowcount
            if written != 1:
                report.failed += 1
                logger.warning(
                    "backfill: token count write touched no row",
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    written=written,
                )
                continue
            session.commit()
        report.filled += 1

        # 超限判定用「写入之后」的合计：本篇算在它自己的库里。
        used = _kb_usage(session, tenant_id=document.tenant_id, kb_id=document.kb_id)
        limit_tokens = _limit_of(document.tenant_id)
        if used > limit_tokens and (document.tenant_id, document.kb_id) not in [
            (item[0], item[1]) for item in report.over_limit
        ]:
            report.over_limit.append((document.tenant_id, document.kb_id, used, limit_tokens))

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 documents.token_count（默认 dry-run）")
    parser.add_argument("--kb-id", default=None, help="只处理这一个知识库")
    parser.add_argument("--tenant-id", default=None, help="只处理这一个租户")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少篇")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只统计）")
    args = parser.parse_args(argv)

    tokenizer.verify_ready()

    session = SessionLocal()
    try:
        report = run(
            session,
            apply=bool(args.apply),
            kb_id=args.kb_id,
            tenant_id=args.tenant_id,
            limit=args.limit,
        )
    finally:
        session.close()

    mode = "apply" if args.apply else "dry-run"
    logger.info(
        "backfill finished",
        mode=mode,
        scanned=report.scanned,
        filled=report.filled,
        skipped=report.skipped,
        failed=report.failed,
        over_limit=len(report.over_limit),
        no_content=len(report.no_content),
    )
    if report.no_content:
        print(f"没有正文、只能重建或重传的文档（{len(report.no_content)} 篇）：")
        for document_id in report.no_content:
            print(f"  - {document_id}")
    if report.over_limit:
        print(f"回填后超过单库容量上限的知识库（{len(report.over_limit)} 个）：")
        for tenant_id, kb, used, limit_tokens in report.over_limit:
            print(f"  - 租户 {tenant_id} · 库 {kb}：已用 {used} / 上限 {limit_tokens} token")
        if not args.apply:
            print("（dry-run 未写库，上面是「如果现在回填会发生什么」；确认后再加 --apply）")
    if report.failed and report.filled == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
