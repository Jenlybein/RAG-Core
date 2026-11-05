"""检索业务：embed_query → similarity_search → 组装检索结果。

边界：不拼上下文、不调大模型、不生成答案，只返回切片 + 分数 + 出处。
user_id 骨架期由调用方传入但不落库（阶段 7 才接检索日志）。
"""

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding import get_embedding_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import knowledge_base_repository


async def retrieve(
    db: Session, *, tenant_id: str, kb_id: str, user_id: str, query: str
) -> dict:
    kb = knowledge_base_repository.get_by_id(db, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, "knowledge base not found")

    query_vector = await get_embedding_provider().embed_query(query)
    chunks = await get_vector_store().similarity_search(
        db, query_vector, tenant_id=tenant_id, kb_id=kb_id, top_k=settings.TOP_K
    )
    return {
        "query": query,
        "kb_id": kb_id,
        "retrieved_chunks": chunks,
        "metadata": {"top_k": settings.TOP_K, "vector_store": settings.VECTOR_STORE},
    }