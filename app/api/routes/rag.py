"""检索接口：POST /api/v1/rag/retrieve。

只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
头部带占位 API Key，tenant_id 由服务端从 Key 解出，不进请求体。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import DevKeyContext, resolve_dev_key
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.rag import RetrieveData, RetrieveRequest
from app.services import rag_service

router = APIRouter()


@router.post("/rag/retrieve", response_model=ApiResponse[RetrieveData], summary="检索相关切片")
async def retrieve(
    payload: RetrieveRequest,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[RetrieveData]:
    data = await rag_service.retrieve(
        db,
        tenant_id=key.tenant_id,
        kb_ids=payload.kb_ids,
        query=payload.query,
        mode=payload.mode,
        top_k=payload.top_k,
        include_parent=payload.include_parent,
    )
    return ok(RetrieveData.model_validate(data))
