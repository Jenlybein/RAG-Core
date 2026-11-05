"""上传文档接口：POST /api/v1/documents/upload（同步索引闭环）。

kb_id 放 body，不放路径参数。知识库不存在 / 跨租户统一返回 20008「不存在」。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.document import UploadDocumentData, UploadDocumentRequest
from app.services import document_service

router = APIRouter()


@router.post("/documents/upload", response_model=ApiResponse[UploadDocumentData], summary="上传文档并同步索引")
async def upload_document(
    payload: UploadDocumentRequest, db: Session = Depends(get_db)
) -> ApiResponse[UploadDocumentData]:
    data = await document_service.upload(
        db,
        tenant_id=payload.tenant_id,
        kb_id=payload.kb_id,
        title=payload.title,
        content=payload.content,
    )
    return ok(UploadDocumentData.model_validate(data))