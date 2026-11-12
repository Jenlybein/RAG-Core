"""控制面「管库管文档」的业务编排：树、建库、删空库、上传、重建、删除。

路由只做「取参数 → 调这里 → 装壳」，所有判定收在这一层。**与数据面同构**是这一票的
主线：上传/重建/删除复用的不是另一套实现，而是 ``document_service.upload``、
``document_state``、``document_repository.prepare_for_indexing``、``enqueue_document``、
``purge_document_chunks`` 这些同一批函数；控制面换的只是身份（会话 cookie）与响应壳
外面的那道门。

三处顺序是契约的一部分，不是随手排的：

- **重建只准备入队、不盖租约**：路由先走 ``ensure_reindexable``（处理中 20025、成功
  未 force 20027），再 ``prepare_for_indexing`` 把行置成「已排队、未开工」，最后投递；
  Worker 才是唯一抢占者。若这里先盖租约，Worker 会因租约新鲜而拒绝执行，重建变成
  静默 no-op。
- **删除文档的清除顺序**：purge 切片 → 删原文件 → 删行 → 提交。审计插在「删行」与
  「提交」之间，顺序本身不变。
- **删库不做级联**：库内仍有文档就 20015；级联会跨越「清向量 / 清关键词 / 删文件 /
  删行」四步，半途失败会留下不可解释的中间态。

**审计只记 id / title / status 这类结构化字段，绝不记文档正文。** 控制面的写接口各自
写一条账本（``kb.create`` / ``kb.delete`` / ``doc.upload`` / ``doc.reindex`` /
``doc.delete``），读接口（树）不写。

跨租户与不存在都按各自对象已有的「不存在」码：库 ``20008``、文档 ``20016``，绝不返回
「无权限」——可区分的拒绝会泄露资源存在性（ADR-0006 的同一口径）。

**整层都在异步栈上**（第 11 步票 #130）：会话是 ``AsyncSession``，账本行与业务行共用
同一个会话、由路由上的 ``Depends(get_async_transaction)`` 一笔提交。数据面那三条同名
端点（列 / 重建 / 删除）调的是这里的同一批函数，于是它们也一起换到了异步栈上——
「两边共用一份实现」这条纪律没有变，变的只是这份实现跑在哪种会话上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache
from app.db.transaction import after_commit
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase, formats_of, settings_of
from app.models.plan import PlanSpec
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import (
    chunk_repository,
    document_repository,
    knowledge_base_repository,
    tenant_repository,
)
from app.services import (
    document_service,
    document_state,
    knowledge_base_service,
    plan_service,
    static_quota,
)
from app.services.chunk_store import build_chunk_stores
from app.services.console_service import append_audit
from app.services.document_ingestion import prepare_document_content
from app.services.document_storage import remove_document_file
from app.services.index_dispatch import enqueue_document
from app.services.indexing_service import purge_document_chunks
from app.utils.status import DocumentStatus

#: 审计动作名：列表与筛选取的是这些字符串，因此只在服务层写一次。
#: 前缀把对象类型放在名字里（``kb.`` / ``doc.``），一个动作名就能看出动的是哪类行。
ACTION_KB_CREATE: Final[str] = "kb.create"
ACTION_KB_DELETE: Final[str] = "kb.delete"
ACTION_DOC_UPLOAD: Final[str] = "doc.upload"
ACTION_DOC_REINDEX: Final[str] = "doc.reindex"
ACTION_DOC_DELETE: Final[str] = "doc.delete"

#: 删非空库的对外提示。票面要求**消息里说要先删文档**：只说「参数非法」会让面板只能
#: 弹一句「出错了」，而操作者其实知道下一步做什么。
MSG_KB_NOT_EMPTY: Final[str] = "该知识库下还有文档，请先删文档再删库"

#: 账号的租户行不存在（数据被改坏）时的提示。它不是「没权限」——会话里的账号指向了
#: 一个不存在的租户，按未授权处理，前端该重新登录。
MSG_TENANT_NOT_FOUND: Final[str] = "console tenant not found"


@dataclass(frozen=True)
class TreeDocument:
    """树里的一份文档摘要；切片数由每库一次分组查询回填。"""

    document_id: str
    title: str
    status: int
    chunk_count: int
    error_message: str | None
    source_type: str
    created_at: datetime


@dataclass(frozen=True)
class TreeKnowledgeBase:
    """树里的一个库（含 ``documents``；空库就是空列表，不是 None）。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    documents: list[TreeDocument]


async def list_tree(db: AsyncSession, *, tenant_id: str) -> list[TreeKnowledgeBase]:
    """本租户全部知识库 + 每库文档 + 切片数。**不做白名单收窄**。

    控制面要看整个租户：数据范围白名单约束的是「这把 Key 能查什么」，不该让面板
    看不见自己名下的库。切片数按库一次分组查询（``count_by_document_ids``），
    绝不按文档逐条查——一个租户有几个库、每个库几百份文档时那是几百次往返。
    """

    nodes: list[TreeKnowledgeBase] = []
    for kb in await knowledge_base_repository.list_by_tenant(db, tenant_id):
        documents = await document_repository.list_by_kb_async(
            db, kb.id, tenant_id=tenant_id
        )
        counts = await chunk_repository.count_by_document_ids(
            db, [document.id for document in documents], tenant_id=tenant_id
        )
        nodes.append(
            TreeKnowledgeBase(
                kb_id=kb.id,
                name=kb.name,
                split_strategy=kb.split_strategy,
                allowed_formats=formats_of(kb),
                chunk_size=settings_of(kb)["chunk_size"],
                documents=[
                    TreeDocument(
                        document_id=document.id,
                        title=document.title,
                        status=document.status,
                        chunk_count=counts.get(document.id, 0),
                        error_message=document.error_message,
                        source_type=document.source_type,
                        created_at=document.created_at,
                    )
                    for document in documents
                ],
            )
        )
    return nodes


async def create_kb(
    db: AsyncSession,
    *,
    tenant_id: str,
    actor: ConsoleIdentity,
    cache: PlanCache,
    name: str,
    description: str | None,
    split_strategy: str,
    chunk_size: int,
    allowed_formats: Sequence[str] | None,
) -> dict[str, Any]:
    """建库：套餐校验与落库复用 ``knowledge_base_service``，审计与写入同一个事务。

    提交点是路由上的 ``Depends(get_async_transaction)``：这里只写，不提交。
    """

    plan = await _plan_for_tenant(db, tenant_id=tenant_id, cache=cache)
    data = await knowledge_base_service.create_kb(
        db,
        name=name,
        description=description,
        tenant_id=tenant_id,
        split_strategy=split_strategy,
        plan=plan,
        chunk_size=chunk_size,
        allowed_formats=allowed_formats,
    )
    await append_audit(
        db,
        actor=actor,
        action=ACTION_KB_CREATE,
        target=f"kb:{data['kb_id']}",
        before=None,
        after=_kb_snapshot_of_data(data),
    )
    return data


async def delete_empty_kb(
    db: AsyncSession, *, tenant_id: str, kb_id: str, actor: ConsoleIdentity
) -> dict[str, Any]:
    """删库：仅空库。库内仍有文档 → ``20015``（提示先删文档）；缺失/跨租户 → ``20008``。"""

    kb = await knowledge_base_repository.get_by_id_for_tenant(
        db, kb_id, tenant_id=tenant_id
    )
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    if await document_repository.count_by_kb_async(db, kb_id, tenant_id=tenant_id) > 0:
        raise AppError(ErrorCode.PARAM_ERROR, MSG_KB_NOT_EMPTY)

    before = _kb_snapshot(kb)
    await db.delete(kb)
    await append_audit(
        db,
        actor=actor,
        action=ACTION_KB_DELETE,
        target=f"kb:{kb_id}",
        before=before,
        after=None,
    )
    return {"kb_id": kb_id, "deleted": True}


async def upload_document(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    actor: ConsoleIdentity,
    cache: PlanCache,
    title: str | None = None,
    content: str = "",
    filename: str | None = None,
    data: bytes = b"",
    request_id: str | None = None,
) -> dict[str, Any]:
    """上传文档：登记 + 投递复用 ``document_service.upload``，立即回 ``status=3``。

    ``document_service.upload`` 只登记、不提交（投递也由它登记在提交之后），所以这里的
    审计与登记行**落在同一笔请求事务里**：审计写不进去时登记也不落库，不会留下「文档
    已登记、账本里没有」的窗口。整笔的提交点是路由上的 ``Depends(get_async_transaction)``。
    """

    plan = await _plan_for_tenant(db, tenant_id=tenant_id, cache=cache)
    result = await document_service.upload(
        db,
        tenant_id=tenant_id,
        kb_id=kb_id,
        plan=plan,
        title=title,
        content=content,
        filename=filename,
        data=data,
        request_id=request_id,
    )
    await append_audit(
        db,
        actor=actor,
        action=ACTION_DOC_UPLOAD,
        target=f"doc:{result['document_id']}",
        before=None,
        after=_document_submission_snapshot(result, title=title, filename=filename),
    )
    return result


async def reindex_document(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    reparse: bool,
    force: bool,
    actor: ConsoleIdentity,
    cache: PlanCache,
    request_id: str | None,
) -> dict[str, Any]:
    """单文档重建：守卫 → 准备入队 → 审计（同一笔事务）→ 投递，立即回 ``status=3``。

    顺序与数据面逐字一致：先按 id + 租户取（缺失/跨租户 20016），再
    ``ensure_reindexable``（20025 / 20027），再过并发处理配额，然后
    ``prepare_for_indexing``（不盖租约）。准备失败说明这一行刚被抢先，仍按 20025。
    投递登记在 ``after_commit`` 上：先让「已排队」这个状态落库，再让 Worker 去抢。
    """

    document = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if document is None:
        raise AppError(ErrorCode.NOT_FOUND)

    document_state.ensure_reindexable(document, force=force)

    # 重建跳过「单库文档数」（文档本来就在库里），但照样占用一个 Worker 名额。
    plan = await _plan_for_tenant(db, tenant_id=tenant_id, cache=cache)
    await static_quota.ensure_processing_quota(db, tenant_id=tenant_id, plan=plan)

    # 改前快照在 prepare 之前取：那条 UPDATE 用 ``synchronize_session=False``，不该让
    # 审计的「改前」依赖会话是否恰好刷新了对象。
    before = _document_state_snapshot(document)
    prepared = await document_repository.prepare_for_indexing_async(
        db, document_id, tenant_id=tenant_id, allow_success=force
    )
    if not prepared:
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)

    await append_audit(
        db,
        actor=actor,
        action=ACTION_DOC_REINDEX,
        target=f"doc:{document_id}",
        before=before,
        after={
            **before,
            "status": DocumentStatus.PROCESSING,
            "reparse": reparse,
            "force": force,
        },
    )
    after_commit(
        db,
        lambda: enqueue_document(
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=document.kb_id,
            request_id=request_id,
            reparse=reparse,
        ),
    )
    return {
        "document_id": document_id,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "reparse": reparse,
    }


async def delete_document(
    db: AsyncSession, *, tenant_id: str, document_id: str, actor: ConsoleIdentity
) -> dict[str, Any]:
    """删除文档：守卫 → purge 切片 → 删原文件 → 删行 → 审计 → 由请求边界提交。

    顺序是契约的一部分（见 ``app/api/routes/documents.py`` 的同名端点）；审计插在
    「删行」与「提交」之间，不改动四步本身。有效处理中（租约未过期）拒绝删除；租约
    过期视为 Worker 卡死，允许删除。
    """

    document = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if document is None:
        raise AppError(ErrorCode.NOT_FOUND)

    document_state.ensure_deletable(document)

    stores = build_chunk_stores(get_vector_store(), get_keyword_search_provider())
    await purge_document_chunks(db, document_id, tenant_id=tenant_id, stores=stores)
    remove_document_file(document.source_file_path)

    before = _document_state_snapshot(document)
    await db.delete(document)
    await append_audit(
        db,
        actor=actor,
        action=ACTION_DOC_DELETE,
        target=f"doc:{document_id}",
        before=before,
        after=None,
    )
    return {"document_id": document_id, "deleted": True}


async def _tenant_plan_id(db: AsyncSession, *, tenant_id: str) -> int:
    """会话租户 → 它当前的套餐 id；租户不在就 ``20010``（与数据面同一句提示）。"""

    tenant = await tenant_repository.get_by_id_async(db, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_TENANT_NOT_FOUND)
    return tenant.plan_id


async def _plan_for_tenant(
    db: AsyncSession, *, tenant_id: str, cache: PlanCache
) -> PlanSpec:
    """会话租户 → 它当前的套餐快照（走与数据面同一个解析与缓存）。"""

    return await plan_service.resolve_plan(
        db, await _tenant_plan_id(db, tenant_id=tenant_id), cache=cache
    )


def _kb_snapshot(kb: KnowledgeBase) -> dict[str, Any]:
    """库的结构化快照：只留这次改动关心的字段，正文（description）不进账本。

    建库与删库共用同一份形状，事后对比「改了什么」不需要人肉对齐两套键名。
    """

    return {
        "kb_id": kb.id,
        "name": kb.name,
        "split_strategy": kb.split_strategy,
        "allowed_formats": formats_of(kb),
        "chunk_size": settings_of(kb)["chunk_size"],
    }


def _kb_snapshot_of_data(data: dict[str, Any]) -> dict[str, Any]:
    """建库回执 → 与 :func:`_kb_snapshot` 同一形状的审计快照。"""

    return {
        "kb_id": data["kb_id"],
        "name": data["name"],
        "split_strategy": data["split_strategy"],
        "allowed_formats": list(data["allowed_formats"]),
        "chunk_size": data["chunk_size"],
    }


def _document_state_snapshot(document: Document) -> dict[str, Any]:
    """文档的状态快照：**不含正文**，只回答「哪一份、什么状态」的问题。"""

    return {
        "document_id": document.id,
        "kb_id": document.kb_id,
        "title": document.title,
        "status": document.status,
        "source_type": document.source_type,
    }


def _document_submission_snapshot(
    result: dict[str, Any], *, title: str | None, filename: str | None
) -> dict[str, Any]:
    """上传回执 → 审计快照。标题取调用方给的，缺省按文件基名（与落库标题同一规则）。"""

    resolved_title = title or (Path(filename).stem if filename else "")
    return {
        "document_id": result["document_id"],
        "kb_id": result["kb_id"],
        "title": resolved_title,
        "status": result["status"],
        "source_type": result["source_type"],
    }


#: 单次回显的正文上限（字符）。
#:
#: 上传上限是 20MB，一份 PDF 解析出来几十万字并不罕见；整份塞进一个 JSON 响应对浏览器
#: 是硬的负担（前端要把它放进 DOM）。超过就截断并**明确告诉调用方截断了**——静默截断
#: 会让人以为文档只有这么长，进而怀疑索引漏了内容。
DOCUMENT_CONTENT_MAX_CHARS: Final[int] = 200_000


async def read_document_content(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """读一份文档的**原文**（只读：不改状态、不写审计、不碰索引）。

    正文从哪来，完全交给 Worker 用的同一个函数 ``prepare_document_content``：

    - **JSON 模式**（贴正文上传）：``documents.content`` 里就有，直接读库；
    - **文件模式**（md / txt / docx / pdf）：库里 ``content`` 是空串，正文只在落盘文件里，
      这里现场解析。

    复用同一个函数而不是「库里没有就读文件」再写一遍：解析口径必须与切块时一致，
    否则用户看到「原文」和检索命中的切片对不上，就会怀疑是检索漏了。

    ``max_chars`` 默认取模块常量而**不写成默认参数值**：默认参数在函数定义时求值，
    写成 ``max_chars: int = DOCUMENT_CONTENT_MAX_CHARS`` 会让常量从此不可调整
    （测试里改常量也不生效），这类「看起来能配、其实不能」的常量比没有更糟。
    """

    cap = DOCUMENT_CONTENT_MAX_CHARS if max_chars is None else max_chars

    document = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if document is None:
        # 跨租户与不存在同码（20016）：可区分的拒绝会泄露资源存在性。
        raise AppError(ErrorCode.NOT_FOUND)

    parsed = prepare_document_content(document)
    text = parsed.content
    truncated = len(text) > cap
    return {
        "document_id": document.id,
        "title": document.title,
        "source_type": document.source_type or "text",
        "source_filename": document.source_filename,
        "status": document.status,
        "content": text[:cap],
        "truncated": truncated,
        "length": len(text),
    }
