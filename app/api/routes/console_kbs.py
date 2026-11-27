"""控制面「管库管文档」接口（``/console``）：树、建库、删空库、上传、重建、删除、
版本列表与回滚。

与 :mod:`app.api.routes.console`（登录 / 套餐 / 账号 / 审计）**分成两个路由模块**，
各自 ``include_router``：并行票共用同一个文件会让每一票的 diff 互相重叠；本模块只带
``prefix="/console"``，不重复那一条。

鉴权分两档，写死在依赖里而不是每条各写一遍：

- 读（树、原文、版本列表）用 ``require_console_tenant``：会话 cookie + 租户账号；
- 写（其余六条）用 ``require_console_tenant_write``：它内含防跨站头与租户判定，
  所以**未登录仍是 20010**（该去登录），登录了但缺头 / 管理员仍是 **20011**
  （不该被前端当成掉线清 cookie）。

两个依赖都返回**已校验的** ``ConsoleIdentity``；``tenant_id`` 从它经
:func:`app.api.console_deps.tenant_id_of` 取（只做类型收窄），``actor`` 直接用同一个对象
——审计要的是它的 ``account_id``，而 FastAPI 的依赖缓存保证身份只解析一次。

判定、顺序与错误码全在 :mod:`app.services.console_kb_service`；这一层只取参数、调服务、
装响应壳。上传是单端点按 content-type 分流（multipart = 文件模式，其余 = JSON 模式），
与数据面完全同构；分流与错误体收在 :mod:`app.api.upload_parsing`（#82 修复前两个路由
各写一遍），这里只声明 Form/File 并把解析结果整份交给服务层。

**版本列表与回滚（#177）换的是门，不是契约**：出参与数据面那两条版本接口同一个形状
（``DocumentVersionListData`` / ``RollbackDocumentVersionData``），业务进的是同一个
``document_version_service``。控制面在这里的价值是「不写 curl 也能看与点」，而不是
第二份实现。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import (
    get_plan_cache,
    require_console_tenant,
    require_console_tenant_write,
    tenant_id_of,
)
from app.api.upload_parsing import parse_upload_request, submitted_request_id
from app.core.console_auth import ConsoleIdentity
from app.core.plan_cache import PlanCache
from app.db.async_session import get_async_db, get_async_transaction
from app.db.transaction import AsyncTransaction
from app.schemas.common import ApiResponse, ok
from app.schemas.console_kbs import (
    ConsoleUploadDocumentRequest,
    DeleteKnowledgeBaseData,
    DocumentContentData,
    KnowledgeBaseNode,
    KbBuildData,
    KbDocumentsData,
    KnowledgeBaseListData,
    KnowledgeBaseTreeData,
    RebuildKBData,
    UpdateKBSettingsData,
    UpdateKBSettingsRequest,
)
from app.schemas.console_kbs import KBSettingsPreviewData
from app.schemas.document import (
    DeleteDocumentData,
    DocumentListItem,
    DocumentVersionListData,
    ReindexDocumentData,
    RollbackDocumentVersionData,
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
        section_part_max_chars=node.section_part_max_chars,
        needs_rebuild=node.needs_rebuild,
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
async def knowledge_base_tree(
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KnowledgeBaseTreeData]:
    """控制面列表页唯一的读接口：不做白名单收窄，空库的 ``documents`` 是 ``[]``。"""

    nodes = await console_kb_service.list_tree(db, tenant_id=tenant_id_of(identity))
    return ok(KnowledgeBaseTreeData(knowledge_bases=[_node_data(node) for node in nodes]))


@router.get(
    "/knowledge-bases",
    response_model=ApiResponse[KnowledgeBaseListData],
    summary="库列表（摘要：四个数字 + 需重建 + 构建状态，不含文档）",
)
async def list_knowledge_bases(
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KnowledgeBaseListData]:
    """卡片网格的那一份数据。

    与 ``/knowledge-bases/tree`` 的差别只有一条：**不回文档数组**。卡片只显示四个数字，
    为一个 5000 篇的库把 5000 行读出来再丢掉，是「列表与详情被迫共用一份数据」的根因
    ——那种耦合让两边都没法有合适的刷新节奏。
    """

    data = await console_kb_service.list_summaries(db, tenant_id=tenant_id_of(identity))
    return ok(KnowledgeBaseListData.model_validate(data))


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=ApiResponse[KbDocumentsData],
    summary="详情页一页：库摘要 + 构建摘要 + 文档（含「本次构建」第二根轴）",
)
async def list_knowledge_base_documents(
    kb_id: str = Path(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(
        console_kb_service.DEFAULT_DOCUMENTS_PAGE_SIZE,
        ge=1,
        le=console_kb_service.MAX_DOCUMENTS_PAGE_SIZE,
    ),
    status: int | None = Query(None, ge=1, le=4),
    keyword: str | None = Query(None, max_length=200),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KbDocumentsData]:
    """详情弹窗**打开即取**的那一份：分页、可筛状态与标题。

    这是「进详情要不要按 F5」的正面回答：它有自己的请求，所以永远是打开那一刻的数据；
    响应带 ``generated_at``，界面据此显示「数据更新于 14:03:12」。
    """

    data = await console_kb_service.list_documents_page(
        db,
        tenant_id=tenant_id_of(identity),
        kb_id=kb_id,
        page=page,
        page_size=page_size,
        status=status,
        keyword=keyword,
    )
    return ok(KbDocumentsData.model_validate(data))


@router.get(
    "/knowledge-bases/{kb_id}/build",
    response_model=ApiResponse[KbBuildData],
    summary="这个库的构建状态（进行中优先，否则最近一次）",
)
async def knowledge_base_build(
    kb_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KbBuildData]:
    """只回答「这一版建到哪儿了」。细粒度刷新时只问这一格，不必重取整页文档。"""

    data = await console_kb_service.build_of(db, tenant_id=tenant_id_of(identity), kb_id=kb_id)
    return ok(KbBuildData.model_validate(data))


@router.get(
    "/knowledge-bases/{kb_id}/settings/preview",
    response_model=ApiResponse[KBSettingsPreviewData],
    summary="改切块设置前的确认信息（当前设置、生效参数、重建量）",
)
async def knowledge_base_settings_preview(
    kb_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KBSettingsPreviewData]:
    """确认弹窗要的那份事实。只读，不改任何东西。"""

    data = await console_kb_service.settings_preview(
        db, tenant_id=tenant_id_of(identity), kb_id=kb_id
    )
    return ok(KBSettingsPreviewData.model_validate(data))


@router.patch(
    "/knowledge-bases/{kb_id}/settings",
    response_model=ApiResponse[UpdateKBSettingsData],
    summary="改切块设置并自动整库重建（需确认）",
)
async def update_knowledge_base_settings(
    payload: UpdateKBSettingsRequest,
    request: Request,
    kb_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[UpdateKBSettingsData]:
    """一次改完并重建：设置、审计、构建行同一笔事务；投递登记在提交之后。

    ``confirm=false`` 直接 20002 —— 不确认就不动（这次改动的代价是整库重嵌入）。
    """

    data = await console_kb_service.update_kb_settings(
        db,
        tenant_id=tenant_id_of(identity),
        kb_id=kb_id,
        actor=identity,
        request_id=submitted_request_id(request),
        split_strategy=payload.split_strategy,
        chunk_size=payload.chunk_size,
        section_part_max_chars=payload.section_part_max_chars,
        confirmed=payload.confirm,
        name=payload.name,
        description=payload.description,
    )
    return ok(UpdateKBSettingsData.model_validate(data))


@router.post(
    "/knowledge-bases/{kb_id}/rebuild",
    response_model=ApiResponse[RebuildKBData],
    summary="按当前切块设置重建整个库（建完自动激活）",
)
async def rebuild_knowledge_base(
    request: Request,
    kb_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[RebuildKBData]:
    """不改设置，只按**当前**设置重切一遍并让新版本生效。

    它是「需重建」那条提示的配套动作：切块参数是索引期旋钮，改完必须产出新版本才生效，
    而逐篇重建只会写回旧版本（参数快照不变，提示消不掉）。
    """

    data = await console_kb_service.rebuild_kb(
        db,
        tenant_id=tenant_id_of(identity),
        kb_id=kb_id,
        actor=identity,
        request_id=submitted_request_id(request),
    )
    return ok(RebuildKBData.model_validate(data))


@router.post(
    "/knowledge-bases",
    response_model=ApiResponse[CreateKBData],
    summary="建知识库（面板身份，含套餐静态配额）",
)
async def create_knowledge_base(
    payload: CreateKBRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
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
        section_part_max_chars=payload.section_part_max_chars,
        allowed_formats=payload.allowed_formats,
    )
    return ok(CreateKBData.model_validate(data))


@router.delete(
    "/knowledge-bases/{kb_id}",
    response_model=ApiResponse[DeleteKnowledgeBaseData],
    summary="删知识库（仅空库；库内仍有文档先删文档）",
)
async def delete_knowledge_base(
    kb_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[DeleteKnowledgeBaseData]:
    """删空库：非空 ``20015``、缺失/跨租户 ``20008``；不做级联删除。"""

    data = await console_kb_service.delete_empty_kb(
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
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
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


@router.get(
    "/documents/{document_id}/versions",
    response_model=ApiResponse[DocumentVersionListData],
    summary="列一篇文档的版本（按稿号倒序，正文按需返回）",
)
async def list_document_versions(
    document_id: str = Path(..., min_length=1),
    include_content: bool = Query(False),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[DocumentVersionListData]:
    """这篇文档的全部稿次（按稿号倒序，现在这一稿在最前）+ 当前稿指针。

    走**读门**（``require_console_tenant``）：看版本不改任何状态，也不该要求
    ``x-requested-with``。租户只来自会话，跨租户与不存在同为 20016。

    出参字段由 ``document_version_service.version_list_data`` 决定——与数据面那条列表
    接口是同一个函数（控制面换的是门，不是契约）。
    """

    data = await console_kb_service.list_document_versions(
        db,
        tenant_id=tenant_id_of(identity),
        document_id=document_id,
        include_content=include_content,
    )
    return ok(DocumentVersionListData.model_validate(data))


@router.post(
    "/documents/{document_id}/versions/{version}/rollback",
    response_model=ApiResponse[RollbackDocumentVersionData],
    summary="回滚到某一稿（切指针并重建；影响所有调用方）",
)
async def rollback_document_version(
    request: Request,
    document_id: str = Path(..., min_length=1),
    version: int = Path(...),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[RollbackDocumentVersionData]:
    """把当前稿指针拨回第 ``version`` 稿，并让这篇文档立刻回到「等重建」的状态。

    **这条操作影响所有调用方**：面板上的一次点击会改掉整个租户检索到的内容，所以前端
    必须先确认（票面验收项）。后端不再要求额外的「确认」参数——它只回答「按当前库状态
    这次回滚合不合法」；把「要不要做」留在人的那一步，接口就还是那条幂等语义明确的写。

    语义与数据面那条逐字一致：处理中 20025 / 稿不存在 20030 / 已是当前稿 20032，都是
    HTTP 200 + ``code``（ADR-0007）。这些判定全部在被委托的
    ``document_version_service.rollback_to_version`` 里，本层只取参数、调服务、装信封。

    写入口声明 ``Depends(get_async_transaction)``：一个请求一笔事务（指针 + 账本），
    提交点只在 ``app.db.transaction`` 一处；投递重建与翻关键词标记登记在 ``after_commit``。
    """

    data = await console_kb_service.rollback_document(
        db,
        tenant_id=tenant_id_of(identity),
        document_id=document_id,
        version=version,
        actor=identity,
        cache=cache,
        request_id=submitted_request_id(request),
    )
    return ok(RollbackDocumentVersionData.model_validate(data))


@router.post(
    "/documents/{document_id}/reindex",
    response_model=ApiResponse[ReindexDocumentData],
    summary="重建单文档索引（可重解析原始文件、可强制重建成功文档）",
)
async def reindex_document(
    request: Request,
    document_id: str = Path(..., min_length=1),
    reparse: bool = Query(False),
    force: bool = Query(False),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    cache: PlanCache = Depends(get_plan_cache),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[ReindexDocumentData]:
    """守卫 → 准备入队（不盖租约）→ 审计（同一笔事务）→ 投递，立即回 ``status=3``。"""

    data = await console_kb_service.reindex_document(
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
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
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
async def read_document_content(
    document_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[DocumentContentData]:
    """走**读门**（`require_console_tenant`）而不是写门：看原文不改任何状态，也不该要求
    `x-requested-with`——那个头是给写操作防跨站用的，加在读接口上只会让「查看」多一步
    无意义的配合。跨租户与不存在同为 20016。
    """

    data = await console_kb_service.read_document_content(
        db, tenant_id=tenant_id_of(identity), document_id=document_id
    )
    return ok(DocumentContentData.model_validate(data))
