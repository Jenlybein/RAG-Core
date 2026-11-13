"""关系边的接线层：读节目录 → 抽取 → 先删后写 → 清悬空（ADR-0027 / 票 #152）。

**索引期是唯一建边时点**。切片写进哪个版本，边就写进哪个版本——两者共用一个事务，
于是「构建成功但这个版本没有边」这种中间态不存在（ADR-0025 决策一 / 决策二）。

## 重建语义：先删、再建、再清一遍悬空

重建某一份文档时按三步走（见 :func:`rebuild_document_edges`）：

```text
① 删掉这份文档在本版本里的边（它作为起点、也作为终点）
② 从它现在的节与正文重新抽，幂等写入（同一条边不会变成两条）
③ 用本版本里**真实的**节全集，删掉两边至少一端已不存在的边（悬空）
```

第 ③ 步是必须的：**别的文档**指向它的边可能指向已经消失的节，只删一侧的话重建之后库里
会留下悬空边——检索时拉不到东西，却不报错。这就是验收里「重建同一文档后边仍指向存在
的节」的实现方式。

## 为什么整库构建还要再跑一遍

跨文档引用有一个天然的先后问题：A 引用 B 的 2.4 节时，B 可能还没被这一轮构建写入——
那一刻目录里没有 B，按「悬空边一律不建」的规则这条边不会建。所以
:meth:`IndexBuildService.build` 在逐篇重建**之后**调用 :func:`rebuild_version_edges`
再算一遍（幂等，成本是每篇多抽一次）。这不是「两套实现」，是同一份抽取跑两遍：
第二遍的目录才是这一版本完整的目录。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import chunk_repository, section_edge_repository
from app.services.section_graph import (
    EdgeDraft,
    EdgeScope,
    SectionCatalog,
    SectionRecord,
    SectionText,
    extract_section_edges,
)
from app.utils.section_identity import section_id_of

logger = structlog.get_logger(__name__)


def catalog_from_rows(
    rows: Iterable[tuple[str, str, str, str, int, str]], *, scope: EdgeScope
) -> SectionCatalog:
    """``(文档 id, 标题, 节 id, 标题路径, 片序号, 正文)`` → 节目录。

    节 id 用的是**库里那一份**（切片 meta 里的 ``section_id``）：边必须指向真实存在的
    节，而不是「按同一算法重算出来的那个」。重算只在这一列缺值时兜底（历史切片 / 自定义
    切块器），并且走 :func:`section_id_of`——本模块不许自己拼哈希。
    """

    sections: list[SectionRecord] = []
    for document_id, title, section_id, heading_path, order, _content in rows:
        sections.append(
            SectionRecord(
                tenant_id=scope.tenant_id,
                kb_id=scope.kb_id,
                index_version=scope.index_version,
                document_id=document_id,
                document_title=title,
                section_id=section_id
                or section_id_of(document_id=document_id, heading_path=heading_path),
                heading_path=heading_path,
                order=order,
            )
        )
    return SectionCatalog.of(sections, scope=scope)


def section_texts_from_rows(
    rows: Iterable[tuple[str, str, str, str, int, str]], *, scope: EdgeScope
) -> tuple[SectionText, ...]:
    """同一批行 → 每片一份 ``SectionText``（引用从正文里抽，所以正文必须跟着来）。"""

    entries: list[SectionText] = []
    for document_id, title, section_id, heading_path, order, content in rows:
        entries.append(
            SectionText(
                record=SectionRecord(
                    tenant_id=scope.tenant_id,
                    kb_id=scope.kb_id,
                    index_version=scope.index_version,
                    document_id=document_id,
                    document_title=title,
                    section_id=section_id
                    or section_id_of(document_id=document_id, heading_path=heading_path),
                    heading_path=heading_path,
                    order=order,
                ),
                text=content,
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class EdgeRebuildResult:
    """一次重建的可观测结果：写了哪些边、清掉了几条（悬空）。"""

    edges: tuple[EdgeDraft, ...]

    @property
    def count(self) -> int:
        return len(self.edges)


async def load_catalog(db: AsyncSession, *, scope: EdgeScope) -> SectionCatalog:
    """读本版本已有的节目录（作用域三列全带，跨租户 / 跨版本的节读不出来）。"""

    rows = await chunk_repository.list_version_sections(
        db,
        tenant_id=scope.tenant_id,
        kb_id=scope.kb_id,
        index_version=scope.index_version,
    )
    return catalog_from_rows(rows, scope=scope)


async def rebuild_document_edges(
    db: AsyncSession,
    *,
    scope: EdgeScope,
    document_id: str,
    document_title: str,
    sections: Sequence[SectionText],
    catalog: SectionCatalog | None = None,
) -> EdgeRebuildResult:
    """重建一份文档在本版本里的边（先删 → 再建 → 清悬空），**不提交**。

    ``sections`` 是这份文档刚写进去的那些切片（索引期手里就有，不必回库读一遍）；
    ``catalog`` 缺省时回库读——它决定「目标节存不存在」，所以必须在切片写入之后读。

    事务归调用方：索引期的 Tx2 里 purge + 写切片 + 建边 + 回写状态同成同败，于是不会
    出现「切片好了、边还是空的」这种状态。
    """

    resolved = catalog if catalog is not None else await load_catalog(db, scope=scope)
    drafts = extract_section_edges(
        scope=scope,
        document_id=document_id,
        document_title=document_title,
        sections=sections,
        catalog=resolved,
    )

    await section_edge_repository.delete_for_document(db, scope=scope, document_id=document_id)
    await section_edge_repository.insert_many(db, drafts)
    await section_edge_repository.prune_dangling(
        db, scope=scope, valid_pairs=_valid_pairs(resolved)
    )
    logger.info(
        "section edges rebuilt",
        tenant_id=scope.tenant_id,
        kb_id=scope.kb_id,
        index_version=scope.index_version,
        document_id=document_id,
        edge_count=len(drafts),
    )
    return EdgeRebuildResult(edges=drafts)


async def rebuild_version_edges(db: AsyncSession, *, scope: EdgeScope) -> int:
    """把**整个版本**的边重算一遍（幂等），返回写入的边数。

    整库构建收尾用：此时目录才是完整的目录，跨文档引用才能解析到这一轮刚写进来的目标。
    逐文档调用同一份 :func:`rebuild_document_edges`，不另写一套。
    """

    rows = await chunk_repository.list_version_sections(
        db,
        tenant_id=scope.tenant_id,
        kb_id=scope.kb_id,
        index_version=scope.index_version,
    )
    catalog = catalog_from_rows(rows, scope=scope)
    entries = section_texts_from_rows(rows, scope=scope)

    grouped: dict[str, list[SectionText]] = {}
    for entry in entries:
        grouped.setdefault(entry.record.document_id, []).append(entry)

    total = 0
    for document_id, document_entries in grouped.items():
        title = document_entries[0].record.document_title
        result = await rebuild_document_edges(
            db,
            scope=scope,
            document_id=document_id,
            document_title=title,
            sections=document_entries,
            catalog=catalog,
        )
        total += result.count

    if not grouped:
        # 一个节都没有的版本：边一条都不该留（否则全是悬空边）。
        await section_edge_repository.prune_dangling(db, scope=scope, valid_pairs=())
    logger.info(
        "section edges rebuilt for the whole version",
        tenant_id=scope.tenant_id,
        kb_id=scope.kb_id,
        index_version=scope.index_version,
        document_count=len(grouped),
        edge_count=total,
    )
    return total


def _valid_pairs(catalog: SectionCatalog) -> set[tuple[str, str]]:
    return {(record.document_id, record.section_id) for record in catalog.sections}


class SectionEdgeBuilder:
    """索引期建边的默认实现（一个可注入的薄壳）。

    为什么包一层而不是让 ``IndexingService`` 直接调函数：索引链路的其余协作者
    （切块器 / embedding / 存储列表）都是构造注入的，测试用记录型替身跑整条编排而不连库。
    建边是第五个协作者，形状必须一致，否则「这条流水线可离线跑完」就少了一角。
    """

    async def rebuild_document(
        self,
        db: AsyncSession,
        *,
        scope: EdgeScope,
        document_id: str,
        document_title: str,
        sections: Sequence[SectionText],
    ) -> EdgeRebuildResult:
        return await rebuild_document_edges(
            db,
            scope=scope,
            document_id=document_id,
            document_title=document_title,
            sections=sections,
        )


__all__ = [
    "EdgeRebuildResult",
    "SectionEdgeBuilder",
    "catalog_from_rows",
    "load_catalog",
    "rebuild_document_edges",
    "rebuild_version_edges",
    "section_texts_from_rows",
]
