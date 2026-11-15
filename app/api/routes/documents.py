"""文档接口：异步上传、状态查询、重建索引、删除，以及版本（建 / 列）。

只有一个模块、一个 router：
  * ``POST /documents/upload`` —— 单端点按 content-type 分流（multipart = 文件模式，
    其余 = JSON 模式）。只登记 + 投递索引任务，立即返回 ``status=3``。
  * ``GET /documents?kb_id=`` —— 该知识库下的文档列表，异步化后调用方唯一的进度观测口。
  * ``POST /documents/{document_id}/reindex?reparse=&force=`` —— 单文档重建：守卫 →
    标记排队（不盖租约）→ 由请求事务提交 → 投递；purge、抢占与写入仍只在 Worker
    的事务里发生。
  * ``DELETE /documents/{document_id}`` —— 守卫通过后按租户清除切片、删原文件、删记录。
  * ``POST /documents/{document_id}/versions`` —— 提交一份新正文，得到下一个稿次
    （#175；写路径的顺序见 ``app.services.document_version_service``）。
  * ``GET /documents/{document_id}/versions?include_content=`` —— 这篇文档改过几稿，
    按稿号倒序，正文默认不带（#175）。

写入口都声明事务依赖：一个请求一笔事务，成功提交、异常回滚，提交点只在
:mod:`app.db.transaction` 一处。读入口（列文档）**不声明**——读路径不该提交。

四条入口全部在异步栈上（第 11 步票 #130 收尾）：``Depends(get_async_db)`` +
``Depends(get_async_transaction)``，会话是 ``AsyncSession``，写路径的提交点是
``app.db.transaction`` 的异步孪生。同一个路由文件里只剩一套会话依赖——迁移期那种
「两个会话依赖并存」的形状随最后一个同步入口一起消失了。

按 ``document_id`` 取文档一律经 ``get_by_id_for_tenant``（ADR-0006）：缺失与跨租户
统一按「不存在」（20016）处理，不泄露存在性。守卫一律走 ``document_state`` 的
``ensure_reindexable`` / ``ensure_deletable``，不在路由里另写 if。

四个入口都先过**第二道关卡**（数据范围白名单，``app.services.data_scope``）：上传与
列文档按请求里的 ``kb_id`` 判，重建与删除按文档所属的库判。越界与跨租户同一个码
（20008），理由与 ``ensure_visible`` 相同——可区分的拒绝会泄露资源存在性。
"""

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_key
from app.api.upload_parsing import (
    parse_upload_request,
    parse_version_request,
    submitted_request_id,
)
from app.core.auth import DevKeyContext
from app.core.exceptions import AppError, ErrorCode
from app.db.async_session import get_async_db, get_async_transaction
from app.db.transaction import AsyncTransaction, after_commit
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import (
    chunk_repository,
    document_repository,
    document_version_repository,
)
from app.schemas.common import ApiResponse, ok
from app.schemas.document import (
    CreateDocumentVersionData,
    CreateDocumentVersionRequest,
    DeleteDocumentData,
    DocumentListData,
    DocumentListItem,
    DocumentVersionItem,
    DocumentVersionListData,
    ReindexDocumentData,
    UploadDocumentData,
    UploadDocumentRequest,
)
from app.services import document_service, document_state, document_version_service, static_quota
from app.services.chunk_store import build_chunk_stores
from app.services.data_scope import ensure_kb_scope
from app.services.document_storage import remove_document_file
from app.services.knowledge_base_access import ensure_visible
from app.services.index_dispatch import enqueue_document, enqueue_document_after_commit
from app.services.indexing_service import purge_document_chunks
from app.utils.status import DocumentStatus

router = APIRouter()

logger = structlog.get_logger(__name__)


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
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[UploadDocumentData]:
    """单端点按 content-type 分流：multipart 走文件模式，其余走 JSON 模式。

    FastAPI 不能在同一次操作里既声明 Pydantic body 又声明 Form/File，因此这里声明
    只有 Form/File，JSON 分支由 ``parse_upload_request`` 手工 ``model_validate`` 请求正文
    （表单解析对非 multipart 的 content-type 不读 body，所以 JSON 分支仍能取到完整正文）。
    分流的细节、缺字段的 20002 与非法 JSON 的兜底都在 :mod:`app.api.upload_parsing`，
    与控制面那份上传共用同一段实现。
    """

    upload = await parse_upload_request(
        request,
        kb_id_field=kb_id,
        title_field=title,
        file=file,
        json_model=UploadDocumentRequest,
        require_form_kb_id=True,
    )
    uploaded_kb_id = upload.kb_id
    assert uploaded_kb_id is not None, "require_form_kb_id=True：解析器必然给出 kb_id"

    # 第二道关卡：白名单。两种分流同一份判定、同一个码，位置在**落库与落盘之前**：
    # 被拒的上传不写行、不建索引任务。
    ensure_kb_scope(policy=key.policy, kb_ids=[uploaded_kb_id])

    data = await document_service.upload(
        db,
        tenant_id=key.tenant_id,
        kb_id=uploaded_kb_id,
        plan=key.plan,
        title=upload.title,
        filename=upload.filename,
        data=upload.data,
        content=upload.content,
        request_id=upload.request_id,
    )
    return ok(UploadDocumentData.model_validate(data))


@router.get(
    "/documents",
    response_model=ApiResponse[DocumentListData],
    summary="列知识库下的文档（轮询异步进度）",
)
async def list_documents(
    kb_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[DocumentListData]:
    """返回该知识库下全部文档的 status / chunk_count / error_message 等摘要。

    知识库不存在或不属于调用方租户，一律按「不存在」（20008）处理，不泄露存在性；
    列表查询也强制带 ``tenant_id``，绝不返回其他租户的行。
    ``chunk_count`` 用一次分组计数批量取回，不对每份文档各查一次（N+1）。
    """

    # 第二道关卡：白名单（先于任何库行查询），然后才是租户可见性。
    ensure_kb_scope(policy=key.policy, kb_ids=[kb_id])
    await ensure_visible(db, tenant_id=key.tenant_id, kb_id=kb_id)

    rows = await document_repository.list_by_kb_async(db, kb_id, tenant_id=key.tenant_id)
    counts = await chunk_repository.count_by_document_ids(
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
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[ReindexDocumentData]:
    """守卫 → 标记排队（不盖租约）→ 由 ``tx`` 提交 → 投递，立即返回 ``status=3``。

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

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=key.tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 第二道关卡：白名单。这条入口按 ``document_id`` 进来，因此范围要看**它所属的库**；
    # 取文档那一句已经强制带了租户，范围判定紧接着做，之后才轮到状态守卫。
    ensure_kb_scope(policy=key.policy, kb_ids=[doc.kb_id])

    document_state.ensure_reindexable(doc, force=force)

    # 静态配额：重建虽然跳过「单库文档数」（文档本来就在库里），却照样往队列塞一份
    # 解析任务，所以必须过并发处理数——这是最容易漏、也最能饿死其他租户的一条。
    await static_quota.ensure_processing_quota(db, tenant_id=key.tenant_id, plan=key.plan)

    prepared = await document_repository.prepare_for_indexing_async(
        db, document_id, tenant_id=key.tenant_id, allow_success=force
    )
    if not prepared:
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)

    after_commit(
        db,
        lambda: enqueue_document_after_commit(
            publish=enqueue_document,
            document_id=document_id,
            tenant_id=key.tenant_id,
            kb_id=doc.kb_id,
            request_id=submitted_request_id(request),
            reparse=reparse,
            # 稿号在**这里**从指针取下来、随任务走（#173）：Worker 不再读指针，所以
            # 这次重建算哪一稿在投递那一刻就定了。指针之后被回滚改动也不影响它。
            doc_version=doc.current_version,
        ),
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
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[DeleteDocumentData]:
    """守卫通过后按固定顺序删除：清除切片 → 删原文件 → 删版本行 → 删行 → 由 ``tx`` 提交。

    ``purge_document_chunks`` 遍历存储列表（向量存储 + 关键词检索，接线见
    ``app.services.chunk_store.build_chunk_stores``），因此删文档不需要知道存储
    形态；删除切片同样强制带租户（ADR-0006）。有效处理中（租约未过期）拒绝删除
    （20025）；租约过期视为 Worker 卡死，允许删除。

    **版本行单独删一次**（#172）：一张表都没有外键（仓库零外键），库不会替我们级联，
    所以由这条路径显式删，且与清除切片、删行同一笔事务。
    """

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=key.tenant_id
    )
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
    # 版本行的源文件按**稿号**落盘（``<doc_id>.v<N><ext>``，#175），所以它们不会出现在
    # ``doc.source_file_path`` 里：不清就是一批永远无人认领的文件（文档行与版本行都没了，
    # 没有任何地方还记着这些路径）。
    for version_file in await document_version_repository.source_file_paths(db, document_id):
        remove_document_file(version_file)
    await document_version_repository.delete_for_document(db, document_id)
    await db.delete(doc)

    logger.info(
        "document deleted",
        document_id=document_id,
        tenant_id=key.tenant_id,
        kb_id=doc.kb_id,
        source_type=doc.source_type,
    )
    return ok(DeleteDocumentData(document_id=document_id, deleted=True))


@router.post(
    "/documents/{document_id}/versions",
    response_model=ApiResponse[CreateDocumentVersionData],
    summary="提交新版本（建一个稿次，随后重建）",
)
async def create_document_version(
    document_id: str,
    request: Request,
    file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[CreateDocumentVersionData]:
    """提交一份新正文（JSON ``{"content": ...}`` 或 multipart 的 ``file``）→ 新稿次。

    顺序与理由见 ``app.services.document_version_service``。路由这一层只做三件事：
    **按 content-type 分流**（与上传共用 ``app.api.upload_parsing`` 里那套错误体）、
    **第二道关卡**（数据范围白名单，按文档所属的库判；越界与跨租户同一个码 20008）、
    以及把结果套进统一信封。

    为什么守卫在路由里而判重在服务里：范围白名单是**授权**，它属于 HTTP 入口（每条入口
    各有各的来源：API Key 的 policy / 控制面的会话租户）；而「内容有没有变」「稿号是几」
    是业务规则，属于服务。写接口因此照旧声明 ``Depends(get_async_transaction)``：
    一个请求一笔事务，提交点只在 ``app.db.transaction`` 一处。
    """

    submitted = await parse_version_request(
        request, file=file, json_model=CreateDocumentVersionRequest
    )

    # 按 id + 租户取文档：缺失与跨租户统一 20016（不泄露存在性），随后才是范围白名单。
    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=key.tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)
    ensure_kb_scope(policy=key.policy, kb_ids=[doc.kb_id])

    data = await document_version_service.create_version(
        db,
        tenant_id=key.tenant_id,
        document_id=document_id,
        plan=key.plan,
        content=submitted.content,
        filename=submitted.filename,
        data=submitted.data,
        request_id=submitted.request_id,
    )
    return ok(CreateDocumentVersionData.model_validate(data))


@router.get(
    "/documents/{document_id}/versions",
    response_model=ApiResponse[DocumentVersionListData],
    summary="列文档的版本（按稿号倒序，默认不带正文）",
)
async def list_document_versions(
    document_id: str,
    include_content: bool = Query(False),
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[DocumentVersionListData]:
    """返回这篇文档的全部稿次，**按稿号倒序**（现在这一稿在最前）+ 当前稿指针。

    **正文默认不给**（``include_content=False``）：一批整篇正文的响应既大又慢，而列表的
    常见用途是「这篇文档改过几稿、最近一稿是哪天」。要看某一稿的正文得显式要求。

    读入口**不声明**事务依赖：读路径不提交（见模块文档）。当前稿指针取文档行上的那一列，
    不取「状态是 current 的那一行」——指针是唯一真值，版本行的状态是它的派生（#175 的写路径
    保证两者一致，但读的时候只认指针）。
    """

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=key.tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 第二道关卡：白名单（与创建 / 重建 / 删除同一条，按文档所属的库判）。
    ensure_kb_scope(policy=key.policy, kb_ids=[doc.kb_id])

    rows = await document_version_repository.list_by_document(db, document_id)
    return ok(
        DocumentVersionListData(
            document_id=document_id,
            current_version=doc.current_version,
            items=[
                DocumentVersionItem(
                    version=row.version,
                    status=row.status,
                    checksum=row.checksum,
                    created_at=row.created_at,
                    content=row.content if include_content else None,
                )
                for row in rows
            ],
        )
    )
