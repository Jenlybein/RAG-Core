"""检索接口：POST /api/v1/rag/retrieve。

只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ApiResponse, fail, ok
from app.schemas.rag import RetrieveData, RetrieveRequest
from app.services import rag_service

router = APIRouter()


@router.post("/rag/retrieve", response_model=ApiResponse[RetrieveData], summary="检索相关切片")
async def retrieve(
    payload: RetrieveRequest, db: Session = Depends(get_db)
) -> ApiResponse[RetrieveData]:
    data = await rag_service.retrieve(
        db,
        tenant_id=payload.tenant_id,
        kb_id=payload.kb_id,
        user_id=payload.user_id,
        query=payload.query,
    )
    if data is None:
        return fail(20008, "knowledge base not found")
    return ok(RetrieveData.model_validate(data))