"""索引编排：切块 → 向量化 → 写库。

第一期同步调用，阶段 3 改成 Celery 异步时这个类的签名不变——骨架期就预留的
第一个扩展点。这里不 commit、不碰 HTTP，事务交给 DocumentService。
"""

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.providers.embedding import get_embedding_provider
from app.providers.vectorstores import get_vector_store
from app.utils.id_generator import new_chunk_id
from app.utils.text_splitter import CharacterTextSplitter


class IndexingService:
    def __init__(self) -> None:
        self._splitter = CharacterTextSplitter(settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        self._embeddings = get_embedding_provider()
        self._store = get_vector_store()

    async def index(self, db: Session, *, doc: Document, content: str) -> int:
        texts = self._splitter.split(content)
        if not texts:
            return 0
        vectors = await self._embeddings.embed_texts(texts)
        if len(vectors) != len(texts):
            raise ValueError(f"embedding count mismatch: {len(texts)} texts -> {len(vectors)} vectors")

        chunks = [
            {
                "id": new_chunk_id(),
                "tenant_id": doc.tenant_id,
                "kb_id": doc.kb_id,
                "document_id": doc.id,
                "content": text,
                "embedding": vector,
                "meta": {"title": doc.title, "index": i},
            }
            for i, (text, vector) in enumerate(zip(texts, vectors))
        ]
        await self._store.add_chunks(db, chunks)
        return len(chunks)