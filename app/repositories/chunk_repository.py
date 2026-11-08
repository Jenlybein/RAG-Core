"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

``count_by_document_ids`` 供文档列表接口一次拿到全部文档的切片数，不做 N+1；
``count_by_kb_ids`` 供检索层判定「空结果到底是哪种空」（票 4）；
``delete_by_document_id`` 由索引重建 / 删除文档复用。
"""

from sqlalchemy import Integer, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def fetch_meta_by_chunk_ids(
    db: Session, *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> dict[tuple[str, str], dict]:
    """按 ``(kb_id, chunk_id)`` 一次取回 ``meta``，供检索层定位与回填。

    检索结果一路从向量/关键词通道到融合层，只带 ``document_id`` / ``chunk_id`` /
    ``content`` 这些**对外字段**（``meta`` 不属于检索契约）。父节需要用
    ``section_id``，所以在这里按 ID 回查一次，而不是把内部字段塞进融合结果、
    再让它们出现在响应里。``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    if not chunk_ids or not kb_ids:
        return {}
    # 唯一键是 ``(kb_id, chunk_id)``（ADR-0010），不是单独的 chunk_id：跨库同 ID
    # 的两条结果必须能各自取到自己的 meta，不能被后到的那条覆盖。
    stmt = select(Chunk.kb_id, Chunk.id, Chunk.meta).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id.in_(kb_ids),
        Chunk.id.in_(chunk_ids),
    )
    return {
        (str(row.kb_id), str(row.id)): dict(row.meta or {})
        for row in db.execute(stmt).all()
    }


def fetch_table_chunks(
    db: Session,
    *,
    tenant_id: str,
    document_ids: list[str],
    section_ids: list[str],
    index_version: str | None = None,
) -> dict[tuple[str, str, int], str]:
    """取这些文档/章节里的**整表片**正文，供「行级召回 → 整表返回」展开。

    返回 ``{(document_id, section_id, table_part): 整表文本}``。

    为什么行级片要展开成整表：命中「型号=XR-2100，配件=万兆光模块」说明用户问的
    是表里某个单元格，但只把这一行喂给模型，它看不到列含义与相邻行、也没法对比。
    行级片负责**找得准**，整表片负责**读得懂**。

    ``tenant_id`` 是强制过滤参数（ADR-0006）：展开也要按租户拦住。
    """

    if not document_ids or not section_ids:
        return {}
    stmt = (
        select(
            Chunk.document_id,
            Chunk.meta["section_id"].astext,
            Chunk.meta["table_part"].astext.cast(Integer),
            Chunk.content,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id.in_(document_ids),
            Chunk.meta["section_id"].astext.in_(section_ids),
            Chunk.meta["chunk_type"].astext == "table",
        )
        # 确定性排序（ADR-0010）：同一份输入两次必须给出同一张表，不能取决于
        # PG 的行序。再加 index_version 过滤：混进另一批次会出现「表与命中片
        # 对不上」的上下文。
        .order_by(Chunk.document_id, Chunk.meta["table_part"].astext.cast(Integer), Chunk.id)
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    tables: dict[tuple[str, str, int], str] = {}
    for document_id, section_id, table_part, content in db.execute(stmt).all():
        key = (str(document_id), str(section_id), int(table_part or 0))
        # 先到先得：排序已定，重复键不该由「谁后到」决定。
        tables.setdefault(key, str(content))
    return tables


def fetch_unit_rows(
    db: Session,
    *,
    tenant_id: str,
    document_ids: list[str],
    unit_paths: list[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[tuple[int, str, str, str, int]]]:
    """按 **h2 级章节路径**取该单元下的全部切片，含挂在这一节自己身上的片。

    返回 ``{(document_id, unit_path): [(chunk_index, 正文, chunk_id, chunk_type, table_part)...]}``。

    为什么按路径前缀而不是 ``parent_section_id``：后者是**上一级**，对 h2 级的
    切片（节下没有 h3）它指向 h1，按它归组会把整篇文档并成一个单元。这里用
    「路径等于它」或「以它开头」，两个层级都收进来。
    """

    if not document_ids or not unit_paths:
        return {}
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
    grouped: dict[tuple[str, str], list[tuple[int, str, str, str, int]]] = {}
    for document_id, path, index, content, chunk_id, chunk_type, table_part in (
        db.execute(stmt).all()
    ):
        grouped.setdefault((str(document_id), str(path)), []).append(
            (int(index or 0), str(content), str(chunk_id), str(chunk_type or ""), int(table_part or 0))
        )
    return grouped


def fetch_parent_section_chunks(
    db: Session,
    *,
    tenant_id: str,
    document_ids: list[str],
    parent_section_ids: list[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[tuple[int, str, str]]]:
    """一次查出这些**父节**（h2 级）下的全部切片，供上下文恢复拼回整个父节。

    返回 ``{(document_id, parent_section_id): [(chunk_index, 正文, chunk_id)...]}``。

    为什么按 ``parent_section_id`` 而不是 ``section_id``：h3 成为切片边界后，
    ``section_id`` 指的是子节（h3），而「父节」在这一步的语义是 h2 级的那一节。
    ``parent_section_id`` 正是为此存在的（见 GLOSSARY「父块」），此前一直没被用上。

    为什么按 ``chunk_index``（文档级）排序：``order_index`` 只是节内序号，跨 h3 拼
    一个 h2 时它排不出文档顺序，拼出来的上下文会是断的。
    """

    if not document_ids or not parent_section_ids:
        return {}
    stmt = (
        select(
            Chunk.document_id,
            Chunk.meta["parent_section_id"].astext,
            Chunk.meta["chunk_index"].astext.cast(Integer),
            Chunk.content,
            Chunk.id,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id.in_(document_ids),
            Chunk.meta["parent_section_id"].astext.in_(parent_section_ids),
        )
        # 确定性的文档顺序（ADR-0010）：同一份输入两次拼出的父节必须一样。
        .order_by(
            Chunk.document_id, Chunk.meta["chunk_index"].astext.cast(Integer), Chunk.id
        )
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    grouped: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for document_id, parent_id, chunk_index, content, chunk_id in db.execute(stmt).all():
        grouped.setdefault((str(document_id), str(parent_id)), []).append(
            (int(chunk_index or 0), str(content), str(chunk_id))
        )
    return grouped


def count_by_kb(db: Session, kb_id: str) -> int:
    stmt = select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id)
    return int(db.execute(stmt).scalar_one())


def count_by_kb_ids(
    db: Session, *, tenant_id: str, kb_ids: list[str], index_version: str | None = None
) -> int:
    """这些库里共有多少条可检索切片（一次 count，不按库循环）。

    检索层用它区分「库内没有可检索内容」（``0``）与「有内容但本次没命中」
    （``> 0``）。**不许用本次结果的条数去猜**：某库没有切片进入结果不是失败，
    而结果为空时，只有数据库知道是「空库」还是「没命中」。``tenant_id`` 是强制
    过滤参数（ADR-0006），计数绝不跨租户。

    传了 ``index_version`` 就按版本过滤，**调用方必须与召回时的过滤口径一致**：
    否则「库里只有另一个版本的切片」会被算成「有内容」，把「该换个问法」误报成
    「没命中」，把排查引向错误的方向。
    """

    if not kb_ids:
        return 0
    stmt = (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.tenant_id == tenant_id, Chunk.kb_id.in_(kb_ids))
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    return int(db.execute(stmt).scalar_one())


def count_by_document_ids(
    db: Session, document_ids: list[str], *, tenant_id: str
) -> dict[str, int]:
    """一次分组查询拿回 ``{document_id: 切片数}``；没有切片的文档不在字典里。

    文档列表接口必须用它，不能对每个文档各查一次（N+1）。``tenant_id`` 是强制
    过滤参数（ADR-0006）：即使文档 id 已被上游按租户筛过，计数也不跨租户。
    """

    if not document_ids:
        return {}
    stmt = (
        select(Chunk.document_id, func.count())
        .where(Chunk.document_id.in_(document_ids), Chunk.tenant_id == tenant_id)
        .group_by(Chunk.document_id)
    )
    return {document_id: int(count) for document_id, count in db.execute(stmt).all()}


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()
