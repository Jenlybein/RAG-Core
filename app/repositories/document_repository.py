"""文档数据访问：创建文档、回写处理状态。"""

from sqlalchemy.orm import Session

from app.models.document import Document


def create(db: Session, doc: Document) -> Document:
    db.add(doc)
    db.flush()
    return doc


def get_by_id(db: Session, document_id: str) -> Document | None:
    return db.get(Document, document_id)


def update_status(db: Session, doc: Document, status: int, error_message: str | None = None) -> None:
    doc.status = status
    doc.error_message = error_message
    db.flush()