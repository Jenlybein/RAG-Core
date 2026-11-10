"""控制面「管库管文档」接口（``/console``）：树、建库、删空库、上传、重建、删除。

与 :mod:`app.api.routes.console`（登录 / 套餐 / 账号 / 审计）**分成两个路由模块**，
各自 ``include_router``：并行票共用同一个文件会让每一票的 diff 互相重叠；本模块只带
``prefix="/console"``，不重复那一条。

鉴权分两档，写死在依赖里而不是每条各写一遍：

- 读（树）用 ``require_console_tenant``：会话 cookie + 租户账号；
- 写（其余五条）用 ``require_console_tenant_write``：它内含防跨站头与租户判定，
  所以**未登录仍是 20010**（该去登录），登录了但缺头 / 管理员仍是 **20011**
  （不该被前端当成掉线清 cookie）。

判定、顺序与错误码全在 :mod:`app.services.console_kb_service`；这一层只取参数、调服务、
装响应壳。上传是单端点按 content-type 分流（multipart = 文件模式，其余 = JSON 模式），
与数据面完全同构；FastAPI 不能在同一次操作里既声明 Pydantic body 又声明 Form/File，
所以这里声明只有 Form/File，JSON 分支手工 ``model_validate(await request.json())``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.console_deps import (
    get_console_account,
    get_plan_cache,
    require_console_tenant,
    require_console_tenant_write,
)
from app.core.console_auth import ConsoleIdentity
from app.core.plan_cache import PlanCache
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.console_kbs import (
    ConsoleUploadDocumentRequest,
    DeleteKnowledgeBaseData,
    KnowledgeBaseNode,
    KnowledgeBaseTreeData,
)
from app.schemas.document import (
    DeleteDocumentData,
    DocumentListItem,
    ReindexDocumentData,
    UploadDocumentData,
)
from app.schemas.knowledge_base import CreateKBData, CreateKBRequest
from app.services import console_kb_service

router = APIRouter(prefix="/console", tags=["console"])

_MISSING_FILE_ERROR = {"type": "missing", "loc": ("body", "file"), "msg": "Field required"}
_INVALID_JSON_ERROR = {"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error"}


def _submitted_request_id(request: Request) -> str | None:
    """关联标识来自中间件写进 scope 的同一个值，不直接读 structlog contextvars。

    与数据面同名函数逐字一致：控制面与数据面的上传 / 重建落到同一个 Worker 时，
    日志里的 request_id 必须是同一种来源，否则「一次上传在两边对不上号」。
    """

    stored = request.scope.get("state", {}).get("request_id")
    return stored if isinstance(stored, str) else None


def _node_data(node: console_kb_service.TreeKnowledgeBase) -> KnowledgeBaseNode:
    """服务层的树节点 → 响应模型；文档项复用数据面的 ``DocumentListItem``。"""

    return KnowledgeBaseNode(
        kb_id=node.kb_id,
        name=node.name,
        split_strategy=node.split_strategy,
        allowed_formats=node.allowed_formats,
        chunk_size=node.chunk_size,
        documents=[
            DocumentListItem(
                document_id=document.document_id,
                title=document.title,
                status=document.status,
                chunk_count=document.chunk_count,
                error_message=document.error_message,
                source_type=document.source_type,
                created_at=document.created_at,
            )
            for document in node.documents
        ],
    )


@router.get(
    "/knowledge-bases/tree",
    response_model=ApiResponse[KnowledgeBaseTreeData],
    summary="一次拿全本租户的知识库与文档状态",
)
def knowledge_base_tree(
    tenant_id: str = Depends(require_console_tenant),
    db: Session = Depends(get_db),
) -> ApiResponse[KnowledgeBaseTreeData]:
    """控制面列表页唯一的读接口：不做白名单收窄，空库的 ``documents`` 是 ``[]``。"""

    nodes = console_kb_service.list_tree(db, tenant_id=tenant_id)
    return ok(KnowledgeBaseTreeData(knowledge_bases=[_node_data(node) for node in nodes]))


@router.post(
    "/knowledge-bases",
    response_model=ApiResponse[CreateKBData],
    summary="建知识库（面板身份，含套餐静态配额）",
)
async def create_knowledge_base(
    payload: CreateKBRequest,
    tenant_id: str = Depends(require_console_tenant_write),
    identity: ConsoleIdentity = Depends(get_console_account),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
) -> ApiResponse[CreateKBData]:
    """建库：租户只来自会话；套餐校验与建库规则复用 ``knowledge_base_service``。"""

    data = await console_kb_service.create_kb(
        db,
        tenant_id=tenant_id,
        actor=identity,
        cache=cache,
        name=payload.name,
        description=payload.description,
        split_strategy=payload.split_strategy,
        chunk_size=payload.chunk_size,
        allowed_formats=payload.allowed_formats,
    )
    return ok(CreateKBData.model_validate(data))


@router.delete(
    "/knowledge-bases/{kb_id}",
    response_model=ApiResponse[DeleteKnowledgeBaseData],
    summary="删知识库（仅空库；库内仍有文档先删文档）",
)
def delete_knowledge_base(
    kb_id: str = Path(..., min_length=1),
    tenant_id: str = Depends(require_console_tenant_write),
    identity: ConsoleIdentity = Depends(get_console_account),
    db: Session = Depends(get_db),
) -> ApiResponse[DeleteKnowledgeBaseData]:
    """删空库：非空 ``20015``、缺失/跨租户 ``20008``；不做级联删除。"""

    data = console_kb_service.delete_empty_kb(db, tenant_id=tenant_id, kb_id=kb_id, actor=identity)
    return ok(DeleteKnowledgeBaseData.model_validate(data))


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=ApiResponse[UploadDocumentData],
    summary="上传文档并异步索引（multipart / JSON 单端点分流）",
)
async def upload_document(
    request: Request,
    kb_id: str = Path(..., min_length=1),
    title: str | None = Form(None),
    file: UploadFile | None = File(None),
    tenant_id: str = Depends(require_console_tenant_write),
    identity: ConsoleIdentity = Depends(get_console_account),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
) -> ApiResponse[UploadDocumentData]:
    """单端点按 content-type 分流；登记与投递复用 ``document_service.upload``。

    表单解析对非 multipart 的 content-type 不读 body，所以 JSON 分支之后仍能取到
    完整正文（与数据面同一段说明）。缺 ``file`` 手工补 20002；半截/非法 JSON 也归入
    20002，不让它穿成未捕获异常的 500。
    """

    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        if file is None:
            raise RequestValidationError([_MISSING_FILE_ERROR])
        data = await console_kb_service.upload_document(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            actor=identity,
            cache=cache,
            title=title,
            filename=file.filename or "",
            data=await file.read(),
            request_id=_submitted_request_id(request),
        )
        return ok(UploadDocumentData.model_validate(data))

    try:
        payload = ConsoleUploadDocumentRequest.model_validate(await request.json())
    except ValidationError as exc:
        # 交给既有的 RequestValidationError 处理器：20002 + 只挑 loc/msg/type。
        raise RequestValidationError(exc.errors()) from None
    except ValueError:
        raise RequestValidationError([_INVALID_JSON_ERROR]) from None

    data = await console_kb_service.upload_document(
        db,
        tenant_id=tenant_id,
        kb_id=kb_id,
        actor=identity,
        cache=cache,
        title=payload.title,
        content=payload.content,
        request_id=_submitted_request_id(request),
    )
    return ok(UploadDocumentData.model_validate(data))


@router.post(
    "/documents/{document_id}/reindex",
    response_model=ApiResponse[ReindexDocumentData],
    summary="重建单文档索引（可重解析原始文件、可强制重建成功文档）",
)
def reindex_document(
    request: Request,
    document_id: str = Path(..., min_length=1),
    reparse: bool = Query(False),
    force: bool = Query(False),
    tenant_id: str = Depends(require_console_tenant_write),
    identity: ConsoleIdentity = Depends(get_console_account),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
) -> ApiResponse[ReindexDocumentData]:
    """守卫 → 准备入队（不盖租约）→ 审计 + 提交 → 投递，立即回 ``status=3``。"""

    data = console_kb_service.reindex_document(
        db,
        tenant_id=tenant_id,
        document_id=document_id,
        reparse=reparse,
        force=force,
        actor=identity,
        cache=cache,
        request_id=_submitted_request_id(request),
    )
    return ok(ReindexDocumentData.model_validate(data))


@router.delete(
    "/documents/{document_id}",
    response_model=ApiResponse[DeleteDocumentData],
    summary="删除单文档（清切片、删原文件、删记录，顺序不变）",
)
async def delete_document(
    document_id: str = Path(..., min_length=1),
    tenant_id: str = Depends(require_console_tenant_write),
    identity: ConsoleIdentity = Depends(get_console_account),
    db: Session = Depends(get_db),
) -> ApiResponse[DeleteDocumentData]:
    """purge 切片 → 删原文件 → 删行 → 审计 → 提交；有效处理中拒绝（20025）。"""

    data = await console_kb_service.delete_document(
        db, tenant_id=tenant_id, document_id=document_id, actor=identity
    )
    return ok(DeleteDocumentData.model_validate(data))
