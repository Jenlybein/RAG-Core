"""Embedding 能力出口：抽象 + 工厂。业务层写 get_embedding_provider()。"""

from app.core.config import settings
from app.providers.embedding.base import EmbeddingProvider
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding


def get_embedding_provider() -> EmbeddingProvider:
    return OpenAICompatibleEmbedding(
        base_url=settings.MODEL_BASE_URL,
        api_key=settings.MODEL_API_KEY,
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
    )


__all__ = ["EmbeddingProvider", "get_embedding_provider"]