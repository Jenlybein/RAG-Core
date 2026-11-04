"""建知识库接口：POST /api/v1/knowledge-bases/create。

API 层只做入参出参，业务在 KnowledgeBaseService。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.knowledge_base import CreateKBData, CreateKBRequest
from app.services import knowledge_base_service

router = APIRouter()


@router.post("/knowledge-bases/create", response_model=ApiResponse[CreateKBData], summary="创建知识库")
async def create_knowledge_base(
    payload: CreateKBRequest, db: Session = Depends(get_db)
) -> ApiResponse[CreateKBData]:
    data = await knowledge_base_service.create_kb(
        db, name=payload.name, description=payload.description, tenant_id=payload.tenant_id
    )
    return ok(CreateKBData.model_validate(data))