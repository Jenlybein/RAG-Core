"""按构建 id 或库 id **立刻**收敛一次构建（#214 的运维入口，默认只读）。

## 它回答什么问题

一次构建停在那里不动时，运维要回答的只有一句：**「它是还没跑完，还是已经被放弃了？」**
本脚本把这个答案摊成三个数字再给一个结论：

```text
truth     这一版、这一稿下已经有切片的文档数（读 chunks 表，不是任何计数器）
planned   起构建那一刻快照的可参与文档数（构建行的 documents_planned）
missing   差的那几篇**具体是哪几篇**（参与文档 − 已有切片的文档）
verdict   ready / failed（这是按真值算出来的结论，不是猜的）
```

判断依据与任务体、清扫器**完全同一个**（``app.services.index_build_service.decide_convergence``）：
真值够、且没有失败明细才允许 ``ready``；不够就是 ``failed``。所以「任务本来说它成功了、
脚本却说不够」这种两个真相并存的情况不会出现——只可能有一处判据，而它是纯函数。

## 默认只读，``--apply`` 才动手

默认**什么都不写**：这条命令动的是「一次构建的结论」，误跑的代价是把一个还能收敛的构建
提前判死（``failed`` 是终态，只能重新发起一次构建，也就是另一个版本号）。所以：

* 不加参数 = 报告（``--dry-run`` 是同一件事的显式写法，两种都接受）；
* ``--apply`` 才真的收敛（调 ``converge``，它自己幂等，重复跑不会收两次尾）。

## 三种跑法

| 跑法 | 命令 | 适用 |
| --- | --- | --- |
| 单个构建 | ``--build-id <id>`` | 从日志 / 告警里拿到了那个 id |
| 整个库的未收尾构建 | ``--kb-id <id>`` | 「这个库怎么一直转圈」 |
| 补租户 | 上面任一加 ``--tenant-id <id>`` | 有权限问题时显式给，缺省会从库里反查 |

**为什么 ``--tenant-id`` 是可选的**：运维手里通常只有构建 id 或库 id（日志里就是这两个），
而租户可以从那一行反查一次——把「先查租户再回来跑第二遍」变成一次命令，是这类应急动作的
全部意义。反查不到就明确报错退出，不猜。

## 退出码

复用 ``_ops_common`` 的规则：本脚本自己出错（读不到行、收敛时抛异常）且**一份都没收敛**
时为 1，否则 0。注意「收成 ``failed``」**不是**错误：那是一次成功的裁决——构建本来就该失败，
脚本把它落下来而已，所以它记在 ``failed_verdict`` 里、不影响退出码。

顶层不解析 argv、不连数据库、不建引擎：导入即安全，测试可以直接 import 函数。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.models.index_version import (
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_READY,
    IndexBuildVersion,
)
from app.models.knowledge_base import KnowledgeBase
from app.repositories import (
    chunk_repository,
    document_repository,
    index_version_repository,
)
from app.services.index_build_service import (
    CONVERGE_WAIT,
    BuildTruth,
    IndexBuildService,
    decide_convergence,
)
from app.utils.status import DocumentStatus

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconcileStats:
    """一次对账的统计。

    ``failed_verdict`` 与 ``failed`` 是两件事，别混：前者是**裁决的结果**（这次构建确实不该
    上线，脚本照实落成 ``failed``），后者是**脚本自己的错**（读不到行、收敛时抛异常）。
    共享退出码规则只看后者。
    """

    considered: int = 0
    reconciled: int = 0
    ready: int = 0
    failed_verdict: int = 0
    settled: int = 0
    failed: int = 0

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已投递」口径 = 本次真的落下结论的行数。"""

        return self.reconciled


@dataclass(frozen=True)
class BuildReport:
    """一个构建的对账读数（打印用；不含任何「要做什么」的隐含动作）。"""

    row: IndexBuildVersion
    truth: BuildTruth
    missing: tuple[str, ...]
    verdict: str

    @property
    def needs_attention(self) -> bool:
        """真值不够时才是「该看一眼」的那一行（``verdict`` 为 ``ready`` 的不需要人手）。"""

        return self.verdict != INDEX_BUILD_READY


async def resolve_tenant_id(
    db: AsyncSession, *, build_id: str | None, kb_id: str | None, tenant_id: str | None
) -> str:
    """``--tenant-id`` 缺省时从构建行 / 库行反查一次归属；查不到抛 ``LookupError``。"""

    if tenant_id is not None:
        return tenant_id
    if build_id is not None:
        row = await db.get(IndexBuildVersion, build_id)
        if row is None:
            raise LookupError(f"index build not found: {build_id}")
        return str(row.tenant_id)
    assert kb_id is not None, "调用方已经保证 --build-id / --kb-id 二选一"
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise LookupError(f"knowledge base not found: {kb_id}")
    return str(kb.tenant_id)


async def _targets(
    db: AsyncSession, *, build_id: str | None, kb_id: str | None, tenant_id: str
) -> list[IndexBuildVersion]:
    """本次要看的那些构建行：按 id 就一行，按库就把这个库还没收尾的都拿出来。"""

    if build_id is not None:
        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise LookupError(f"index build not found for this tenant: {build_id}")
        return [row]
    assert kb_id is not None, "调用方已经保证 --build-id / --kb-id 二选一"
    rows = await index_version_repository.list_for_kb(db, kb_id, tenant_id=tenant_id)
    # 只看还没收尾的：已经 ready / failed / active 的行已经不是「该收敛却没收敛」的问题，
    # 把它们列出来只会把真正要处置的那一行淹掉。
    return [row for row in rows if row.status == INDEX_BUILD_BUILDING]


async def inspect_build(
    db: AsyncSession,
    service: IndexBuildService,
    row: IndexBuildVersion,
    *,
    tenant_id: str,
) -> BuildReport:
    """读一个构建的真值 / 分母 / 缺口，并给出按真值算出来的结论（**不写库**）。"""

    truth = await service.collect_truth(db, row=row, tenant_id=tenant_id)
    missing: tuple[str, ...] = ()
    if truth.shortfall:
        # 「缺哪些篇」只在真值不够时才值得算：真值够的时候这份名单没有读者，
        # 而它要多读两遍库（参与文档 + 已有切片的文档）。
        participants = await document_repository.list_by_kb_async(
            db, str(row.kb_id), tenant_id=tenant_id
        )
        expected = {
            str(document.id)
            for document in participants
            if document.status == DocumentStatus.SUCCESS
        }
        indexed = await chunk_repository.list_document_ids_in_version(
            db,
            str(row.kb_id),
            tenant_id=tenant_id,
            index_version=str(row.version),
            doc_version=truth.doc_version,
        )
        missing = tuple(sorted(expected - indexed))
    verdict = decide_convergence(truth=truth.documents, planned=truth.planned, failed=0, final=True)
    return BuildReport(row=row, truth=truth, missing=missing, verdict=verdict)


def print_report(report: BuildReport, *, applied: str | None) -> None:
    """把一个构建的读数逐行打出来（运维照这几行决定下一步）。"""

    row = report.row
    truth = report.truth
    print(
        f"build {row.id} kb={row.kb_id} version={row.version} status={row.status} "
        f"deadline={_isoformat(row.converge_deadline)}"
    )
    print(
        f"  truth: {truth.documents} document(s) with chunks at {row.version} "
        f"(draft {truth.doc_version if truth.doc_version is not None else '-'})"
    )
    print(f"  planned: {truth.planned} participating document(s) at build start")
    print(f"  shortfall: {truth.shortfall}")
    if report.missing:
        # 只打前二十个：这是给人看的线索，不是数据导出（整份名单在库里查得到）。
        shown = ", ".join(report.missing[:20])
        more = "" if len(report.missing) <= 20 else f" (+{len(report.missing) - 20} more)"
        print(f"  missing: {shown}{more}")
    print(f"  verdict: {report.verdict}")
    if applied is not None:
        print(f"  applied: {applied}")


def _isoformat(value: Any) -> str:
    """时间列打印成 ISO；没有值（手工摆的行）就打个 ``-``，不炸。"""

    return value.isoformat() if value is not None else "-"


async def reconcile(
    db: AsyncSession,
    *,
    build_id: str | None = None,
    kb_id: str | None = None,
    tenant_id: str | None = None,
    apply: bool = False,
) -> ReconcileStats:
    """对账（可选收敛）本次选中的构建行；返回统计。不打印——打印在 ``main`` 里。"""

    resolved_tenant = await resolve_tenant_id(
        db, build_id=build_id, kb_id=kb_id, tenant_id=tenant_id
    )
    rows = await _targets(db, build_id=build_id, kb_id=kb_id, tenant_id=resolved_tenant)
    service = IndexBuildService()
    considered = reconciled = ready = failed_verdict = settled = failed = 0

    for row in rows:
        considered += 1
        if row.status != INDEX_BUILD_BUILDING:
            # 已经有结论的行不再参与：收敛不是「重新裁决」，已经落下来的结论由状态真值表守门。
            print(f"build {row.id} kb={row.kb_id} version={row.version} status={row.status}")
            print("  settled: this build already has a conclusion; nothing to converge")
            settled += 1
            continue
        try:
            report = await inspect_build(db, service, row, tenant_id=resolved_tenant)
            applied: str | None = None
            if apply:
                converged = await service.converge(
                    db, row.id, tenant_id=resolved_tenant, final=True
                )
                applied = converged.status
                reconciled += 1
                if converged.status == INDEX_BUILD_BUILDING:
                    # final=True 时不该发生（要么 ready、要么 failed）；真发生了就说明
                    # 判据与收尾动作之间有分歧，报出来而不是假装收敛了。
                    applied = CONVERGE_WAIT
                elif converged.status == INDEX_BUILD_READY:
                    ready += 1
                else:
                    failed_verdict += 1
            print_report(report, applied=applied)
        except AppError as exc:
            # 四项计数对不上这类拒绝：行还在 building，得让运维看见原因（不是静默跳过）。
            failed += 1
            print(f"build {row.id}: {type(exc).__name__}: {exc.msg}", file=sys.stderr)
            logger.warning(
                "reconcile_build could not converge",
                build_id=row.id,
                tenant_id=resolved_tenant,
                reason=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - 一个构建炸了不该带走其余的
            failed += 1
            print(f"build {row.id}: {type(exc).__name__}", file=sys.stderr)
            logger.exception(
                "reconcile_build failed on a build",
                build_id=row.id,
                tenant_id=resolved_tenant,
            )
            await db.rollback()

    return ReconcileStats(
        considered=considered,
        reconciled=reconciled,
        ready=ready,
        failed_verdict=failed_verdict,
        settled=settled,
        failed=failed,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report (and with --apply, converge) an index build by reading the truth: "
            "how many documents really have chunks in the target version versus how many "
            "were planned when the build started."
        )
    )
    parser.add_argument("--build-id", default=None, help="converge this single build")
    parser.add_argument(
        "--kb-id",
        default=None,
        help="converge every unfinished build of this knowledge base",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="tenant id; defaults to the owner of the build / knowledge base",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the verdict (ready / failed); without it nothing is written",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly read-only (this is the default)",
    )
    return parser


async def _open_session() -> Any:
    """开一个运维会话（惰性 import：模块导入时绝不建引擎）。"""

    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal()


async def _run(args: argparse.Namespace) -> int:
    db = await _open_session()
    try:
        stats = await reconcile(
            db,
            build_id=args.build_id,
            kb_id=args.kb_id,
            tenant_id=args.tenant_id,
            apply=args.apply,
        )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await db.close()

    mode = "apply" if args.apply else "dry-run"
    scope = (
        f"build_id={args.build_id}"
        if args.build_id is not None
        else f"kb_id={args.kb_id} (unfinished builds)"
    )
    print_summary(
        f"reconcile_build ({mode}): {scope}",
        stats=stats,
        counters=(
            ("candidates", "considered"),
            ("converged", "reconciled"),
            ("ready", "ready"),
            ("failed_verdict", "failed_verdict"),
            ("already_settled", "settled"),
            ("errors", "failed"),
        ),
        note=(
            "NOTE: 'truth' comes from chunks (COUNT DISTINCT document_id), not from any "
            "counter; the verdict is the same pure function the worker and the sweeper use."
        ),
    )
    if not args.apply:
        print("dry-run: nothing was written (add --apply to converge)")
    return exit_code_for(stats)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的会话 → 打印统计。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if (args.build_id is None) == (args.kb_id is None):
        # 两个都给等于「我到底想看哪一个」，猜比拒绝危险：一次对账动的是构建的结论。
        parser.error("pass exactly one of --build-id / --kb-id")
    if args.apply and args.dry_run:
        parser.error("--dry-run and --apply are mutually exclusive")

    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
    # InterfaceError）。运维脚本多半在开发机上被敲，所以在这里换成 Selector 循环——
    # 与 ``scripts/rebuild_kb_version.py`` 同一个理由。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
