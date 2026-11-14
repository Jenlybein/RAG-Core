"""把一个知识库全量重建到新的索引版本（#143；流水线是 #142 的那一条，不另写一套）。

```text
start_build（一行 building） → 逐文档重建到目标版本 → 四项计数 → ready
                                                                  │
                                        （可选，--activate）单事务 → active，旧版 archived
```

**为什么要有这个脚本**：#142 把「整库重建」做成了构建流水线（``IndexBuildService`` +
``build_index_version_task``），但除测试之外**没有任何入口**能真的发起一次构建。而 #143
需要「把一个库重建到 v2」来给「新旧构建逐节对比」提供数据来源——没有入口，那件事只能手工
拼，拼出来的东西迟早与流水线分叉。

## 三种跑法，默认走 Worker

| 跑法 | 命令 | 适用 |
| --- | --- | --- |
| 投递到 Worker（默认） | ``--kb-id <id>`` | 线上：构建与请求互不阻塞，线上照旧查旧版本 |
| 就地跑（``--in-process``） | ``--in-process [--activate]`` | 小库 / 没有 Worker 的机器 / 想把「构建 + 激活」一次做完 |
| **回滚**（``--rollback``） | ``--rollback <version>`` | 新切法出问题时退回去（#155 的实现，本脚本给入口，#156） |

三种跑法**共用同一个服务**（:class:`app.services.index_build_service.IndexBuildService`）：
构建那两种的差别只有「谁执行 ``build()``」——Worker 还是本进程。默认落在投递上，是因为
就地跑会把 embedding 的全部耗时压在运维用的这个终端里，且没有心跳、没有超时、没有重试队列。

``--activate`` 只允许与 ``--in-process`` 同用：投递出去之后本进程就不知道 Worker 什么时候
跑完，此时「顺手激活」只能靠轮询，而轮询会把「激活」这件事变成一段时序猜测。**激活是一个
独立、可审计、可重试的动作**（ADR-0025 决策三），不该藏在一个隐式等待里。

本脚本仍**不提供**「把某个 ``ready`` 的 build 直接激活」这个反向操作（要那个，等 #147 的
编排入口）：那会让人用一次构建动作改名线上的生效版本，而 ``--rollback`` 只认 ``archived``
且带物理数据守卫——两条路的准入条件不同，不该合成一个参数。

## ``--rollback <version>``：按**版本名**回切

``--rollback v1`` 把该库切回 **名为 v1 的那一行版本**（``archived → active``），走的是
:meth:`IndexBuildService.rollback`——与激活同一段实现，外加一条物理数据守卫（目标版本的
物理索引与切片必须还在）。回滚**按版本名而不是 build_id**：运维手里有的是「上一次构建叫
v1」，不是那行 UUID；要查 UUID 再回切，等于把一次应急动作变成两段手工 SQL。

三条刻意的限制：

* **不许与构建参数混用**：``--rollback`` 与 ``--version`` / ``--in-process`` / ``--activate``
  同时出现时直接拒绝解析（退出码 2）。回滚是「切回既有的某一版」，构建是「造一个新版本」，
  猜用户想干什么比拒绝更危险；
* **不改版本名**：回滚不新建版本行，也不允许顺手重建；
* **失败什么都不动**：被守卫拒绝时库列（``active_index_version``）与关键词别名都保持原样，
  由 :class:`IndexBuildService` 保证，本脚本只负责把退出码说清楚。

**退出码**：0 = 已投递 / 已 ready（``--activate`` 时是已激活）/ 已回滚；1 = 拒绝或失败
（库不存在、版本名对不上、已有构建在跑、构建落 failed、回滚被守卫拒绝、并发切换相撞）。
脚本顶层不解析 argv、不连数据库、不建引擎：导入即安全。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.models.index_version import IndexBuildVersion
from app.models.knowledge_base import KnowledgeBase
from app.repositories import index_version_repository
from app.services.index_build_service import IndexBuildAlreadyRunning, IndexBuildService
from app.services.index_dispatch import enqueue_index_build

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = structlog.get_logger(__name__)


async def resolve_tenant_id(
    db: AsyncSession, *, kb_id: str, tenant_id: str | None
) -> str:
    """``--tenant-id`` 缺省时取该库的 ``tenant_id``；库不存在抛 ``LookupError``。

    运维脚本是唯一「先不知道租户」的调用点（与 ``reindex_knowledge_base`` 同一处处境）：
    用一次主键读发现归属，之后每一处调用都显式带这个租户。
    """

    if tenant_id is not None:
        return tenant_id
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise LookupError(f"knowledge base not found: {kb_id}")
    return str(kb.tenant_id)


async def start_rebuild(
    db: AsyncSession,
    *,
    kb_id: str,
    tenant_id: str,
    version: str | None = None,
) -> IndexBuildVersion:
    """起一行 ``building`` 并把构建任务投给 Worker；返回那一行。

    版本名缺省由已有版本算出（``v1`` / ``v2`` / …）：**版本是构建的产物，不是参数**，
    所以不给它就是正确用法；给它是为了「这次构建叫 v2」这种需要与别处对齐的场合。
    """

    service = IndexBuildService()
    row = await service.start_build(db, kb_id=kb_id, tenant_id=tenant_id, version=version)
    enqueue_index_build(
        build_id=row.id,
        tenant_id=tenant_id,
        # 号牌让 Worker 的日志与这一次运维动作对得上；不参与去重（去重靠 building 行的
        # 部分唯一索引）。
        request_id=f"ops-rebuild:{row.id}",
    )
    return row


async def run_rebuild(
    db: AsyncSession,
    *,
    kb_id: str,
    tenant_id: str,
    version: str | None = None,
    activate: bool = False,
) -> IndexBuildVersion:
    """就地跑完整条流水线（可选连激活），返回最后那一行。

    就地跑**不**改「一次构建 = 逐文档一笔事务」的分工：``build()`` 自己逐篇提交，
    ``activate()`` 自己开一笔单事务。这里只是省掉了 Celery 这一跳。
    """

    service = IndexBuildService()
    row = await service.start_build(db, kb_id=kb_id, tenant_id=tenant_id, version=version)
    await service.build(db, row.id, tenant_id=tenant_id)
    if activate:
        await service.activate(db, row.id, tenant_id=tenant_id)
    return row


async def rollback_to(
    db: AsyncSession,
    *,
    kb_id: str,
    tenant_id: str,
    version: str,
) -> IndexBuildVersion:
    """把该库切回**名为 ``version`` 的那一行**（``archived → active``）；返回那一行。

    版本名 → 版本行的那一步在这里，而不在服务里：服务认的是构建 id（它不该知道运维习惯用
    哪个名字），运维认的是版本名（``v1`` / ``v2``）。两者之间只有这一处翻译。

    名字对不上任何一行时抛 ``LookupError``（调用方据此退 1）——**不**顺手建一个新版本，
    也不把「名字打错了」降级成「什么都没做，退出码 0」。
    """

    rows = await index_version_repository.list_for_kb(db, kb_id, tenant_id=tenant_id)
    target = next((row for row in rows if row.version == version), None)
    if target is None:
        raise LookupError(
            f"index version not found for this knowledge base: {version} "
            f"(known versions: {', '.join(sorted(row.version for row in rows)) or 'none'})"
        )
    service = IndexBuildService()
    return await service.rollback(db, target.id, tenant_id=tenant_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild one knowledge base into a new index version through the build "
            "pipeline (start_build -> build -> ready [-> activate]), or roll it back "
            "to an existing version (--rollback <version>)."
        )
    )
    parser.add_argument("--kb-id", required=True, help="knowledge base id to rebuild")
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="tenant id; defaults to the knowledge base owner's tenant",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="target version name; defaults to the next one (v1, v2, ...)",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="run the build in this process instead of dispatching it to a worker",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="activate the new version after the in-process build is ready",
    )
    parser.add_argument(
        "--rollback",
        default=None,
        metavar="VERSION",
        help=(
            "switch the knowledge base back to an existing version (archived -> active); "
            "takes the version name (e.g. v1), not the build id"
        ),
    )
    return parser


async def _open_session() -> Any:
    """开一个运维会话（惰性 import：模块导入时绝不建引擎）。"""

    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal()


async def _run(args: argparse.Namespace) -> int:
    db = await _open_session()
    try:
        tenant_id = await resolve_tenant_id(db, kb_id=args.kb_id, tenant_id=args.tenant_id)
        if args.rollback:
            row = await rollback_to(
                db,
                kb_id=args.kb_id,
                tenant_id=tenant_id,
                version=args.rollback,
            )
        elif args.in_process:
            row = await run_rebuild(
                db,
                kb_id=args.kb_id,
                tenant_id=tenant_id,
                version=args.version,
                activate=args.activate,
            )
        else:
            row = await start_rebuild(
                db, kb_id=args.kb_id, tenant_id=tenant_id, version=args.version
            )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except IndexBuildAlreadyRunning:
        # 一个库同时只允许一条 building 行：这不是故障，是「上一轮还没收尾」。
        print(
            "an index build is already running for this knowledge base; "
            "wait for it to finish (or inspect it) before starting another",
            file=sys.stderr,
        )
        return 1
    except AppError as exc:
        # 动词跟着用户敲的那个子动作走：回滚被守卫拒绝与构建被拒绝，处置完全不同。
        # 取 ``msg`` 而不是 ``message``：``AppError`` 的字段叫 ``msg``（#156 顺手修正——
        # 这条路径此前没有被任何用例走过，写错字段名的后果是「拒绝变成一次 AttributeError」）。
        verb = "rollback" if args.rollback else "rebuild"
        print(f"{verb} refused: {exc.msg}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 运维脚本：把类型打出来，别吞掉
        verb = "rollback" if args.rollback else "rebuild"
        print(f"{verb} failed: {type(exc).__name__}", file=sys.stderr)
        raise
    finally:
        await db.close()

    mode = "rollback" if args.rollback else ("in-process" if args.in_process else "dispatched")
    print(
        f"rebuild_kb_version ({mode}): kb_id={args.kb_id} tenant_id={tenant_id} "
        f"build_id={row.id} version={row.version} status={row.status}"
    )
    if row.activated_at is not None:
        print(f"  activated_at: {row.activated_at.isoformat()}")
    if row.document_count is not None:
        print(f"  documents: {row.document_count}")
    if row.chunk_count is not None:
        print(f"  chunks: {row.chunk_count}")
    if not args.in_process and not args.rollback:
        print(
            "NOTE: the build runs in a worker; this row stays 'building' until that "
            "worker finishes. Watch it with: SELECT status, document_count, chunk_count, "
            "error_message FROM index_build_versions WHERE id = '<build_id>'."
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的会话 → 打印结果。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.activate and not args.in_process:
        # 见模块文档：投递出去之后本进程不知道 Worker 何时跑完，「顺手激活」只能靠轮询。
        parser.error("--activate requires --in-process")
    if args.rollback is not None and (args.version or args.in_process or args.activate):
        # 回滚是「切回既有的某一版」，构建是「造一个新版本」：混在一起时拒绝解析，
        # 不替用户猜他想先建再退、还是只想退。
        parser.error("--rollback cannot be combined with --version / --in-process / --activate")

    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
    # InterfaceError）。运维脚本多半在开发机上被敲，所以在这里换成 Selector 循环——
    # 与离线套件 conftest 里那一行同一个理由。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
