"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

本步没有删除接口，这两个方法先就位；阶段 3 重建索引时会用到。
"""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


def count_by_kb(db: Session, kb_id: str) -> int:
    stmt = select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id)
    return int(db.execute(stmt).scalar_one())


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()