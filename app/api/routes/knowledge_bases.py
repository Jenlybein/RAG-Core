"""建知识库 / 列知识库接口。

API 层只做入参出参，业务在 KnowledgeBaseService / Repository。
头部带占位 API Key，tenant_id 由服务端从 Key 解出，不进请求体。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import DevKeyContext, resolve_dev_key
from app.db.session import get_db
from app.models.knowledge_base import settings_of
from app.repositories import knowledge_base_repository
from app.schemas.common import ApiResponse, ok
from app.schemas.knowledge_base import (
    CreateKBData,
    CreateKBRequest,
    KnowledgeBaseItem,
    KnowledgeBaseListData,
)
from app.services import knowledge_base_service

router = APIRouter()


@router.post(
    "/knowledge-bases/create",
    response_model=ApiResponse[CreateKBData],
    summary="创建知识库",
)
async def create_knowledge_base(
    payload: CreateKBRequest,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[CreateKBData]:
    data = await knowledge_base_service.create_kb(
        db,
        name=payload.name,
        description=payload.description,
        tenant_id=key.tenant_id,
        split_strategy=payload.split_strategy,
        chunk_size=payload.chunk_size,
    )
    return ok(CreateKBData.model_validate(data))


@router.get(
    "/knowledge-bases",
    response_model=ApiResponse[KnowledgeBaseListData],
    summary="列这把 Key 可见的知识库",
)
async def list_knowledge_bases(
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[KnowledgeBaseListData]:
    rows = knowledge_base_repository.list_by_tenant(db, key.tenant_id)
    data = KnowledgeBaseListData(
        knowledge_bases=[
            KnowledgeBaseItem(
                kb_id=row.id,
                name=row.name,
                tenant_id=row.tenant_id,
                split_strategy=row.split_strategy,
                chunk_size=settings_of(row)["chunk_size"],
            )
            for row in rows
        ]
    )
    return ok(data)
