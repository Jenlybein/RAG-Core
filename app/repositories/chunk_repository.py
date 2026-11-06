"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

``count_by_document_ids`` 供文档列表接口一次拿到全部文档的切片数，不做 N+1；
``delete_by_document_id`` 由索引重建 / 删除文档复用。
"""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def count_by_kb(db: Session, kb_id: str) -> int:
    stmt = select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id)
    return int(db.execute(stmt).scalar_one())


def count_by_document_ids(db: Session, document_ids: list[str]) -> dict[str, int]:
    """一次分组查询拿回 ``{document_id: 切片数}``；没有切片的文档不在字典里。

    文档列表接口必须用它，不能对每个文档各查一次（N+1）。
    """

    if not document_ids:
        return {}
    stmt = (
        select(Chunk.document_id, func.count())
        .where(Chunk.document_id.in_(document_ids))
        .group_by(Chunk.document_id)
    )
    return {document_id: int(count) for document_id, count in db.execute(stmt).all()}


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()
