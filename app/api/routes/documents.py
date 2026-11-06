"""上传文档接口：POST /api/v1/documents/upload（同步索引闭环）。

kb_id 放 body，不放路径参数。知识库不存在 / 跨租户统一返回 20008「不存在」。
头部带占位 API Key，tenant_id 由服务端从 Key 解出，不进请求体。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import DevKeyContext, resolve_dev_key
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.document import UploadDocumentData, UploadDocumentRequest
from app.services import document_service

router = APIRouter()


@router.post(
    "/documents/upload",
    response_model=ApiResponse[UploadDocumentData],
    summary="上传文档并同步索引",
)
async def upload_document(
    payload: UploadDocumentRequest,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[UploadDocumentData]:
    data = await document_service.upload(
        db,
        tenant_id=key.tenant_id,
        kb_id=payload.kb_id,
        title=payload.title,
        content=payload.content,
    )
    return ok(UploadDocumentData.model_validate(data))
