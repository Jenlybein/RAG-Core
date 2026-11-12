"""控制面「管库管文档」接口（``/console``）：树、建库、删空库、上传、重建、删除。

与 :mod:`app.api.routes.console`（登录 / 套餐 / 账号 / 审计）**分成两个路由模块**，
各自 ``include_router``：并行票共用同一个文件会让每一票的 diff 互相重叠；本模块只带
``prefix="/console"``，不重复那一条。

鉴权分两档，写死在依赖里而不是每条各写一遍：

- 读（树）用 ``require_console_tenant``：会话 cookie + 租户账号；
- 写（其余五条）用 ``require_console_tenant_write``：它内含防跨站头与租户判定，
  所以**未登录仍是 20010**（该去登录），登录了但缺头 / 管理员仍是 **20011**
  （不该被前端当成掉线清 cookie）。

两个依赖都返回**已校验的** ``ConsoleIdentity``；``tenant_id`` 从它经
:func:`app.api.console_deps.tenant_id_of` 取（只做类型收窄），``actor`` 直接用同一个对象
——审计要的是它的 ``account_id``，而 FastAPI 的依赖缓存保证身份只解析一次。

判定、顺序与错误码全在 :mod:`app.services.console_kb_service`；这一层只取参数、调服务、
装响应壳。上传是单端点按 content-type 分流（multipart = 文件模式，其余 = JSON 模式），
与数据面完全同构；分流与错误体收在 :mod:`app.api.upload_parsing`（#82 修复前两个路由
各写一遍），这里只声明 Form/File 并把解析结果整份交给服务层。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.console_deps import (
    get_plan_cache,
    require_console_tenant,
    require_console_tenant_write,
    tenant_id_of,
)
from app.api.upload_parsing import parse_upload_request, submitted_request_id
from app.core.console_auth import ConsoleIdentity
from app.core.plan_cache import PlanCache
from app.db.session import get_db, get_transaction
from app.db.transaction import Transaction
from app.schemas.common import ApiResponse, ok
from app.schemas.console_kbs import (
    ConsoleUploadDocumentRequest,
    DeleteKnowledgeBaseData,
    DocumentContentData,
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
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: Session = Depends(get_db),
) -> ApiResponse[KnowledgeBaseTreeData]:
    """控制面列表页唯一的读接口：不做白名单收窄，空库的 ``documents`` 是 ``[]``。"""

    nodes = console_kb_service.list_tree(db, tenant_id=tenant_id_of(identity))
    return ok(KnowledgeBaseTreeData(knowledge_bases=[_node_data(node) for node in nodes]))


@router.post(
    "/knowledge-bases",
    response_model=ApiResponse[CreateKBData],
    summary="建知识库（面板身份，含套餐静态配额）",
)
async def create_knowledge_base(
    payload: CreateKBRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[CreateKBData]:
    """建库：租户只来自会话；套餐校验与建库规则复用 ``knowledge_base_service``。

    建库行与它那条审计由 ``tx`` 一笔提交：审计写不进去时库也不会留下。
    """

    data = await console_kb_service.create_kb(
        db,
        tenant_id=tenant_id_of(identity),
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
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: Session = Depends(get_db),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[DeleteKnowledgeBaseData]:
    """删空库：非空 ``20015``、缺失/跨租户 ``20008``；不做级联删除。"""

    data = console_kb_service.delete_empty_kb(
        db, tenant_id=tenant_id_of(identity), kb_id=kb_id, actor=identity
    )
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
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[UploadDocumentData]:
    """单端点按 content-type 分流；登记与投递复用 ``document_service.upload``。

    分流与错误体在 :mod:`app.api.upload_parsing`（与数据面共用同一段实现）；这里只把
    解析结果整份交给服务层，``kb_id`` 来自路径而不是表单。登记行与账本行由 ``tx``
    一笔提交，投递登记在这笔提交之后。
    """

    upload = await parse_upload_request(
        request,
        kb_id_field=None,
        title_field=title,
        file=file,
        json_model=ConsoleUploadDocumentRequest,
    )

    data = await console_kb_service.upload_document(
        db,
        tenant_id=tenant_id_of(identity),
        kb_id=kb_id,
        actor=identity,
        cache=cache,
        title=upload.title,
        filename=upload.filename,
        data=upload.data,
        content=upload.content,
        request_id=upload.request_id,
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
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: Session = Depends(get_db),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[ReindexDocumentData]:
    """守卫 → 准备入队（不盖租约）→ 审计（同一笔事务）→ 投递，立即回 ``status=3``。"""

    data = console_kb_service.reindex_document(
        db,
        tenant_id=tenant_id_of(identity),
        document_id=document_id,
        reparse=reparse,
        force=force,
        actor=identity,
        cache=cache,
        request_id=submitted_request_id(request),
    )
    return ok(ReindexDocumentData.model_validate(data))


@router.delete(
    "/documents/{document_id}",
    response_model=ApiResponse[DeleteDocumentData],
    summary="删除单文档（清切片、删原文件、删记录，顺序不变）",
)
async def delete_document(
    document_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: Session = Depends(get_db),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[DeleteDocumentData]:
    """purge 切片 → 删原文件 → 删行 → 审计 → 由 ``tx`` 提交；有效处理中拒绝（20025）。"""

    data = await console_kb_service.delete_document(
        db, tenant_id=tenant_id_of(identity), document_id=document_id, actor=identity
    )
    return ok(DeleteDocumentData.model_validate(data))


@router.get(
    "/documents/{document_id}/content",
    response_model=ApiResponse[DocumentContentData],
    summary="读一份文档的原文（只读；文件模式的正文现场解析，与切块同源）",
)
def read_document_content(
    document_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: Session = Depends(get_db),
) -> ApiResponse[DocumentContentData]:
    """走**读门**（`require_console_tenant`）而不是写门：看原文不改任何状态，也不该要求
    `x-requested-with`——那个头是给写操作防跨站用的，加在读接口上只会让「查看」多一步
    无意义的配合。跨租户与不存在同为 20016。
    """

    data = console_kb_service.read_document_content(
        db, tenant_id=tenant_id_of(identity), document_id=document_id
    )
    return ok(DocumentContentData.model_validate(data))
