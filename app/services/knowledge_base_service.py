"""建库业务：生成 kb_id → 落库 → 提交。

Service 是全链路唯一 commit 的地方；Repository 与 Provider 都不提交。
"""

from sqlalchemy.orm import Session

from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MERGE_THRESHOLD,
    SPLIT_STRUCTURE,
    KnowledgeBase,
    settings_of,
)
from app.repositories import knowledge_base_repository
from app.utils.id_generator import new_kb_id


async def create_kb(
    db: Session,
    *,
    name: str,
    description: str | None,
    tenant_id: str,
    split_strategy: str = SPLIT_STRUCTURE,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    merge_threshold: int = DEFAULT_MERGE_THRESHOLD,
) -> dict:
    kb = KnowledgeBase(
        id=new_kb_id(),
        tenant_id=tenant_id,
        name=name,
        description=description,
        split_strategy=split_strategy,
        settings={"chunk_size": chunk_size, "merge_threshold": merge_threshold},
    )
    knowledge_base_repository.create(db, kb)
    db.commit()
    values = settings_of(kb)
    return {
        "kb_id": kb.id,
        "name": kb.name,
        "tenant_id": kb.tenant_id,
        "split_strategy": kb.split_strategy,
        "chunk_size": values["chunk_size"],
        "merge_threshold": values["merge_threshold"],
        "created_at": kb.created_at,
    }
