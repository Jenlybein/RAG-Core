"""保留策略脚本（第 12 步票 #178）：留最近 3 稿，窗口外的归档稿连切片带版本行一起清掉。

为什么需要它：ADR-0005 D4 选了「每稿各留切片」，于是稿子会无限长下去——每稿一套切片、一套
关键词文档。保留窗口把这个代价卡住：只留最近 :data:`RETAIN_VERSIONS` 稿，更老的**归档**稿
连切片带版本行清掉。超出窗口的稿子既不能回滚（#176 只认窗口内的稿），也不该再占存储。

**它是脚本，不是定时任务**（D10）：仓库没有 Celery beat，本步也不引入。怎么定时是运维的事
（``cron`` / Windows 任务计划调这个脚本），代码里只有「跑一次、退出、报统计」。

行为：

1. 扫出「窗口可能非空」的文档（``current_version > retain``，见
   :func:`app.repositories.document_repository.list_with_expired_versions`），``--kb-id``
   收窄范围、``--limit`` 收敛条数；
2. 逐篇算窗口（:func:`app.services.document_version_service.plan_retention`）：稿号
   ``<= current_version - retain`` 的**归档**稿在窗口外；
3. **默认 dry-run**：只打印「将删哪几篇文档的哪几稿」，一个字都不写、不提交；
4. ``--apply`` 才执行，**每篇文档一笔事务**：先删切片（向量 + 关键词两个存储），再删版本行
   （见 :func:`app.services.document_version_service.purge_expired_versions`）。中途失败
   整笔回滚，记 failed 并继续下一篇。

三条口径（票面 + D10）：

* **只删归档稿，当前稿永不删**——哪怕它是 v1 而指针在 v9（库里残留的低稿号当前行）；
* 窗口按**稿数**算（``current_version - retain``），不按时间：一天改十稿的文档不会保留
  几十个工作日的切片；
* 幂等：跑第二遍没有候选、不发语句。失败重跑也安全——窗口外那几稿的切片即使上一次已经
  在关键词索引里被删掉，再删一次仍是 no-op。

**回滚保证的是库内的一致性**：向量切片行与版本行在同一个数据库事务里，失败时一起复原。
关键词索引（ES）不在数据库事务里，最坏情况下会多删一次那几稿的关键词文档——它们属于窗口外
的归档稿，检索永远查不到（检索 pin 在生效版本 + 当前稿上），本次或下一次重建会补回来。

供运维使用：``uv run python scripts/purge_document_versions.py [--retain 3] [--kb-id <id>]
[--limit N] [--apply]``。退出码沿用共享规则：有 failed 且一篇都没清成时为 1，否则 0
（部分失败仍算跑过）。模块顶层不解析 argv、不连库、不建引擎，导入即安全。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.db.transaction import async_transaction
from app.repositories import document_repository
from app.services.chunk_store import ChunkStore
from app.services.document_version_service import (
    RETAIN_VERSIONS,
    plan_retention,
    purge_expired_versions,
)

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)

APPLIED_NOTE = (
    'NOTE: "purged" counts drafts whose chunks (vector + keyword) and version row are gone. '
    "The current draft is never deleted, and the window is counted in drafts, not in days."
)

DRY_RUN_NOTE = (
    'NOTE: dry-run only — nothing was written. Re-run with "--apply" to delete the chunks '
    "and version rows of the archived drafts listed above."
)


@dataclass(frozen=True)
class PurgeStats:
    """一次保留清理的统计。

    ``considered`` 是扫到的候选文档数（指针已经长出窗口的那些），``documents`` 是其中**真的
    有过期稿**的篇数——「扫到」与「有事可做」是两回事。``expired`` 是窗口外的归档稿总数，
    ``purged`` 是**已经删掉**的稿数（dry-run 恒为 0），``pending`` 是 dry-run 的清单
    （文档 id 与将删的稿号），CLI 会原样打印——运维先看这份清单再决定要不要 ``--apply``。
    """

    considered: int = 0
    documents: int = 0
    expired: int = 0
    purged: int = 0
    failed: int = 0
    pending: tuple[tuple[str, tuple[int, ...]], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已处理」口径（清掉几稿）。"""

        return self.purged


async def purge_expired(
    db: AsyncSession,
    *,
    retain: int = RETAIN_VERSIONS,
    kb_id: str | None = None,
    limit: int | None = None,
    apply: bool = False,
    stores: Sequence[ChunkStore] | None = None,
) -> PurgeStats:
    """逐篇清掉窗口外的归档稿，返回统计（不打印）；``apply=False`` 是 dry-run。

    事务边界在**这一层**而不是服务层：一篇文档一笔 ``async with async_transaction(db)``，
    中途失败整笔回滚（不留「切片已删、版本行还在」的半份），下一篇照常处理——运维跑一次要
    能把剩下的都做完，而不是第一篇卡住就整批不动。

    窗口在事务**内**再算一次（``purge_expired_versions`` 自己会重算）：从「预检」到「真正
    开事务」之间可能有人切了指针，那时这一篇就没有候选稿了，清理自然变成 no-op。
    """

    documents = await document_repository.list_with_expired_versions(
        db, retain=retain, kb_id=kb_id, limit=limit
    )

    considered = len(documents)
    documents_hit = 0
    expired = 0
    purged = 0
    failed = 0
    pending: list[tuple[str, tuple[int, ...]]] = []

    for doc in documents:
        # 预检先读一遍（不开事务）：绝大多数文档的窗口是空的，为它们开一笔事务再提交
        # 只是白白往库里写一次。
        plan = await plan_retention(db, document=doc, retain=retain)
        if plan.is_empty:
            continue
        documents_hit += 1
        expired += plan.expired_count
        if not apply:
            pending.append((plan.document_id, plan.expired_versions))
            continue
        try:
            async with async_transaction(db):
                purged_plan = await purge_expired_versions(
                    db, document=doc, retain=retain, stores=stores
                )
        except Exception:
            failed += 1
            logger.exception(
                "document version retention purge failed",
                document_id=plan.document_id,
                tenant_id=doc.tenant_id,
                expired_versions=list(plan.expired_versions),
            )
            continue
        purged += purged_plan.expired_count

    return PurgeStats(
        considered=considered,
        documents=documents_hit,
        expired=expired,
        purged=purged,
        failed=failed,
        pending=tuple(pending),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Keep only the last N drafts of every document: delete the chunks (vector + "
            "keyword) and the version rows of archived drafts outside the window. "
            "The current draft is never deleted."
        )
    )
    parser.add_argument(
        "--retain",
        type=int,
        default=RETAIN_VERSIONS,
        help=f"how many drafts to keep (default: {RETAIN_VERSIONS})",
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
        help="actually delete chunks and version rows (default: dry-run, nothing is written)",
    )
    return parser


async def _open_session() -> Any:
    """开一个运维会话（惰性 import：模块导入时绝不建引擎）。"""

    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal()


async def _close_keyword_provider() -> None:
    """退出前关掉已构造的关键词客户端（惰性 import，只关已存在的）。

    清理会经过关键词存储删切片，也就可能建出 ES 客户端；不关的话进程收尾会刷一串
    ``Unclosed client session``，把「这次清了几篇」这类真正要看的输出淹掉。
    """

    from app.providers.keyword_search import aclose_keyword_search_provider

    await aclose_keyword_search_provider()


def _print_pending(pending: Sequence[tuple[str, tuple[int, ...]]]) -> None:
    """把 dry-run 的清单逐条打出来：``<document_id>  <稿号, 稿号>``。"""

    if not pending:
        return
    print("  archived drafts outside the window:")
    for document_id, versions in pending:
        print(f"    {document_id}  {', '.join(str(version) for version in versions)}")


async def _run(args: argparse.Namespace) -> int:
    db = await _open_session()
    try:
        stats = await purge_expired(
            db,
            retain=args.retain,
            kb_id=args.kb_id,
            limit=args.limit,
            apply=args.apply,
        )
    finally:
        await db.close()
        await _close_keyword_provider()

    mode = "live" if args.apply else "dry-run"
    scope = "all knowledge bases" if args.kb_id is None else f"kb_id={args.kb_id}"
    print_summary(
        f"purge_document_versions ({mode}): {scope} retain={args.retain}",
        stats=stats,
        counters=(
            ("considered", "considered"),
            ("documents", "documents"),
            ("expired", "expired"),
            ("purged", "purged"),
            ("failed", "failed"),
        ),
        note=APPLIED_NOTE if args.apply else DRY_RUN_NOTE,
    )
    _print_pending(stats.pending)
    return exit_code_for(stats)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的会话 → 统计与退出码。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.retain < 0:
        # 负数会把窗口下界抬到指针之上（``current - (-1) = current + 1``）——那已经不是
        # 「留几稿」的意思了，宁可拒绝解析（退出码 2），也不替用户猜。
        parser.error("--retain must be >= 0")

    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
    # InterfaceError）。运维脚本多半在开发机上被敲，所以这里换成 Selector 循环——
    # 与离线套件 conftest 里那一行同一个理由（当年 ``rebuild_kb_version`` 也这么做，
    # 那个脚本已删除）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
