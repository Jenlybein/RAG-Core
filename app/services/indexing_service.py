"""索引编排：切块 → 向量化 → 写库。

第一期同步调用，阶段 3 改成 Celery 异步时这个类的签名不变——骨架期就预留的
第一个扩展点。这里不 commit、不碰 HTTP，事务交给 DocumentService。
"""

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.providers.embedding import get_embedding_provider
from app.providers.vectorstores import get_vector_store
from app.utils.id_generator import new_chunk_id
from app.utils.text_splitter import CharacterTextSplitter

# 索引链路失败时回传给调用方的唯一原因文案：不含异常对象、堆栈、路径或 SQL。
# 失败回写由 T4 的 Celery 任务负责，它从这里 import；不要把异常原文拼进来。
INDEXING_FAILED_MESSAGE = "document indexing failed, please retry or contact support"


class IndexingService:
    def __init__(self) -> None:
        self._splitter = CharacterTextSplitter(settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        self._embeddings = get_embedding_provider()
        self._store = get_vector_store()

    async def index(self, db: Session, *, doc: Document, content: str) -> int:
        pieces = self._splitter.split(content)
        if not pieces:
            return 0
        texts = [piece.text for piece in pieces]
        vectors = await self._embeddings.embed_texts(texts)
        if len(vectors) != len(texts):
            raise ValueError(f"embedding count mismatch: {len(texts)} texts -> {len(vectors)} vectors")

        chunks: list[dict[str, Any]] = [
            {
                "id": new_chunk_id(),
                "tenant_id": doc.tenant_id,
                "kb_id": doc.kb_id,
                "document_id": doc.id,
                "content": piece.text,
                "embedding": vector,
                "meta": {**piece.metadata, "title": doc.title, "index": i},
            }
            for i, (piece, vector) in enumerate(zip(pieces, vectors))
        ]
        await self._store.add_chunks(db, chunks)
        return len(chunks)