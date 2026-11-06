"""文档接口：异步上传 + 状态查询。

只有一个模块、一个 router：
  * ``POST /documents/upload`` —— 单端点按 content-type 分流（multipart = 文件模式，
    其余 = JSON 模式）。只登记 + 投递索引任务，立即返回 ``status=3``。
  * ``GET /documents?kb_id=`` —— 该库下的文档列表，异步化后调用方唯一的进度观测口。

上传的两条分支共用 ``document_service.upload``，请求线程不做解析 / 向量化。
"""

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.auth import DevKeyContext, resolve_dev_key
from app.db.session import get_db
from app.repositories import chunk_repository, document_repository
from app.schemas.common import ApiResponse, ok
from app.schemas.document import (
    DocumentListData,
    DocumentListItem,
    UploadDocumentData,
    UploadDocumentRequest,
)
from app.services import document_service

router = APIRouter()

_MISSING_KB_ID_ERROR = {"type": "missing", "loc": ("body", "kb_id"), "msg": "Field required"}
_MISSING_FILE_ERROR = {"type": "missing", "loc": ("body", "file"), "msg": "Field required"}
_INVALID_JSON_ERROR = {"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error"}


def _submitted_request_id(request: Request) -> str | None:
    """关联标识来自中间件写进 scope 的同一个值，不直接读 structlog contextvars。"""

    stored = request.scope.get("state", {}).get("request_id")
    return stored if isinstance(stored, str) else None


@router.post(
    "/documents/upload",
    response_model=ApiResponse[UploadDocumentData],
    summary="上传文档并异步索引",
)
async def upload_document(
    request: Request,
    kb_id: str | None = Form(None),
    title: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[UploadDocumentData]:
    """单端点按 content-type 分流：multipart 走文件模式，其余走 JSON 模式。

    FastAPI 不能在同一次操作里既声明 Pydantic body 又声明 Form/File，因此这里声明
    只有 Form/File，JSON 分支手工 ``model_validate(await request.json())``。表单解析
    对非 multipart 的 content-type 不读 body，所以 JSON 分支之后仍能取到完整正文。
    """

    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        if kb_id is None or not kb_id.strip():
            raise RequestValidationError([_MISSING_KB_ID_ERROR])
        if file is None:
            raise RequestValidationError([_MISSING_FILE_ERROR])
        data = await document_service.upload(
            db,
            tenant_id=key.tenant_id,
            kb_id=kb_id,
            title=title,
            filename=file.filename or "",
            data=await file.read(),
            request_id=_submitted_request_id(request),
        )
        return ok(UploadDocumentData.model_validate(data))

    try:
        payload = UploadDocumentRequest.model_validate(await request.json())
    except ValidationError as exc:
        # 交给既有的 RequestValidationError 处理器：20002 + 只挑 loc/msg/type，
        # 不回显 input / ctx / url。
        raise RequestValidationError(exc.errors()) from None
    except ValueError:
        # 半截 / 非法 JSON 也归入 20002，不要让它穿成未捕获异常的 500。
        raise RequestValidationError([_INVALID_JSON_ERROR]) from None

    data = await document_service.upload(
        db,
        tenant_id=key.tenant_id,
        kb_id=payload.kb_id,
        title=payload.title,
        content=payload.content,
        request_id=_submitted_request_id(request),
    )
    return ok(UploadDocumentData.model_validate(data))


@router.get(
    "/documents",
    response_model=ApiResponse[DocumentListData],
    summary="列知识库下的文档（轮询异步进度）",
)
async def list_documents(
    kb_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(resolve_dev_key),
) -> ApiResponse[DocumentListData]:
    """返回该库下全部文档的 status / chunk_count / error_message 等摘要。

    ``chunk_count`` 用一次分组计数批量取回，不对每份文档各查一次（N+1）。
    """

    rows = document_repository.list_by_kb(db, kb_id)
    counts = chunk_repository.count_by_document_ids(db, [row.id for row in rows])
    data = DocumentListData(
        items=[
            DocumentListItem(
                document_id=row.id,
                title=row.title,
                status=row.status,
                chunk_count=counts.get(row.id, 0),
                error_message=row.error_message,
                source_type=row.source_type,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
    return ok(data)
