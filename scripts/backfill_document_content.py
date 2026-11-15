"""正文补齐脚本（第 12 步票 #170）：给「正文从来没落过库」的历史文档补上正文。

为什么需要它：在 #170 之前，文件上传的文档 ``documents.content`` 一直是空串——Worker
解析完只切块、不回写。正文回写的代码上线之后，**新**上传的文档有了正文，但**存量**
文档还是空的；而后续的版本行、对账脚本都以这份正文为真值，空正文会让它们全部失效。
所以上线顺序是「回写代码 → 本脚本 → 迁移」。

## 只用于升级前：跑错时机会被拒绝

这条脚本的适用期是**一个窗口**——「正文回写代码已上线、文档版本迁移还没跑」。脚本启动
时会自己判这件事：库里**已经有** ``document_versions`` 表（= 第 12 步那条迁移跑过了）
就拒绝运行、打印一句可操作的说明并以非 0 退出。

**为什么升级后就不许跑了**：迁移会把 ``documents.content`` 抄进每篇文档的版本行，此后
``documents.content`` 只是「当前稿正文」的快照，不变量是**文档正文 == 当前稿正文**。
而本脚本只改文档正文、不碰版本行：跑在迁移之后，两份真值当场分叉，且**没有任何报错**
——症状将来才出现，还是最难查的那种（重建 / 重建索引读到的是版本行，文档行上却是另一份
正文，同一个问题两个答案）。

**升级之后要补正文，正确姿势是重建 / 重解析**：
``POST /console/documents/{document_id}/reindex?reparse=true``（成功文档要 ``force=true``）。
那条链路同时回写文档正文与版本行，不变量在它手里成立；这条脚本从来没有那种能力，守卫
只是把「它的适用期结束了」这件事说出来，而不是给一条本可以走通的路加障碍。

行为：
1. 用仓储的 :func:`app.repositories.document_repository.list_missing_content` 扫出
   ``content`` 为空串 / ``NULL`` 的文档，可选 ``--kb-id`` 缩小范围、``--limit`` 收敛条数；
   候选是**显式列集**（:class:`app.repositories.document_repository.MissingContentDocument`），
   不含任何迁移后才存在的列——它必须在升级前跑得起来；
2. 逐篇读**源文件**并解析，走唯一解析入口
   :func:`app.services.document_ingestion.parse_source_file`（只吃路径 + 文件名，不吃
   文档实体），与 Worker 侧同一份实现；
3. **默认 dry-run**：只统计、只列出补不了的，一个字都不写；``--apply`` 才把正文写回
   （仓储的 :func:`app.repositories.document_repository.set_content`，一条 ``UPDATE``）并
   逐篇提交（一篇失败不影响其余，与两个既有运维脚本同形）；
4. 读不到源文件的（没有 ``source_file_path``、落盘文件已丢、解析不出文字）**跳过**，
   连文档 id 与原因一起列出来——这些文档要么找回原文件，要么只能重新上传，运维需要
   知道具体是哪几篇；写库失败才算 ``failed``。

**没有自动回退手段**：写进去的是解析后的全文，改写前库里本来就是空串，所以
``--apply`` 不会覆盖任何已有正文（候选查询已经把非空正文排除在外）。

供运维使用：``uv run python scripts/backfill_document_content.py [--kb-id <id>]
[--limit N] [--apply]``。退出码沿用共享规则：有 failed 且一篇都没补成时为 1，否则 0
（读不到源文件的跳过不算失败）；**跑错时机（已升级）退出 2**，与「跑过但失败」分开，
免得 CI / 人把它当成一次普通的补算失败。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.repositories import document_repository
from app.repositories.document_repository import MissingContentDocument
from app.services.document_ingestion import parse_source_file

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import exit_code_for, print_summary  # noqa: E402

logger = structlog.get_logger(__name__)

NO_SOURCE_FILE_REASON = "no source file"

#: 迁移跑过的判据：``document_versions`` 表只在第 12 步的 ``7c2e5a9b4d31`` 之后存在。
#: 它与模型的 ``__tablename__`` 同值（``app.models.document_version``），这里只读名字、
#: 不 import 模型：判「表在不在」不需要那张表的映射。
VERSIONS_TABLE = "document_versions"

#: 跑错时机时的拒绝说明，原样给运维看：判据、为什么、升级后该用什么、有没有写过东西。
ALREADY_MIGRATED_MESSAGE = (
    "REFUSED: this database already has the document_versions table, so the "
    "documents/versions migration has run. This script is only for the window BEFORE that "
    "migration: the migration copies every documents.content into its version row, so from "
    'then on the invariant is "document body == current draft body", and a backfill that '
    "only updates documents would fork it silently. To fill a missing body after the "
    "migration, rebuild / re-parse instead: "
    "`POST /console/documents/{document_id}/reindex?reparse=true` (add `force=true` for a "
    "SUCCESS document) — that path writes the document body and the version row together. "
    "Nothing was written."
)

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


def database_is_migrated(db: Session) -> bool:
    """库里是否已经有 ``document_versions`` 表（= 文档版本迁移已经跑过）。

    用 SQLAlchemy 的 inspector 而不是一句 ``SELECT``：判「表在不在」是结构问题，各家的写法
    不同（``information_schema`` / ``sqlite_master`` / ``to_regclass``），硬编码一种就把脚本
    绑死在一个方言上；inspector 是现成的、只有一份实现。``db.get_bind()`` 拿到的是这条会话
    背后的连接（或引擎），在会话里判结构不额外开连接。
    """

    return inspect(db.get_bind()).has_table(VERSIONS_TABLE)


def _read_source_text(document: MissingContentDocument) -> str:
    """读源文件并解析出正文——唯一解析入口，与 Worker 第一次索引时是同一份实现。

    「重解析」在这里就是意思本身：候选是正文为空的文档，补齐等于「重读磁盘上的文件」。
    走 :func:`app.services.document_ingestion.parse_source_file`（只吃路径 + 文件名）而不是
    自己按扩展名分流，也不传 ``Document`` 实体：解析口径只有一份实现，才不会两处漂移；而
    升级前的这条路径连实体都不该碰（见 :func:`document_repository.list_missing_content`
    的纪律）。
    """

    return parse_source_file(
        document.source_file_path, filename=document.source_filename
    ).content


def backfill_documents(
    db: Session,
    *,
    kb_id: str | None = None,
    limit: int | None = None,
    apply: bool = False,
) -> BackfillStats:
    """给正文为空的文档补正文，返回统计（不打印）；``apply=False`` 是 dry-run。

    这里**不做**「跑错时机」的守卫：那是 CLI 入口（:func:`main`）的判断，属于「这条命令
    现在该不该跑」。本函数是「按给定会话补一批」，测试与将来的调用方要的正是这个动作本身。
    """

    documents = document_repository.list_missing_content(db, kb_id=kb_id, limit=limit)

    considered = len(documents)
    ready = 0
    backfilled = 0
    skipped = 0
    failed = 0
    skipped_documents: list[tuple[str, str]] = []

    for document in documents:
        if not document.source_file_path:
            skipped += 1
            skipped_documents.append((document.id, NO_SOURCE_FILE_REASON))
            continue
        try:
            text = _read_source_text(document)
        except AppError as exc:
            # 「读不到源文件」的两种形态（文件丢了 / 解析不出文字）都是**跳过**：
            # 它们不是脚本坏了，而是这篇文档没有可补的来源。
            skipped += 1
            skipped_documents.append((document.id, exc.msg))
            continue
        ready += 1
        if not apply:
            continue
        try:
            document_repository.set_content(db, document.id, text)
            db.commit()
        except Exception:
            failed += 1
            db.rollback()
            logger.exception(
                "document content backfill failed",
                document_id=document.id,
                tenant_id=document.tenant_id,
                kb_id=document.kb_id,
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
            "(re-parse the source file once, write it back). Only valid BEFORE the "
            "documents/versions migration: it refuses to run once document_versions exists."
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
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 守卫 → 统计。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        # 跑错时机就停在这里：一行都不写（理由见模块文档「只用于升级前」）。
        if database_is_migrated(db):
            print(ALREADY_MIGRATED_MESSAGE, file=sys.stderr)
            return 2
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
