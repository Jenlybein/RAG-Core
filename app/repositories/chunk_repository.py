"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

``count_by_document_ids`` 供文档列表接口一次拿到全部文档的切片数，不做 N+1；
``count_by_kb_ids`` 供检索层判定「空结果到底是哪种空」（票 4）；
``delete_by_document_id`` 由索引重建 / 删除文档复用。
"""

from sqlalchemy import Integer, delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def fetch_meta_by_chunk_ids(
    db: Session, *, tenant_id: str, chunk_ids: list[str]
) -> dict[str, dict]:
    """按切片 ID 一次取回它们的 ``meta``，供检索层定位所属章节。

    检索结果一路从向量/关键词通道到融合层，只带 ``document_id`` / ``chunk_id`` /
    ``content`` 这些**对外字段**（``meta`` 不属于检索契约）。父节需要用
    ``section_id``，所以在这里按 ID 回查一次，而不是把内部字段塞进融合结果、
    再让它们出现在响应里。``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    if not chunk_ids:
        return {}
    stmt = select(Chunk.id, Chunk.meta).where(
        Chunk.tenant_id == tenant_id, Chunk.id.in_(chunk_ids)
    )
    return {str(row.id): dict(row.meta or {}) for row in db.execute(stmt).all()}


def fetch_section_chunks(
    db: Session,
    *,
    tenant_id: str,
    document_ids: list[str],
    section_ids: list[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[str]]:
    """一次查出这些文档里、这些章节的全部切片正文（按 ``order_index`` 排序）。

    返回 ``{(document_id, section_id): [正文...]}``，供检索层拼「父节」用。
    **必须按 (文档, 章节) 而不是只按章节**：同一知识库里两篇文档完全可以共用
    同名标题（《退款政策》与《换货政策》都有「申请条件」），只按 section_id 查
    会把两篇的正文拼成一个假章节。

    一次批量查询而不是逐条查：一次检索的 top_k 条可能全都落在同一节，逐条查
    会把同一次查询重复十几次。``tenant_id`` 是强制过滤参数（ADR-0006）。

    ``index_version`` 与召回时的过滤口径保持一致：不同批次的切片正文可能不同，
    父节若混入另一批次，调用方会拿到「和命中片对不上」的上下文。
    """

    if not document_ids or not section_ids:
        return {}
    stmt = (
        select(
            Chunk.document_id,
            Chunk.meta["section_id"].astext,
            Chunk.meta["order_index"].astext.cast(Integer),
            Chunk.content,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id.in_(document_ids),
            Chunk.meta["section_id"].astext.in_(section_ids),
        )
        # 章节内必须按 order_index 排：乱序拼出来的「父节」读起来是断的。
        .order_by(Chunk.document_id, Chunk.meta["order_index"].astext.cast(Integer))
    )
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    grouped: dict[tuple[str, str], list[str]] = {}
    for document_id, section_id, _order, content in db.execute(stmt).all():
        grouped.setdefault((document_id, str(section_id)), []).append(str(content))
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
