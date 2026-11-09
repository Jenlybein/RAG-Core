"""文档接口：异步上传、状态查询、重建索引与删除。

只有一个模块、一个 router：
  * ``POST /documents/upload`` —— 单端点按 content-type 分流（multipart = 文件模式，
    其余 = JSON 模式）。只登记 + 投递索引任务，立即返回 ``status=3``。
  * ``GET /documents?kb_id=`` —— 该知识库下的文档列表，异步化后调用方唯一的进度观测口。
  * ``POST /documents/{document_id}/reindex?reparse=&force=`` —— 单文档重建：守卫 →
    标记排队（不盖租约）→ 提交 → 投递；purge、抢占与写入仍只在 Worker 的事务里发生。
  * ``DELETE /documents/{document_id}`` —— 守卫通过后按租户清除切片、删原文件、删记录。

按 ``document_id`` 取文档一律经 ``get_by_id_for_tenant``（ADR-0006）：缺失与跨租户
统一按「不存在」（20016）处理，不泄露存在性。守卫一律走 ``document_state`` 的
``ensure_reindexable`` / ``ensure_deletable``，不在路由里另写 if。

四个入口都先过**第二道关卡**（数据范围白名单，``app.services.data_scope``）：上传与
列文档按请求里的 ``kb_id`` 判，重建与删除按文档所属的库判。越界与跨租户同一个码
（20008），理由与 ``ensure_visible`` 相同——可区分的拒绝会泄露资源存在性。
"""

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.core.exceptions import AppError, ErrorCode
from app.db.session import get_db
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import chunk_repository, document_repository
from app.schemas.common import ApiResponse, ok
from app.schemas.document import (
    DeleteDocumentData,
    DocumentListData,
    DocumentListItem,
    ReindexDocumentData,
    UploadDocumentData,
    UploadDocumentRequest,
)
from app.services import document_service, document_state
from app.services.chunk_store import build_chunk_stores
from app.services.data_scope import ensure_kb_scope
from app.services.document_storage import remove_document_file
from app.services.knowledge_base_access import ensure_visible
from app.services.index_dispatch import enqueue_document
from app.services.indexing_service import purge_document_chunks
from app.utils.status import DocumentStatus

router = APIRouter()

logger = structlog.get_logger(__name__)

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
    key: DevKeyContext = Depends(get_current_key),
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
        # 第二道关卡：白名单。FastAPI 在进本函数之前已经解析完 multipart，这里能拦下的
        # 最早位置就是「读正文 / 落库」之前——被拒的上传不写行、也不把字节读进来。
        ensure_kb_scope(policy=key.policy, kb_ids=[kb_id])
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

    # 第二道关卡：白名单（JSON 分支与文件分支同一份判定，同一个码）。
    ensure_kb_scope(policy=key.policy, kb_ids=[payload.kb_id])

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
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[DocumentListData]:
    """返回该知识库下全部文档的 status / chunk_count / error_message 等摘要。

    知识库不存在或不属于调用方租户，一律按「不存在」（20008）处理，不泄露存在性；
    列表查询也强制带 ``tenant_id``，绝不返回其他租户的行。
    ``chunk_count`` 用一次分组计数批量取回，不对每份文档各查一次（N+1）。
    """

    # 第二道关卡：白名单（先于任何库行查询），然后才是租户可见性。
    ensure_kb_scope(policy=key.policy, kb_ids=[kb_id])
    ensure_visible(db, tenant_id=key.tenant_id, kb_id=kb_id)

    rows = document_repository.list_by_kb(db, kb_id, tenant_id=key.tenant_id)
    counts = chunk_repository.count_by_document_ids(
        db, [row.id for row in rows], tenant_id=key.tenant_id
    )
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


@router.post(
    "/documents/{document_id}/reindex",
    response_model=ApiResponse[ReindexDocumentData],
    summary="重建单文档索引（可强制重解析原始文件）",
)
async def reindex_document(
    document_id: str,
    request: Request,
    reparse: bool = Query(False),
    force: bool = Query(False),
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[ReindexDocumentData]:
    """守卫 → 标记排队（不盖租约）→ 提交 → 投递，立即返回 ``status=3``。

    顺序是契约的一部分：先按 id + 租户取文档（缺失 / 跨租户统一 20016），再走
    ``ensure_reindexable``（有效处理中 20025；成功且未 ``force`` 20027），然后
    ``prepare_for_indexing`` 把可重建的行置成「已排队、未开工」
    （``index_claimed_at=NULL``；不在这里盖租约）。准备失败说明这一行已被一个
    有效租约占住（守卫看的是旧快照），可能刚好有 Worker 抢先；仍按 20025 回绝。
    提交后把任务交给 Worker；
    **Worker 是唯一的租约抢占者**（``try_claim_for_indexing``），它进来时才盖
    ``index_claimed_at``。若路由自己先盖租约，Worker 会因租约新鲜而拒绝执行，
    重建就变成静默 no-op。

    **这里绝不 purge 切片**：purge 与写入必须在 Worker 的同一段事务里完成，
    否则会有一段「旧切片没了、新切片还没写」的空窗。``reparse`` 只是透传给
    Worker 的选择位，表示忽略库内 ``content``、重解析落盘原始文件。
    """

    doc = document_repository.get_by_id_for_tenant(db, document_id, tenant_id=key.tenant_id)
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 第二道关卡：白名单。这条入口按 ``document_id`` 进来，因此范围要看**它所属的库**；
    # 取文档那一句已经强制带了租户，范围判定紧接着做，之后才轮到状态守卫。
    ensure_kb_scope(policy=key.policy, kb_ids=[doc.kb_id])

    document_state.ensure_reindexable(doc, force=force)

    prepared = document_repository.prepare_for_indexing(
        db, document_id, tenant_id=key.tenant_id, allow_success=force
    )
    if not prepared:
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)
    db.commit()

    enqueue_document(
        document_id=document_id,
        tenant_id=key.tenant_id,
        kb_id=doc.kb_id,
        request_id=_submitted_request_id(request),
        reparse=reparse,
    )
    logger.info(
        "document reindex accepted",
        document_id=document_id,
        tenant_id=key.tenant_id,
        kb_id=doc.kb_id,
        reparse=reparse,
        force=force,
    )
    return ok(
        ReindexDocumentData(
            document_id=document_id,
            status=DocumentStatus.PROCESSING,
            chunk_count=0,
            reparse=reparse,
        )
    )


@router.delete(
    "/documents/{document_id}",
    response_model=ApiResponse[DeleteDocumentData],
    summary="删除单文档（清切片、删原文件、删记录）",
)
async def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[DeleteDocumentData]:
    """守卫通过后按固定顺序删除：清除切片 → 删原文件 → 删行 → 提交。

    ``purge_document_chunks`` 遍历存储列表（向量存储 + 关键词检索，接线见
    ``app.services.chunk_store.build_chunk_stores``），因此删文档不需要知道存储
    形态；删除切片同样强制带租户（ADR-0006）。有效处理中（租约未过期）拒绝删除
    （20025）；租约过期视为 Worker 卡死，允许删除。
    """

    doc = document_repository.get_by_id_for_tenant(db, document_id, tenant_id=key.tenant_id)
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 第二道关卡：白名单（同重建：按文档进来，范围看它所属的库）。
    ensure_kb_scope(policy=key.policy, kb_ids=[doc.kb_id])

    document_state.ensure_deletable(doc)

    await purge_document_chunks(
        db,
        document_id,
        tenant_id=key.tenant_id,
        stores=build_chunk_stores(get_vector_store(), get_keyword_search_provider()),
    )
    remove_document_file(doc.source_file_path)
    db.delete(doc)
    db.commit()

    logger.info(
        "document deleted",
        document_id=document_id,
        tenant_id=key.tenant_id,
        kb_id=doc.kb_id,
        source_type=doc.source_type,
    )
    return ok(DeleteDocumentData(document_id=document_id, deleted=True))
