"""pgvector 实现：用 SQLAlchemy 直接写 chunks 表。

score = 1 - 余弦距离，在 Python 侧换算成「越大越相关」；SQL 只按距离升序排。
写入只 flush，不 commit——事务由调用它的 Service 统一提交。
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.providers.vectorstores.base import VectorStore


class PgVectorStore(VectorStore):
    async def add_chunks(self, db: Session, chunks: list[dict]) -> None:
        db.add_all(
            [
                Chunk(
                    id=c["id"],
                    tenant_id=c["tenant_id"],
                    kb_id=c["kb_id"],
                    document_id=c["document_id"],
                    content=c["content"],
                    embedding=c["embedding"],
                    meta=c.get("meta", {}),
                )
                for c in chunks
            ]
        )
        db.flush()

    async def similarity_search(
        self, db: Session, query_vector: list[float], *, tenant_id: str, kb_id: str, top_k: int
    ) -> list[dict]:
        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.document_id,
                Chunk.content,
                Document.title,
                distance,
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.tenant_id == tenant_id, Chunk.kb_id == kb_id)
            .order_by(distance)
            .limit(top_k)
        )
        rows = db.execute(stmt).all()
        return [
            {
                "chunk_id": r.chunk_id,
                "document_id": r.document_id,
                "title": r.title,
                "content": r.content,
                "score": 1 - float(r.distance),
            }
            for r in rows
        ]

    async def delete_by_document_id(self, db: Session, document_id: str) -> None:
        db.execute(delete(Chunk).where(Chunk.document_id == document_id))
        db.flush()