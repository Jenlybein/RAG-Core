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

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Integer, Select, delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.chunk import CHUNK_TYPE_REFERENCE_POINTER, Chunk, scope_filter


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


async def fetch_meta_by_chunk_ids(
    db: AsyncSession, *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> dict[tuple[str, str], dict]:
    """:func:`fetch_meta_by_chunk_ids` 的异步面（检索链路，第 11 步票 #127）。"""

    if not chunk_ids or not kb_ids:
        return {}
    rows = (
        await db.execute(_meta_statement(tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=chunk_ids))
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
            (
                int(index or 0),
                str(content),
                str(chunk_id),
                str(chunk_type or ""),
                int(table_part or 0),
            )
        )
    return grouped


async def fetch_unit_rows(
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


def build_parent_rows_statement(
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
    index_version: str | None = None,
) -> Select[tuple[str, str, int, str]]:
    """**父节自己的**正文：按 ``(文档, 精确标题路径)`` 取，供「折叠关闭时补父节」用（#144）。

    为什么要一条新查询、而不是复用 :func:`_unit_rows_statement`：那条是**前缀**语义
    （``path = p OR path LIKE 'p > %'``），拿它取父节会把命中片自己与它的兄弟节一起
    拼回来——材料量翻倍，而 ADR-0027 决策 7 说的正是「不要再翻一倍」。所以这里只认
    **等值**：父节自己那几片，不多不少。

    排序按 ``chunk_index``：父节正文拼回去必须与文档里的顺序一致（ADR-0010 的确定性）。
    """

    heading = Chunk.meta["heading_path"].astext
    chunk_index = Chunk.meta["chunk_index"].astext.cast(Integer)
    stmt = (
        select(Chunk.document_id, heading, chunk_index, Chunk.content)
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id.in_(sorted({str(item) for item in document_ids})),
            heading.in_(sorted({str(item) for item in heading_paths})),
        )
        .order_by(Chunk.document_id, chunk_index, Chunk.id)
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    return stmt


async def fetch_parent_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[str]]:
    """``{(文档 id, 父节路径): [父节自己那几片的正文…]}``，按文档顺序。

    只在这一版本身份里取（``index_version`` 由调用方按「这份文档所属库」给出）：
    跨版本拼父节会让重建中途的检索拿到两套切法拼起来的上下文。
    """

    if not document_ids or not heading_paths:
        return {}
    rows = (
        await db.execute(
            build_parent_rows_statement(
                tenant_id=tenant_id,
                document_ids=list(document_ids),
                heading_paths=list(heading_paths),
                index_version=index_version,
            )
        )
    ).all()
    parents: dict[tuple[str, str], list[str]] = {}
    for document_id, path, _index, content in rows:
        parents.setdefault((str(document_id), str(path)), []).append(str(content or ""))
    return parents


def build_representative_statement(
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    targets: Sequence[tuple[str, str, str]],
) -> Select[tuple[str, str, str, str, str, str, str]]:
    """目标节的**代表片**：注入进候选池的是「这一节的一小片」，不是整节全文（ADR-0027 决策 4）。

    三件事在 ``WHERE`` 里一次说完：

    1. **范围**：租户 + ``kb_id → 版本`` 的配对条件（与召回、折叠、计数同一条
       ``scope_filter``）。目标节落在请求范围外时查不到它，越权由这一步堵死，而不是
       指望建边时禁掉跨库边（那会把最常见的真实关系一起砍掉）；
    2. **身份**：``(库, 文档, 节)`` 三元组，**不是单独的节 id**——节 id 虽然在算法上
       已含文档 id，但拿它当唯一条件等于让「一道 SQL 判越权」依赖另一处算法的正确性；
    3. **路标切片不参战**（ADR-0027 决策 3）：``reference_pointer`` 除了一句「参见别处」
       没有信息量，注入它等于把一句路标当成一条材料。

    排序只用于**确定性**：同一节取 ``chunk_index`` 最小的那一片（先文后表），两次相同
    输入的挑选结果因此一致（ADR-0010 的确定性要求）。
    """

    section = Chunk.meta["section_id"].astext
    chunk_index = Chunk.meta["chunk_index"].astext.cast(Integer)
    triples = sorted(
        {
            (str(kb_id), str(document_id), str(section_id))
            for kb_id, document_id, section_id in targets
        }
    )
    return (
        select(
            Chunk.kb_id,
            Chunk.document_id,
            section,
            Chunk.id,
            Chunk.content,
            Chunk.meta["title"].astext,
            Chunk.meta["heading_path"].astext,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            scope_filter(index_versions),
            tuple_(Chunk.kb_id, Chunk.document_id, section).in_(triples),
            or_(
                Chunk.meta["chunk_type"].astext.is_(None),
                Chunk.meta["chunk_type"].astext != CHUNK_TYPE_REFERENCE_POINTER,
            ),
        )
        .order_by(Chunk.kb_id, Chunk.document_id, section, chunk_index, Chunk.id)
    )


async def fetch_section_representatives(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    targets: Sequence[tuple[str, str, str]],
) -> dict[tuple[str, str, str], tuple[str, str, str, str]]:
    """``{(库, 文档, 节): (代表片 id, 正文, 标题, 章节路径)}``，每节取排序后的第一片。

    目标节在本版本里没有可注入的切片（纯标题节、或只有路标片）时**不在字典里**——
    由调用方跳过它，而不是回一条空正文的材料。
    """

    if not targets or not index_versions:
        return {}
    rows = (
        await db.execute(
            build_representative_statement(
                tenant_id=tenant_id, index_versions=index_versions, targets=targets
            )
        )
    ).all()
    representatives: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
    for kb_id, document_id, section_id, chunk_id, content, title, heading_path in rows:
        key = (str(kb_id), str(document_id), str(section_id))
        if key in representatives:
            continue
        representatives[key] = (
            str(chunk_id),
            str(content or ""),
            str(title or ""),
            str(heading_path or ""),
        )
    return representatives


def _count_by_kb_ids_statement(
    *, tenant_id: str, index_versions: Mapping[str, str]
) -> Select[tuple[int]]:
    """空结果判定的计数语句：租户 + 范围。

    范围用的是**召回时那一条配对条件**（``app.models.chunk.scope_filter``），不是
    「所有库 × 所有版本」：口径一旦与召回不同，「库里有内容、本次没命中」就会被
    误判成「空库」，而这正是 ``empty_reason`` 唯一要说清的事。
    """

    return (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.tenant_id == tenant_id, scope_filter(index_versions))
    )


async def count_by_kb_ids(
    db: AsyncSession, *, tenant_id: str, index_versions: Mapping[str, str]
) -> int:
    """:func:`count_by_kb_ids` 的异步面（检索链路，第 11 步票 #127）。

    口径与召回逐字相同：同一条配对条件、同一个强制租户参数。

    ``index_versions`` 是 ``kb_id → 生效版本`` 的映射（ADR-0025），**不是**一个版本
    常量：多库各自 pin 在不同构建上时，用一个常量的计数会把「别的版本有内容」算成
    「这个库没有内容」。
    """

    if not index_versions:
        return 0
    stmt = _count_by_kb_ids_statement(tenant_id=tenant_id, index_versions=index_versions)
    return int((await db.execute(stmt)).scalar_one())


async def count_by_index_version(
    db: AsyncSession, kb_id: str, *, tenant_id: str, index_version: str
) -> int:
    """该库在**某一个版本**里有多少切片（``ready`` 前的四项计数之一，ADR-0025）。

    与 ``count_by_kb_ids`` 的区别只有一个：这里问的是「某一次构建产出了多少片」，而那
    一条问的是「按生效版本能不能召回」。两者都带租户，也都不许省。
    """

    stmt = (
        select(func.count())
        .select_from(Chunk)
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.kb_id == kb_id,
            Chunk.index_version == index_version,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_section_identity_rows(
    db: AsyncSession, *, tenant_id: str, kb_id: str, index_version: str
) -> list[tuple[str, str, str, str | None]]:
    """这个版本里**每个节**的 ``(文档 id, 标题路径, section_id, parent_section_id)``。

    核对脚本用它验「节的身份证」自洽（#143）：同一节的多片共享同一份 meta，所以先
    ``distinct`` 再判——一片一行会让「这条节被判了 20 次」变成报错条数虚高。

    读的是 ``metadata``（JSONB）里的三个键，不是新增的列：节的身份从第 3 步起就随切片
    落在那份 meta 里，核对不该为了它改表（改表要连带改迁移与回填，而这里只需要「读得到」）。
    """

    stmt = (
        select(
            Chunk.document_id,
            Chunk.meta["heading_path"].astext,
            Chunk.meta["section_id"].astext,
            Chunk.meta["parent_section_id"].astext,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.kb_id == kb_id,
            Chunk.index_version == index_version,
        )
        .distinct()
    )
    rows = (await db.execute(stmt)).all()
    return [
        (str(document_id), str(path or ""), str(section_id or ""), parent or None)
        for document_id, path, section_id, parent in rows
    ]


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


def build_version_sections_statement(
    *,
    tenant_id: str,
    kb_id: str,
    index_version: str,
    document_ids: Sequence[str] | None = None,
) -> Select:
    """某个版本里的切片，按「建关系边要的那几个字段」一次读全（#152）。

    只选 ``id`` / ``document_id`` / ``content`` / ``metadata`` 四列，**不选 embedding**：
    一次整版扫描若把 1536 维向量拖回来，读的就是几 GB 而不是几十 MB，而建边一个维度都
    用不上。``metadata`` 里的键（title / section_id / heading_path / chunk_index）在
    Python 侧解——节的身份从 #143 起就在那份 meta 里，关系边不该为了读它改切片表。

    ``document_ids`` 非空时只读这几篇（单篇重建的那条路）。
    """

    statement = select(Chunk.id, Chunk.document_id, Chunk.content, Chunk.meta).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id == kb_id,
        Chunk.index_version == index_version,
    )
    if document_ids is not None:
        statement = statement.where(Chunk.document_id.in_(list(document_ids)))
    return statement.order_by(
        Chunk.document_id,
        Chunk.meta["chunk_index"].astext.cast(Integer),
        Chunk.id,
    )


async def list_version_sections(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    index_version: str,
    document_ids: Sequence[str] | None = None,
) -> list[tuple[str, str, str, str, int, str]]:
    """:func:`build_version_sections_statement` 的结果：``(文档 id, 标题, 节 id, 标题路径, 片序号, 正文)``。

    一行一片（同一节多行）：「第 N 节」按片的全局顺序解析，去重交给目录
    （``SectionCatalog`` 按 ``(文档 id, 节 id)`` 收）。与 ``list_section_identity_rows``
    的差别是：那个 ``distinct`` 只关心节身份，这个要原文与顺序。
    """

    rows = (
        await db.execute(
            build_version_sections_statement(
                tenant_id=tenant_id,
                kb_id=kb_id,
                index_version=index_version,
                document_ids=document_ids,
            )
        )
    ).all()
    sections: list[tuple[str, str, str, str, int, str]] = []
    for row in rows:
        meta = dict(getattr(row, "meta", None) or {})
        sections.append(
            (
                str(row.document_id),
                str(meta.get("title") or ""),
                str(meta.get("section_id") or ""),
                str(meta.get("heading_path") or ""),
                int(meta.get("chunk_index") or 0),
                str(row.content or ""),
            )
        )
    return sections


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()
