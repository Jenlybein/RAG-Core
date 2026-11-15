"""文档版本对账脚本（第 12 步票 #179）：数出四条不变量的分歧，逐条列出。

```text
uv run python scripts/verify_document_versions.py [--kb-id <id>] [--limit N]
```

## 它回答什么

第 12 步把同一件事放在了几个地方（正文在版本表与文档行各一份、稿号在切片与指针各一份、
「当前稿」在库里与关键词索引各一份），每一处都可能悄悄漂移，而**漂移从不报错**：检索照常
返回，只是返回的是另一稿的东西。四条检查（判据全在
``app.services.document_version_reconcile``，这里只取数与打印）：

1. 每个切片的稿号都能找到对应的版本行；
2. 每篇文档恰好一个当前稿版本行，且等于文档上的指针；
3. 文档正文与当前稿正文逐字相同；
4. 关键词索引里标着「当前稿」的切片，稿号等于文档指针。

再加两个**信息项**（父 spec D5 / #174 的 mapping 补丁，两者都不判失败）：
指针那一稿没有切片（重建可能还在跑）、关键词索引里一篇都没标当前稿（老文档缺字段）。

## 只读

它一条写语句都不发、不提交、不开事务：对账是**观察**，看到的必须是别人留下的状态，
而不是自己刚写的。所以这里没有 ``--apply``（与清理脚本刻意不同）：一条会对账出分歧的
命令，永远不该顺手改点什么。

## 关键词那一维的「不可判定」

没有部署关键词引擎（``KEYWORD_SEARCH_PROVIDER=none``）时这一维**没有答案**，脚本打印
``undecidable`` 与原因、**不判失败**。把「答不上来」记成通过是撒谎；记成失败会让这种
部署里的对账永远红——然后被人加上 ``|| true``，于是四条全部失效。两种原因分开打印：
``capability is not available``（不用排障）与 ``read failed``（要排障）。

## 退出码

0 = 四条都没有分歧；1 = 有分歧（逐条已列出）。**不复用 ``_ops_common.exit_code_for``**：
那条规则是「有 failed 且一份都没投递成功才算失败」（部分失败仍算跑过），而对账恰恰相反
——一处分歧就是失败。套用它会让「库里有 1 处分歧」退 0，而这条脚本存在的理由就是让
分歧能进 CI 的门。

模块顶层不解析 argv、不连库、不建引擎：导入即安全。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterator, Sequence
from typing import Any

import structlog

from app.core.logging import setup_logging
from app.models.document import Document
from app.repositories import chunk_repository, document_repository, document_version_repository
from app.services.document_version_reconcile import (
    CHECK_CHUNK_VERSION_ROWS,
    CHECK_CONTENT_SNAPSHOT,
    CHECK_CURRENT_DRAFT,
    CHECK_KEYWORD_CURRENT_DRAFT,
    KEYWORD_STATE_CHECKED,
    DocumentFacts,
    ReconcileReport,
    VersionRow,
    keyword_state_of,
    reconcile,
)

logger = structlog.get_logger(__name__)

#: 一批读多少篇文档。批读的三个理由：版本行 / 切片稿号 / 关键词查询都是**一条语句打一批**
#: （逐篇发语句是「文档数」次往返，而运维恰恰在库最大的时候跑对账）；同时批不会大到把
#: 一条 ``IN (...)`` 撑成一次超时。
BATCH_SIZE = 200

#: 四条检查的打印用名字（英文，与仓库其它脚本一致；顺序即打印顺序）。
CHECK_LABELS = {
    CHECK_CHUNK_VERSION_ROWS: "every chunk doc_version has a version row",
    CHECK_CURRENT_DRAFT: "exactly one current draft per document, equal to the pointer",
    CHECK_CONTENT_SNAPSHOT: "documents.content equals the current draft body verbatim",
    CHECK_KEYWORD_CURRENT_DRAFT: "keyword current-draft flag matches the pointer",
}

OK_NOTE = (
    "RESULT: OK (no disagreement in the four checks. "
    'The undecidable keyword dimension, if any, is not a failure: "capability absent" '
    'and "read failed" are printed separately.)'
)

FAILED_NOTE = (
    "RESULT: FAILED (each disagreement is listed above; nothing was written — "
    "this script is read-only)"
)


async def reconcile_document_versions(
    db: Any,
    *,
    kb_id: str | None = None,
    limit: int | None = None,
    provider: Any = None,
) -> ReconcileReport:
    """扫一遍、判一遍，返回报表（不打印）；``provider`` 缺省取当前配置的关键词实现。

    三步：扫文档 → 按批读版本行与切片稿号 → 按租户问一遍关键词索引。判据一行都不在这里
    （见 ``app.services.document_version_reconcile``）：这一层只负责**取数**与**分「答不上来」
    的原因**。
    """

    documents = await document_repository.list_for_version_reconcile(
        db, kb_id=kb_id, limit=limit
    )
    keyword_versions, keyword_state, keyword_reason = await _keyword_current_versions(
        documents, provider if provider is not None else _keyword_provider()
    )
    # **不可判定用 ``None`` 表达**，而不是空字典：空字典的意思是「查过了，这篇一篇都没标
    # 当前稿」（信息项）；混成一个会让「没部署引擎」看起来像「数据缺失」。
    answered: dict[str, tuple[int, ...]] | None = (
        keyword_versions if keyword_state == KEYWORD_STATE_CHECKED else None
    )

    facts: list[DocumentFacts] = []
    for batch in _batches(documents, BATCH_SIZE):
        document_ids = [document.id for document in batch]
        version_rows = await document_version_repository.list_for_documents(db, document_ids)
        counts = await chunk_repository.draft_counts(db, document_ids=document_ids)
        facts.extend(
            _facts_of(batch, version_rows=version_rows, counts=counts, keyword=answered)
        )

    return reconcile(facts, keyword_state=keyword_state, keyword_reason=keyword_reason)


def _facts_of(
    documents: Sequence[Document],
    *,
    version_rows: Sequence[Any],
    counts: Sequence[tuple[str, int, int]],
    keyword: dict[str, tuple[int, ...]] | None,
) -> list[DocumentFacts]:
    """把三条读数的行摊成「一篇文档一份读数」。

    ``keyword`` 为 ``None`` 时整维不可判定（每篇都是 ``None``）；否则**答案里没有这一篇**
    就是空元组——「查过、这一篇一篇都没标当前稿」，那是信息项而不是不可判定。
    """

    versions_by_document: dict[str, list[VersionRow]] = {}
    for row in version_rows:
        versions_by_document.setdefault(row.document_id, []).append(
            VersionRow(
                version=int(row.version), status=str(row.status), content=str(row.content or "")
            )
        )

    counts_by_document: dict[str, list[tuple[int, int]]] = {}
    for document_id, doc_version, count in counts:
        counts_by_document.setdefault(document_id, []).append((int(doc_version), int(count)))

    return [
        DocumentFacts(
            document_id=document.id,
            pointer=int(document.current_version),
            content=str(document.content or ""),
            version_rows=tuple(versions_by_document.get(document.id, ())),
            chunk_versions=tuple(counts_by_document.get(document.id, ())),
            keyword_current_versions=(
                None if keyword is None else tuple(keyword.get(document.id, ()))
            ),
        )
        for document in documents
    ]


async def _keyword_current_versions(
    documents: Sequence[Document], provider: Any
) -> tuple[dict[str, tuple[int, ...]], str, str | None]:
    """按租户批量问一遍关键词索引：``(答案, 状态, 原因)``。

    **不可判定时不问也不猜**：能力不存在就一次查询都不发；某一批读失败就整维降级为
    「读不到」并把原因交上去（半份答案会让「没查到的文档」看起来像「没有当前稿标记」）。
    租户分组是硬性的（ADR-0006）：一句按租户隔离的读，跨租户问会把别人的稿号数进来。
    """

    state, reason = keyword_state_of(available=_keyword_available(provider))
    if state != KEYWORD_STATE_CHECKED:
        return {}, state, reason

    versions: dict[str, tuple[int, ...]] = {}
    for tenant_id, document_ids in _by_tenant(documents):
        try:
            answer = await provider.current_draft_versions(
                tenant_id=tenant_id, document_ids=document_ids
            )
        except Exception as exc:  # noqa: BLE001 - 能力在、这次读不到 → 不可判定（不是分歧）
            # 只记错误类型与规模：异常文本可能带集群地址，文档内容更是一个字都不该进日志。
            logger.warning(
                "document version reconcile: keyword read failed",
                error=type(exc).__name__,
                documents=len(document_ids),
            )
            return {}, *keyword_state_of(available=True, error="failed")
        for document_id, drafts in (answer or {}).items():
            versions[str(document_id)] = tuple(sorted(int(draft) for draft in drafts))

    return versions, *keyword_state_of(available=True)


def _keyword_available(provider: Any) -> bool:
    """这项能力在不在（``available`` 是**能力存在性**，不是「这次查到了没有」）。"""

    return bool(getattr(provider, "available", False))


def _keyword_provider() -> Any:
    """当前配置的关键词实现（惰性 import：模块导入时不构造 Provider / 不建引擎）。"""

    from app.providers.keyword_search import get_keyword_search_provider

    return get_keyword_search_provider()


def _by_tenant(documents: Sequence[Document]) -> list[tuple[str, list[str]]]:
    """按租户分组（稳定的顺序）：关键词那一维只能逐租户问。"""

    grouped: dict[str, list[str]] = {}
    for document in documents:
        grouped.setdefault(str(document.tenant_id), []).append(str(document.id))
    return [(tenant_id, ids) for tenant_id, ids in grouped.items()]


def _batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _render(report: ReconcileReport, *, scope: str) -> list[str]:
    """报表 → 要打印的行（纯函数：打印顺序就是这里决定的顺序）。

    头两行是规模（扫了几篇、几片），然后四条检查**逐条**给计数与明细，最后是两个信息项。
    信息项也打计数——「这一项今天是 0」本身是运维要知道的事（0 与非 0 的区别正好是
    「重建跑完了没有」）。
    """

    lines: list[str] = [
        f"verify_document_versions (read-only): {scope}",
        f"  documents: {report.documents}",
        f"  chunks: {report.chunks}",
    ]
    for index, (name, items) in enumerate(report.checks, start=1):
        label = CHECK_LABELS[name]
        if name == CHECK_KEYWORD_CURRENT_DRAFT and report.keyword_undecidable:
            lines.append(
                f"  check {index}/4 {label}: undecidable（{report.keyword_reason}）"
            )
            continue
        lines.append(f"  check {index}/4 {label}: {len(items)} disagreement(s)")
        lines.extend(f"    - {item.render()}" for item in items)

    lines.append(
        f"  info: pointer drafts without chunks: {len(report.pointer_drafts_without_chunks)}"
        "（rebuild may still be in flight — not a failure）"
    )
    lines.extend(
        f"    - {item.render()}" for item in report.pointer_drafts_without_chunks
    )
    if not report.keyword_undecidable:
        lines.append(
            "  info: keyword drafts without a current-draft slice: "
            f"{len(report.keyword_drafts_without_current)}（legacy docs / not written yet"
            " — not a failure）"
        )
        lines.extend(
            f"    - {item.render()}" for item in report.keyword_drafts_without_current
        )
    else:
        lines.append(
            "  （关键词那一维不可判定：能力不存在与数据缺失是两件事，这一维不判失败）"
        )
    return lines


def exit_code_for(report: ReconcileReport) -> int:
    """有分歧退 1，零分歧退 0（**不是** ``_ops_common.exit_code_for``，理由见模块文档）。"""

    return 1 if report.has_disagreements else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the four document-version invariants (chunk draft has a version row / "
            "exactly one current draft equal to the pointer / documents.content equals the "
            "current draft body / keyword current-draft flag matches the pointer). Read-only."
        )
    )
    parser.add_argument(
        "--kb-id", default=None, help="only scan this knowledge base (default: all)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="scan at most N documents (debugging)"
    )
    return parser


async def _open_session() -> Any:
    """开一个运维会话（惰性 import：模块导入时绝不建引擎）。"""

    from app.db.async_session import AsyncSessionLocal

    return AsyncSessionLocal()


async def _run(args: argparse.Namespace) -> int:
    db = await _open_session()
    try:
        report = await reconcile_document_versions(db, kb_id=args.kb_id, limit=args.limit)
    finally:
        await db.close()

    scope = "all knowledge bases" if args.kb_id is None else f"kb_id={args.kb_id}"
    for line in _render(report, scope=scope):
        print(line)
    print(FAILED_NOTE if report.has_disagreements else OK_NOTE)
    return exit_code_for(report)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的会话 → 报表与退出码。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        # 负数会让「扫几篇」变成「一篇都不扫却报 OK」——那是最坏的一种假绿，宁可退 2。
        parser.error("--limit must be >= 0")

    setup_logging()
    # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（与其它运维脚本同一条
    # 注释）：运维脚本多半在开发机上被敲。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
