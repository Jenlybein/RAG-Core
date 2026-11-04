"""向量库能力出口：抽象 + 工厂。业务层写 get_vector_store()。"""

from app.core.config import settings
from app.providers.vectorstores.base import VectorStore
from app.providers.vectorstores.pgvector import PgVectorStore


def get_vector_store() -> VectorStore:
    if settings.VECTOR_STORE == "pgvector":
        return PgVectorStore()
    raise ValueError(f"unknown vector store: {settings.VECTOR_STORE}")


__all__ = ["VectorStore", "get_vector_store"]