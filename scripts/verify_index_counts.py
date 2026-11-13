"""核对一个索引版本：四项计数 + 节的身份证自洽（#143）。

一条命令回答「这个版本能不能用」：

```text
uv run python scripts/verify_index_counts.py --kb-id <id> [--index-version v2]
```

## 判据（两条，任一不过就非零退出）

1. **四项计数**（知识库 / 切片 / 向量 / 关键词文档）**相等且非 0**。
   与 ``ready`` 的闸门差一条：全 0 的版本对闸门来说「彼此相等」，对核对脚本来说 empty。
   没有关键词引擎时那一维打 ``n/a``、不参与判定（能力缺失不是数据不一致）。
2. **节的身份证自洽**：每行的 ``section_id`` 与 ``parent_section_id`` 都必须能由
   ``(文档 id, 标题路径)`` 重算出来；同一个 ``section_id`` 出现在两个文档下也算错
   （说明混进了不带文档 id 的旧 id）。**父节只有标题、因此不产切片**这一档是正常的，
   只计数、不判错——口径见 :mod:`app.services.index_verification`。

## 为什么复用构建服务的 ``collect_counts``

四项计数的读数在 #142 里已经写死成一份（``chunk_repository.count_by_index_version``、
向量存储、关键词引擎三处各读一次）。核对脚本再拼一遍「同样四行」，两边迟早对同一份数据
给出不同答案——而运维最不该遇到的就是「闸门说相等、核对说不等」。

**退出码**：0 = 两条都过；1 = 有一条不过（或库 / 版本不存在）。顶层不解析 argv、不连库。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.models.knowledge_base import KnowledgeBase
from app.repositories import chunk_repository
from app.services.index_build_service import IndexBuildService
from app.services.index_verification import (
    IdentityRow,
    describe_counts,
    judge_counts,
    verify_identity,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = structlog.get_logger(__name__)


async def verify_knowledge_base(
    db: AsyncSession,
    *,
    kb: KnowledgeBase,
    tenant_id: str,
    index_version: str | None,
) -> int:
    """核对这个库的某个版本；打印结论并返回退出码（0 过 / 1 不过）。

    ``index_version`` 缺省取库上的**生效版本**：核对的对象默认是「线上正在用的那一版」，
    而不是某个写死的常量（多库各自 pin 在不同版本上，常量会核对到别人的版本）。
    """

    version = index_version or kb.active_index_version
    if not version:
        print(
            f"verify_index_counts: kb_id={kb.id} 没有生效版本，且没有指定 --index-version；"
            "无从核对（这个库还没完成过一次激活）",
            file=sys.stderr,
        )
        return 1

    service = IndexBuildService()
    counts = await service.collect_counts(db, kb=kb, version=version)
    verdict = judge_counts(counts)

    rows = await chunk_repository.list_section_identity_rows(
        db, tenant_id=tenant_id, kb_id=str(kb.id), index_version=version
    )
    identity = verify_identity(IdentityRow(*row) for row in rows)

    print(
        f"verify_index_counts: kb_id={kb.id} tenant_id={tenant_id} version={version}"
    )
    for label, value in describe_counts(counts):
        print(f"  {label}: {value}")
    if counts.keyword_documents is None:
        print("  （关键词引擎不可用，keyword_documents 这一维不参与判定）")
    if counts.matched:
        print("  ready 闸门口径：相等")
    else:
        print("  ready 闸门口径：不相等（这个版本本来就不该被标 ready）")

    print(
        "  sections: "
        f"{identity.sections}（有父 id 的 {identity.parented} 节：父节有正文 "
        f"{identity.parent_with_body}，父节只有标题 {identity.parent_without_body}）"
    )

    problems = list(verdict.problems) + identity.render_problems()
    if not problems:
        print("RESULT: OK（四项计数相等且非 0；节的身份证自洽）")
        return 0

    print("RESULT: FAILED")
    for problem in problems:
        print(f"  - {problem}")
    return 1


async def _run(args: argparse.Namespace) -> int:
    from app.db.async_session import AsyncSessionLocal

    db = AsyncSessionLocal()
    try:
        kb = await db.get(KnowledgeBase, args.kb_id)
        if kb is None:
            print(f"knowledge base not found: {args.kb_id}", file=sys.stderr)
            return 1
        tenant_id = args.tenant_id or str(kb.tenant_id)
        if tenant_id != str(kb.tenant_id):
            # 跨租户读版本行是不允许的（ADR-0006）：脚本照同一个口径拒绝，而不是
            # 「查到了就当成这个租户的」。
            print(
                f"knowledge base {args.kb_id} does not belong to tenant {tenant_id}",
                file=sys.stderr,
            )
            return 1
        return await verify_knowledge_base(
            db, kb=kb, tenant_id=tenant_id, index_version=args.index_version
        )
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one knowledge base's index version: the four counts "
            "(knowledge base / chunk / vector / keyword documents) plus section identity."
        )
    )
    parser.add_argument("--kb-id", required=True, help="knowledge base id to verify")
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="tenant id; defaults to the knowledge base owner's tenant",
    )
    parser.add_argument(
        "--index-version",
        default=None,
        help="index version to verify; defaults to the knowledge base's live version",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（见
    # scripts/rebuild_kb_version.py 的同一条注释）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
