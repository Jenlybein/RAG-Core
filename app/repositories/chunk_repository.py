"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

``count_by_document_ids`` 供文档列表接口一次拿到全部文档的切片数，不做 N+1；
``count_by_kb_ids`` 供检索层判定「空结果到底是哪种空」（票 4）；
``delete_by_document_id`` 由索引重建 / 删除文档复用。

**两个面，一条语句**（第 11 步票 #127）：检索链路上那三条读（折叠回查 meta、折叠取整节、
空结果计数）各自有一个 ``*_async`` 孪生，与同步版**共用同一个语句构造器**。语句写两遍是
迁移期最容易漂移的东西——两边的租户过滤、版本过滤、排序一旦分叉，症状是「异步路径偶尔
多出几条别人的切片」这种事后极难查的故障。

**文档列表的分组计数已经在异步面上**（第 11 步票 #130）：列文档（数据面）与控制面的树
都换了 ``AsyncSession``，所以 ``count_by_document_ids`` 只剩这一份实现。写侧的
``delete_by_document_id`` 仍只有同步面——purge 走的是存储抽象
（``app/services/chunk_store.py``），不经过它。
"""

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Integer, Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def _meta_statement(
    *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> Select[tuple[str, str, dict]]:
    """折叠回查：按 ``(kb_id, chunk_id)`` 一次取回 ``meta``。"""

    # 唯一键是 ``(kb_id, chunk_id)``（ADR-0010），不是单独的 chunk_id：跨库同 ID
    # 的两条结果必须能各自取到自己的 meta，不能被后到的那条覆盖。
    return select(Chunk.kb_id, Chunk.id, Chunk.meta).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id.in_(kb_ids),
        Chunk.id.in_(chunk_ids),
    )


async def fetch_meta_by_chunk_ids_async(
    db: AsyncSession, *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> dict[tuple[str, str], dict]:
    """:func:`fetch_meta_by_chunk_ids` 的异步面（检索链路，第 11 步票 #127）。"""

    if not chunk_ids or not kb_ids:
        return {}
    rows = (
        await db.execute(
            _meta_statement(tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=chunk_ids)
        )
    ).all()
    return {(str(row.kb_id), str(row.id)): dict(row.meta or {}) for row in rows}


def _unit_rows_statement(
    *,
    tenant_id: str,
    document_ids: list[str],
    unit_paths: list[str],
    index_version: str | None,
) -> Select[tuple[str, str, int, str, str, str, int]]:
    """折叠取整节：按 h2 级章节路径取该单元下的全部切片。"""

    heading = Chunk.meta["heading_path"].astext
    clauses = []
    for path in unit_paths:
        clauses.append(heading == path)
        clauses.append(heading.like(f"{path} > %"))
    stmt = (
        select(
            Chunk.document_id,
            heading,
            Chunk.meta["chunk_index"].astext.cast(Integer),
            Chunk.content,
            Chunk.id,
            Chunk.meta["chunk_type"].astext,
            Chunk.meta["table_part"].astext.cast(Integer),
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id.in_(document_ids),
            or_(*clauses),
        )
        .order_by(Chunk.document_id, Chunk.meta["chunk_index"].astext.cast(Integer), Chunk.id)
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    return stmt


def _group_unit_rows(
    rows: Iterable[Sequence[Any]],
) -> dict[tuple[str, str], list[tuple[int, str, str, str, int]]]:
    grouped: dict[tuple[str, str], list[tuple[int, str, str, str, int]]] = {}
    for document_id, path, index, content, chunk_id, chunk_type, table_part in rows:
        grouped.setdefault((str(document_id), str(path)), []).append(
            (int(index or 0), str(content), str(chunk_id), str(chunk_type or ""), int(table_part or 0))
        )
    return grouped


async def fetch_unit_rows_async(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_ids: list[str],
    unit_paths: list[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[tuple[int, str, str, str, int]]]:
    """:func:`fetch_unit_rows` 的异步面（检索链路，第 11 步票 #127）。"""

    if not document_ids or not unit_paths:
        return {}
    stmt = _unit_rows_statement(
        tenant_id=tenant_id,
        document_ids=document_ids,
        unit_paths=unit_paths,
        index_version=index_version,
    )
    return _group_unit_rows(list((await db.execute(stmt)).all()))


def _count_by_kb_ids_statement(
    *, tenant_id: str, kb_ids: list[str], index_version: str | None
) -> Select[tuple[int]]:
    """空结果判定的计数语句：租户 + 库 + （可选）索引版本。"""

    stmt = (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.tenant_id == tenant_id, Chunk.kb_id.in_(kb_ids))
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    return stmt


async def count_by_kb_ids_async(
    db: AsyncSession, *, tenant_id: str, kb_ids: list[str], index_version: str | None = None
) -> int:
    """:func:`count_by_kb_ids` 的异步面（检索链路，第 11 步票 #127）。

    口径与同步版逐字相同：同一条语句、同一个版本过滤、同一个强制租户参数。
    """

    if not kb_ids:
        return 0
    stmt = _count_by_kb_ids_statement(
        tenant_id=tenant_id, kb_ids=kb_ids, index_version=index_version
    )
    return int((await db.execute(stmt)).scalar_one())


async def count_by_document_ids(
    db: AsyncSession, document_ids: list[str], *, tenant_id: str
) -> dict[str, int]:
    """一次分组查询拿回 ``{document_id: 切片数}``；没有切片的文档不在字典里。

    文档列表接口（数据面与控制面树）必须用它，不能对每个文档各查一次（N+1）。
    ``tenant_id`` 是强制过滤参数（ADR-0006）：即使文档 id 已被上游按租户筛过，计数也不跨租户。
    """

    if not document_ids:
        return {}
    stmt = _count_by_document_ids_statement(document_ids, tenant_id=tenant_id)
    return {document_id: int(count) for document_id, count in (await db.execute(stmt)).all()}


def _count_by_document_ids_statement(document_ids: list[str], *, tenant_id: str):
    """「每份文档各有几片」的那一条分组计数。"""

    return (
        select(Chunk.document_id, func.count())
        .where(Chunk.document_id.in_(document_ids), Chunk.tenant_id == tenant_id)
        .group_by(Chunk.document_id)
    )


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()
