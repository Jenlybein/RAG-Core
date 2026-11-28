"""控制面「管库管文档」的业务编排：树、建库、删空库、上传、重建、删除、版本列表与回滚。

路由只做「取参数 → 调这里 → 装壳」，所有判定收在这一层。**与数据面同构**是这一票的
主线：上传/重建/删除复用的不是另一套实现，而是 ``document_service.upload``、
``document_state``、``document_repository.prepare_for_indexing``、``enqueue_document``、
``purge_document_chunks`` 这些同一批函数；控制面换的只是身份（会话 cookie）与响应壳
外面的那道门。

三处顺序是契约的一部分，不是随手排的：

- **重建只准备入队、不盖租约**：路由先走 ``ensure_reindexable``（有效处理中 20025、成功
  未 force 20027），再 ``prepare_for_indexing`` 把行置成「排队中」（``status=4``，
  不写租约），最后投递；
  Worker 才是唯一抢占者。若这里先盖租约，Worker 会因租约新鲜而拒绝执行，重建变成
  静默 no-op。
- **删除文档的清除顺序**：purge 切片 → 删原文件 → 删行 → 提交。审计插在「删行」与
  「提交」之间，顺序本身不变。
- **删库不做级联**：库内仍有文档就 20015；级联会跨越「清向量 / 清关键词 / 删文件 /
  删行」四步，半途失败会留下不可解释的中间态。

**审计只记 id / title / status 这类结构化字段，绝不记文档正文。** 控制面的写接口各自
写一条账本（``kb.create`` / ``kb.delete`` / ``doc.upload`` / ``doc.reindex`` /
``doc.delete`` / ``doc.rollback``），读接口（树 / 原文 / 版本列表）不写。

**版本相关的两条（#177）是委托，不是第二份实现**：版本列表交给
``document_version_service.version_list_data``（与数据面那条列表接口同一个函数），回滚
整段交给 ``document_version_service.rollback_to_version``。控制面这边只负责「会话租户 →
套餐快照」「写一条账本」——切指针、归档当前稿、置处理中、提交后投递重建与翻关键词标记
那一段顺序一旦写两遍，迟早只有一份是对的。

跨租户与不存在都按各自对象已有的「不存在」码：库 ``20008``、文档 ``20016``，绝不返回
「无权限」——可区分的拒绝会泄露资源存在性（ADR-0006 的同一口径）。

**整层都在异步栈上**（第 11 步票 #130）：会话是 ``AsyncSession``，账本行与业务行共用
同一个会话、由路由上的 ``Depends(get_async_transaction)`` 一笔提交。数据面那三条同名
端点（列 / 重建 / 删除）调的是这里的同一批函数，于是它们也一起换到了异步栈上——
「两边共用一份实现」这条纪律没有变，变的只是这份实现跑在哪种会话上。
"""

from __future__ import annotations

import structlog


from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.core.plan_cache import PlanCache
from app.db.base import now
from app.db.transaction import after_commit
from app.models.document import Document
from app.schemas.console_kbs import (
    DocumentStatusCountsData,
    KbDocumentRowData,
    KnowledgeBaseSummaryData,
    TokenUsageData,
)
from app.models.knowledge_base import (
    build_params_of,
    MAX_CHUNK_SIZE,
    MAX_SECTION_PART_MAX_CHARS,
    MIN_CHUNK_SIZE,
    MIN_SECTION_PART_MAX_CHARS,
    KnowledgeBase,
    formats_of,
    resolve_formats,
    settings_of,
)
from app.models.plan import PlanSpec
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import (
    chunk_repository,
    document_repository,
    document_version_repository,
    knowledge_base_repository,
    tenant_repository,
)
from app.services import (
    document_service,
    document_state,
    document_version_service,
    knowledge_base_service,
    plan_service,
)
from app.services import chunk_ledger, static_quota
from app.services.chunk_store import build_chunk_stores
from app.services.generation_sweep import _stale_predicate
from app.services.index_generation import settings_fingerprint
from app.services.console_service import append_audit
from app.services.document_ingestion import prepare_document_content
from app.services.document_storage import remove_document_file
from app.services.index_dispatch import (
    enqueue_document,
    enqueue_document_after_commit,
)

from app.services.indexing_service import purge_document_chunks
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)


#: 审计动作名：列表与筛选取的是这些字符串，因此只在服务层写一次。
#: 前缀把对象类型放在名字里（``kb.`` / ``doc.``），一个动作名就能看出动的是哪类行。
ACTION_KB_CREATE: Final[str] = "kb.create"
ACTION_KB_DELETE: Final[str] = "kb.delete"
#: 改库的切块设置并重建（第 16 步 #207）：账本里要能看出「改了什么、为什么要重建」。
ACTION_KB_SETTINGS_UPDATE: Final[str] = "kb.settings_update"
#: 单纯重建一个库（不改任何设置）：账本里要能看出「有人主动重切了一次」。
#:
#: 为什么不复用 ``kb.settings_update``：那条动作的 before/after 是**设置快照**，而这条什么
#: 都没改——写成 settings_update 会得到一条 before == after 的记录，读账的人会以为是改坏了。
ACTION_KB_REBUILD: Final[str] = "kb.rebuild"
ACTION_DOC_UPLOAD: Final[str] = "doc.upload"
ACTION_DOC_REINDEX: Final[str] = "doc.reindex"
ACTION_DOC_DELETE: Final[str] = "doc.delete"
#: 回滚是一次指针改动（不产生新稿次）：账本里要能看出「从哪一稿退到哪一稿」（#177）。
ACTION_DOC_ROLLBACK: Final[str] = "doc.rollback"

#: 删非空库的对外提示。票面要求**消息里说要先删文档**：只说「参数非法」会让面板只能
#: 弹一句「出错了」，而操作者其实知道下一步做什么。
MSG_KB_NOT_EMPTY: Final[str] = "该知识库下还有文档，请先删文档再删库"

#: 改切块设置必须显式确认（第 16 步 #207）：确认之后会自动整库重建，那是几分钟到几十分钟的
#: Worker 与一次全库重嵌入。没说清的「顺手改一下」不能悄悄变成这个代价。
MSG_KB_SETTINGS_NEEDS_CONFIRM: Final[str] = (
    "修改切块设置会触发整库重建，请在确认后再提交（confirm=true）"
)

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
    #: 这一篇的正文 token 数（#220），与容量判据同一把尺子。
    token_count: int = 0


@dataclass(frozen=True)
class TreeKnowledgeBase:
    """树里的一个库（含 ``documents``；空库就是空列表，不是 None）。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    #: 节段阈值（第 16 步 #207）：前端要能回显，否则「改设置」的弹窗只能显示默认值。
    section_part_max_chars: int
    documents: list[TreeDocument]
    #: 「N 篇待更新」：按篇比设置指纹得出的篇数（现场九·第三步）。0 = 全部最新。
    #: 放在 ``documents`` 之后是因为 dataclass 的「带默认值的字段必须在后」。
    stale_documents: int = 0
    #: 这个库的 token 容量占用（#221 / 用户故事 #13）；``None`` 只出现在容量读不到的降级路径上。
    token_usage: TokenUsageData | None = None


async def list_tree(db: AsyncSession, *, tenant_id: str) -> list[TreeKnowledgeBase]:
    """本租户全部知识库 + 每库文档 + 切片数。**不做白名单收窄**。

    控制面要看整个租户：数据范围白名单约束的是「这把 Key 能查什么」，不该让面板
    看不见自己名下的库。切片数按库一次分组查询（``count_by_document_ids``），
    绝不按文档逐条查——一个租户有几个库、每个库几百份文档时那是几百次往返。
    """

    # 套餐在**循环外**解析一次：容量上限是租户级属性，每个库都查一遍套餐既多几次往返，
    # 又让「同一个库在同一次响应里有两个上限」这种错有机可乘。
    plan = await plan_service.resolve_plan(db, await _tenant_plan_id(db, tenant_id=tenant_id))
    nodes: list[TreeKnowledgeBase] = []
    for kb in await knowledge_base_repository.list_by_tenant(db, tenant_id):
        documents = await document_repository.list_by_kb_async(db, kb.id, tenant_id=tenant_id)
        counts = await chunk_repository.count_by_document_ids(
            db,
            [document.id for document in documents],
            tenant_id=tenant_id,
        )
        nodes.append(
            TreeKnowledgeBase(
                kb_id=kb.id,
                name=kb.name,
                split_strategy=kb.split_strategy,
                allowed_formats=formats_of(kb),
                chunk_size=settings_of(kb)["chunk_size"],
                section_part_max_chars=settings_of(kb)["section_part_max_chars"],
                stale_documents=await _stale_documents_of(db, kb, tenant_id=tenant_id),
                token_usage=await _token_usage_of(db, kb_id=kb.id, tenant_id=tenant_id, plan=plan),
                documents=[
                    TreeDocument(
                        document_id=document.id,
                        title=document.title,
                        status=document.status,
                        chunk_count=counts.get(document.id, 0),
                        error_message=document.error_message,
                        source_type=document.source_type,
                        created_at=document.created_at,
                        # 新列（#220）对老行/半截对象的安全默认：`token_count` 在
                        # 迁移回填之前是 0，与 schema 的默认值一致。
                        token_count=int(getattr(document, "token_count", 0) or 0),
                    )
                    for document in documents
                ],
            )
        )
    return nodes


# ── 读模型：库摘要 / 详情一页 / 构建状态（第 17 步 #227）──────────────

#: 详情页默认一页多少行。50 是一屏多一点：翻页成本低，一页也够看。
DEFAULT_DOCUMENTS_PAGE_SIZE: Final[int] = 50
#: 一页最多多少行（防「page_size=100000」把一次响应做成几 MB）。
MAX_DOCUMENTS_PAGE_SIZE: Final[int] = 200


def _isoformat(value: Any) -> str | None:
    """datetime → **带时区**的 ISO 串（``None`` 原样传）。

    为什么一定要带时区：界面拿它算「更新于 14:03:12」，而 ``new Date("2026-11-27T13:33:00")``
    （没有 Z、没有偏移）会被浏览器当成**本地时间**解析。数据库里存的是 naive 的 UTC
    （容器 ``TZ`` 就是 UTC），于是 +08 的浏览器会把 05:33 显示成 05:33——差 8 小时，而这一格
    正是「这份数据有多旧」的唯一证据。所以 naïve 一律按 UTC 贴标签，绝不原样吐出去。
    """

    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


async def _summary_of(
    db: AsyncSession, kb: KnowledgeBase, *, tenant_id: str, plan: PlanSpec
) -> KnowledgeBaseSummaryData:
    """一个库的摘要：四个数字 + 待更新篇数 + 切片合计。**不读文档数组**。

    ## 每库四条查询，但都不是 N+1

    ``count_by_kb_by_status`` / ``count_in_kb`` / ``count_tokens_in_kb`` /
    ``count_stale_generations`` 各是一条**聚合**查询，与库里有几百篇还是几千篇无关。本仓
    禁止的 N+1 是「每篇文档各查一次」——那种成本随文档数线性涨；这里随库数线性涨（十几到
    几十）。真要再压下去得按租户批量聚合（``GROUP BY kb_id`` 四个），收益是常数级、代价是
    四个仓储函数各长一个批量版；在 live 档每 3 秒一次的量级上不值得，记在这里而不是悄悄
    留着。

    ``chunk_count`` 走 :func:`chunk_repository.count_in_kb`（一条聚合），与详情表每一行的
    ``chunk_count`` 共用同一个「当前代 × 当前稿」谓词——卡片上的合计与表格里每行之和对
    得上，是这两个数能被相信的前提。
    """

    counts = await document_repository.count_by_kb_by_status(
        db, str(kb.id), tenant_id=tenant_id
    )
    return KnowledgeBaseSummaryData(
        kb_id=str(kb.id),
        name=kb.name,
        split_strategy=kb.split_strategy,
        allowed_formats=formats_of(kb),
        chunk_size=settings_of(kb)["chunk_size"],
        section_part_max_chars=settings_of(kb)["section_part_max_chars"],
        # 总数取状态分组之和：再单独调一次 `count_by_kb` 就是**两套总数口径**，
        # 而它们只会在「有一篇状态是我们不认识的」时候分叉——那时两个数都「对」，
        # 谁也不报错，用户看到的是「4 篇文档、状态加起来 3 篇」。
        document_count=sum(counts.values()),
        status_counts=DocumentStatusCountsData(
            success=counts.get(int(DocumentStatus.SUCCESS), 0),
            failed=counts.get(int(DocumentStatus.FAILED), 0),
            processing=counts.get(int(DocumentStatus.PROCESSING), 0),
            queued=counts.get(int(DocumentStatus.QUEUED), 0),
        ),
        chunk_count=await chunk_repository.count_in_kb(
            db, str(kb.id), tenant_id=tenant_id
        ),
        # 「N 篇待更新」（现场九·第三步）：按篇比设置指纹。
        stale_documents=await _stale_documents_of(db, kb, tenant_id=tenant_id),
        token_usage=await _token_usage_of(
            db, kb_id=str(kb.id), tenant_id=tenant_id, plan=plan
        ),
    )


async def list_summaries(db: AsyncSession, *, tenant_id: str) -> dict[str, Any]:
    """库列表（卡片网格）：只有摘要。

    与 :func:`list_tree` 的差别就是**没有文档数组**：卡片只显示四个数字，为它把整个租户的
    文档行（一个 5000 篇的库就是 5000 行）读出来再丢掉，是「进详情总要刷新一次」那类问题的
    一半原因——列表与详情共用一份太重、太重的东西，就没法各自定刷新节奏。
    """

    plan = await plan_service.resolve_plan(db, await _tenant_plan_id(db, tenant_id=tenant_id))
    items = [
        await _summary_of(db, kb, tenant_id=tenant_id, plan=plan)
        for kb in await knowledge_base_repository.list_by_tenant(db, tenant_id)
    ]
    return {"items": items, "generated_at": _isoformat(now())}


async def list_documents_page(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    page: int = 1,
    page_size: int = DEFAULT_DOCUMENTS_PAGE_SIZE,
    status: int | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """详情页一次拿全：库摘要 + **一页**文档。

    ## 为什么详情要有自己的请求

    它以前读列表那一份树，而树只在「有在途工作时」每 3 秒刷一次、一旦全终态就**永久停止**。
    于是「进详情 → 数据是几十秒前的 → 只能 F5」不是缓存问题，是**没有自己的请求**：
    详情从来没有「打开即取」这个动作。

    现在它有：打开就取一页（并带上 ``generated_at``），之后按自己的节奏刷新。

    ## 分页是 SQL 层的

    ``total`` 与 ``documents`` 用**同一组条件**（:func:`document_repository._kb_scoped_conditions`），
    所以页数不会与实际行数打架。
    """

    kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    plan = await plan_service.resolve_plan(db, await _tenant_plan_id(db, tenant_id=tenant_id))
    summary = await _summary_of(db, kb, tenant_id=tenant_id, plan=plan)

    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 1), MAX_DOCUMENTS_PAGE_SIZE)
    documents = await document_repository.list_page_by_kb(
        db,
        kb_id,
        tenant_id=tenant_id,
        offset=(safe_page - 1) * safe_size,
        limit=safe_size,
        status=status,
        keyword=keyword,
    )
    counts = await chunk_repository.count_by_document_ids(
        db,
        [str(document.id) for document in documents],
        tenant_id=tenant_id,
    )
    return {
        "kb": summary,
        "documents": [
            KbDocumentRowData(
                document_id=str(document.id),
                title=document.title,
                source_type=document.source_type,
                status=int(document.status),
                chunk_count=counts.get(str(document.id), 0),
                token_count=int(getattr(document, "token_count", 0) or 0),
                error_message=document.error_message,
                current_version=int(getattr(document, "current_version", 1) or 1),
                created_at=_isoformat(document.created_at),
            )
            for document in documents
        ],
        "total": await document_repository.count_by_kb_filtered(
            db, kb_id, tenant_id=tenant_id, status=status, keyword=keyword
        ),
        "page": safe_page,
        "page_size": safe_size,
        "generated_at": _isoformat(now()),
    }


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
    section_part_max_chars: int,
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
        section_part_max_chars=section_part_max_chars,
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


#: 单次「全库重建」最多投多少篇（见 :func:`rebuild_kb` 的模块说明）。
#:
#: 上限存在是因为：全库重建已经不再由服务端记一笔账，它就是 N 条队列消息。一个 5000 篇的库
#: 一键全投会把队列压几十分钟，而操作者在这一刻看到的只是一句「已提交」——出了事也说不清
#: 是这一次点的还是别的原因。300 是「一次点击能在一轮排空内看到反馈」与「不用点几十次」的
#: 折中：再多的篇数分批点，每一批的后果都能单独追溯。
#:
#: 超过上限的**落后篇**这一轮不投；回执里的 ``documents_total`` 仍是**落后篇总数**，所以
#: 「再点一次能推进到剩下的篇」是可核对的。但它不是「还差几次」的计数器：投出去的篇立刻
#: 变成排队中、跑完的篇指纹对齐，两者都不再算落后——可投的落完就没了，再点会回
#: ``20038``（没有需要重建的篇），而不是一直回 ``0 / 0``。
MAX_REBUILD_DOCUMENTS_PER_REQUEST: Final[int] = 300

#: 批量重建投递用的队列名。
#:
#: 与上传 / 单篇重建分开：一次全库重建是 N 条任务、可能连续占满常规 Worker 好几轮，而用户
#: 的上传与单篇重建不该排在它后面。部署侧 ``worker-build`` 只消费这条队列（``-Q build``），
#: 于是「构建再长，也只占它自己那一槽」（ADR-0034 决策八）。**只有批量重建走它**：上传与
#: 单篇重建保持 ``queue=None``（常规队列），否则一次上传也要为「一次全库重建」付隔离的代价。
REBUILD_QUEUE: Final[str] = "build"

#: 可投 0 篇时的对外提示。
#:
#: 与「投递 0 / 0 篇」的回执相比，这个码给了用户一个原因与下一步；前端已有 20038 的翻译。
MSG_KB_NOTHING_TO_REBUILD: Final[str] = (
    "这个库里没有需要重建的文档：没有一篇的产物落后于当前设置"
)


def _stale_filter_of(kb: KnowledgeBase) -> ColumnElement[bool]:
    """这个库「产物落后」的 SQL 判据——**与夜间清扫器同一份**。

    三条并集：``settings_hash IS NULL``（从没记过）、与库当前设置指纹不一致（改过没重建）、
    ``current_index_generation IS NULL``（一片产物都没有）。按库算指纹
    （``settings_fingerprint(build_params_of(kb))``），不把规则写进 SQL——两份规则要能对得上，
    只能共用一个函数；写在 SQL 里的话，改一处忘了另一处不会报错，只会在某个夜里多算一批。

    返回值是 SQLAlchemy 条件，由仓储层原样拼进 ``WHERE``（仓储不做判据）。
    """

    return _stale_predicate(settings_fingerprint(build_params_of(kb)))


async def _stale_rebuild_targets(
    db: AsyncSession, kb: KnowledgeBase, *, tenant_id: str, limit: int | None
) -> tuple[int, list[Document]]:
    """「产物落后」的篇：``(落后总数, 这一批)``；``limit=None`` 取全部。"""

    stale = _stale_filter_of(kb)
    total = await document_repository.count_stale_for_rebuild(
        db, str(kb.id), tenant_id=tenant_id, stale=stale
    )
    documents = await document_repository.list_stale_for_rebuild(
        db, str(kb.id), tenant_id=tenant_id, stale=stale, limit=limit
    )
    return total, documents


async def _dispatch_kb_rebuild(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    tenant_id: str,
    request_id: str | None,
    raise_when_empty: bool = True,
) -> tuple[int, int]:
    """把该库**产物落后**的篇逐篇投进索引队列，返回 ``(已投篇数, 落后篇总数)``。

    ## 判据是「产物是不是最新一代」（与夜间清扫器同一份）

    不再是「库里所有 ``SUCCESS`` 的篇」：那样第二次点击会重新选中同一批最新的篇——它们跑完
    回到 ``SUCCESS`` 之后，又原样落在「最新 300 篇」里，第 301 篇之后永远轮不到；而已建好的
    篇还会被重新切块重嵌入一轮（白烧 embedding，违反 tag #209 用户故事 21「失败重试时已经
    建好的篇不重算」）。判据现在由 :func:`_stale_filter_of` 给出，与
    ``app.services.generation_sweep`` 共用同一个 ``_stale_predicate``。

    只投 ``SUCCESS`` 的篇里**落后**的那批：非 SUCCESS 的篇没有可切的正文（失败篇本来就没切
    出来，排队中 / 索引中的篇已经在队列里），再投一次要么白跑一次解析、要么被
    ``prepare_for_indexing`` 的 CAS 拒绝——所以「全库重建」不提供「连非 SUCCESS 也投」的
    开关（旧版 ``force`` 参数随整库构建闸门一起删除，理由见 :func:`rebuild_kb`）。

    ## 为什么第二次点击自然「继续」，不需要游标 / 偏移

    投出去的篇被置成「排队中」→ 不在只取 ``SUCCESS`` 的落后集合里；跑完的篇指纹对齐、也有
    产物 → 也不在。于是**落后集合自己会缩小**，第二次点击从剩下最旧的篇开始。用偏移量反而
    有洞：前一批跑完变新鲜之后，按 ``offset=300`` 会跳过还没投的篇。

    ## 回执语义（``documents_total``）

    ``total`` 是**落后篇总数**，不是全库 ``SUCCESS`` 篇数：用后者时「跑完的篇还是 SUCCESS」，
    「再点几次」永远算不对。``dispatched < total`` 只代表「这次没投完」——撞上单次上限，或
    个别篇刚被别处投走、CAS 抢不到。它**不代表**还要点几次：可投的落后篇投完了，就是投完了
    （再点一次会 ``20038``）。抢不到（``rowcount=0``）的篇不计入 ``dispatched``：那不是这次
    投的。

    每一篇走的是与人工点「重建」**完全相同**的那条链路（``prepare_for_indexing`` 置排队中、
    不盖租约 → 投递登记在提交之后），只是省掉了逐篇审计：一次全库重建写一条 ``kb.rebuild``
    就够，写 N 条既撑爆账本，也把「这是一次操作」拆成了 N 次看不出关系的改动。

    投递走 ``build`` 队列（:data:`REBUILD_QUEUE`）：批量重建不该把常规队列占满，让用户的上传
    与单篇重建排在它后面（ADR-0034 决策八的隔离承诺；``deploy/`` 里的 ``worker-build`` 就是
    这条队列的唯一消费者）。上传 / 单篇重建仍是常规队列，不付这份隔离代价。

    为什么没有配额入口预检：逐篇预检 = N 次查询（一个 5000 篇的库就是 5000 次往返），而
    Worker 里的真值容量判据仍然照跑（``ensure_kb_token_quota``）。重建不新增内容量，预检
    在这里省掉的只是「注定失败的那一次解析」，代价远大于它挡下的浪费。
    """

    total, documents = await _stale_rebuild_targets(
        db, kb, tenant_id=tenant_id, limit=MAX_REBUILD_DOCUMENTS_PER_REQUEST
    )
    if total == 0 and raise_when_empty:
        # 「点下去什么都没发生」不能回成 200 + 0/0：那在界面上就是一句「这次投递 0 / 0 篇」，
        # 没有原因也没有下一步。20038 的文案与前端翻译已经在，缺的只是这个抛出点。
        #
        # ``raise_when_empty=False`` 只给**改设置**那条路用：一个空库先配好切块策略是正常
        # 操作，没有可重建的篇不代表设置不该落库（那条路径的取舍见 ``update_kb_settings``）。
        raise AppError(ErrorCode.KB_NOTHING_TO_REBUILD, MSG_KB_NOTHING_TO_REBUILD)

    dispatched = 0
    for document in documents:
        prepared = await document_repository.prepare_for_indexing_async(
            db, str(document.id), tenant_id=tenant_id, allow_success=True
        )
        if not prepared:
            # 刚被别处（单篇重建、夜间重投）置成排队 / 索引中：跳过，不重复投。
            continue
        dispatched += 1
        after_commit(
            db,
            # ``partial`` 而不是带默认参数的 lambda：默认参数让 mypy 推不出这个
            # ``Callable`` 的类型（而 ``after_commit`` 需要它可判类型）。
            partial(
                enqueue_document_after_commit,
                publish=enqueue_document,
                document_id=str(document.id),
                tenant_id=tenant_id,
                kb_id=str(kb.id),
                request_id=request_id,
                doc_version=int(document.current_version),
                queue=REBUILD_QUEUE,
            ),
        )
    return dispatched, total


async def _token_usage_of(
    db: AsyncSession, *, kb_id: str, tenant_id: str, plan: PlanSpec
) -> TokenUsageData | None:
    """这个库的 token 容量占用（#221 / 用户故事 #13）。

    ``used`` 是**已落库**的 ``token_count`` 合计（不是现算正文）——它是容量判据真正用的那个
    数，进度条与硬拦必须同源，否则「显示 2.9 万、传上去被拒在 3.1 万」会同时出现。

    空库报 ``0 / 上限``而不是 ``None``：这个数是自己算的，没有「读不到」这回事。
    """

    try:
        used = await document_repository.count_tokens_in_kb(db, kb_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - 容量显示失败不能把整个树打成 500
        logger.warning("kb token usage unavailable", kb_id=kb_id, tenant_id=tenant_id)
        return None
    return TokenUsageData(used=used, limit=int(plan.max_tokens_per_kb))


async def _stale_documents_of(
    db: AsyncSession, kb: KnowledgeBase, *, tenant_id: str
) -> int:
    """这个库「有几篇待更新」（现场九·第三步）。

    口径：文档行上记的 ``settings_hash`` 与**库当前设置**的指纹不一致（或从没记过）。
    一次分组计数，不按篇查——一个库几百篇时那是几百次往返。
    """

    # 指纹由**已加载的这行库**直接算：再走一次 ``get_settings_fingerprint`` 就是一次多余的
    # 主键查询（列表页 N 个库 → N 次），而调用方手里正好有这个对象。
    return await document_repository.count_stale_generations(
        db,
        kb.id,
        tenant_id=tenant_id,
        settings_hash=settings_fingerprint(build_params_of(kb)),
    )


async def settings_preview(db: AsyncSession, *, tenant_id: str, kb_id: str) -> dict[str, Any]:
    """确认弹窗要的那份事实：当前设置 + **这次重建的量**（第 16 步 #207）。

    为什么要单独一次读：确认弹窗里必须写清「这次会重建多少篇、多少片」。让人对着一个
    「确定要重建吗？」点「确定」，等于把一次可能几十分钟的 Worker 变成盲盒——而误点的
    代价是服务器和钱，不是一次刷新。

    ## 两个数用的是哪份判据（与点下去之后的回执同一个）

    ``documents_to_rebuild`` 与 ``_dispatch_kb_rebuild`` 回执里的 ``documents_total`` 是
    **同一个数**：**产物落后**的篇数（判据见 :func:`_stale_filter_of`，与夜间清扫器同一份）。
    用「库里所有 ``SUCCESS`` 篇」会多报——已建好、指纹对齐的篇会被算进「将重建 N 篇」，
    而点下去它们一篇都不会投。预览与回执一旦各算各的，用户就会看到「说 12 篇、投 5 篇」，
    然后不再相信这个弹窗。

    ``chunks_to_rebuild`` 只加**落后那批篇**的当前代切片数：把已建好的篇算进来，会让人以为
    这次要重切一个比实际更大的库。

    没有「生效版本的参数」「有没有构建在跑」这两栏：库级构建已经不存在，重建就是按篇投递
    N 条消息，能不能投只看有多少篇的产物落后、以及单次上限
    （``MAX_REBUILD_DOCUMENTS_PER_REQUEST``）。
    """

    kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    documents = await document_repository.list_stale_for_rebuild(
        db,
        str(kb.id),
        tenant_id=tenant_id,
        stale=_stale_filter_of(kb),
        limit=None,
    )
    counts = await chunk_repository.count_by_document_ids(
        db,
        [document.id for document in documents],
        tenant_id=tenant_id,
    )
    values = settings_of(kb)
    return {
        "kb_id": kb.id,
        "name": kb.name,
        "split_strategy": kb.split_strategy,
        "allowed_formats": formats_of(kb),
        "chunk_size": values["chunk_size"],
        "section_part_max_chars": values["section_part_max_chars"],
        "documents_to_rebuild": len(documents),
        "chunks_to_rebuild": sum(counts.values()),
    }


async def update_kb_settings(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    actor: ConsoleIdentity,
    request_id: str | None,
    split_strategy: str | None,
    chunk_size: int | None,
    section_part_max_chars: int | None,
    confirmed: bool,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """改切块设置并**按篇重建**（第 16 步 #207）。

    切块参数是**索引期**的旋钮：改了不重建，读到的仍是旧切法，而页面看起来「已经改好了」。
    所以这里一次做两件事：写设置 + 逐篇投递重建任务——同一笔事务，投递登记在
    ``after_commit`` 上（先让「已排队」落库，再让 Worker 去抢）。

    **没有「正在构建时拒绝」**：整库构建的状态机已经删除，并发在这里的表现是「同一篇被
    投两次」，而 ``prepare_for_indexing`` 的 CAS 会拒掉后到的那一次——不会有两个 Worker
    同时写同一篇。代价是：改动可以连着做两次，第二次会基于第二次的设置重建（旧任务可能在
    新设置生效后才抢到，于是它也按新设置切——这是「重建幂等」而不是「顺序错乱」）。

    **设置不回滚**：重建失败时这一篇的旧代继续服务（旧代按保留策略还在），设置本身留着
    ——回滚设置会让人以为自己刚才没改成，反而更糊涂。

    ## 没有落后篇时**不拒**（与「点重建」刻意不同）

    设置写完之后判据可能说「一篇都不用重建」（空库、或新设置与旧设置完全相同）。这条路径
    仍然成功落库，回执是 ``0 / 0``：**先给一个空库配好切块策略**是正常操作，把它拒掉会让
    人只能先上传一篇再回来改设置。

    与「点重建」的差别在这里，理由也在这里：点重建是**要求一件要发生的事发生**（没有要发生
    的事就回 ``20038``，否则界面上就是一句没有原因的「投递 0 / 0 篇」）；改设置是**要求在
    改一件将来才生效的配置**，没有可重建的篇只说明「暂时没有证据」，不说明「请求错了」。
    """

    kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    # 显式二次确认：这次改动会触发整库重建（全库重嵌入 + 重写 ES，可能几十分钟的 Worker）。
    # 少了它，一次误点就是一次全库重算——而这个代价是**服务器与钱**，不是一次刷新。
    if not confirmed:
        raise AppError(ErrorCode.PARAM_ERROR, MSG_KB_SETTINGS_NEEDS_CONFIRM)

    before = _kb_snapshot(kb)
    values = settings_of(kb)
    # 只改「这次真的发了」的字段：没发的保持原值，否则一次只想改阈值的请求会把策略也重置。
    new_strategy = kb.split_strategy if split_strategy is None else split_strategy
    new_chunk = values["chunk_size"] if chunk_size is None else chunk_size
    new_part_max = (
        values["section_part_max_chars"]
        if section_part_max_chars is None
        else section_part_max_chars
    )
    try:
        resolve_formats(new_strategy, None)
    except ValueError as exc:
        raise AppError(ErrorCode.PARAM_ERROR, str(exc)) from exc
    if not MIN_CHUNK_SIZE <= new_chunk <= MAX_CHUNK_SIZE:
        raise AppError(
            ErrorCode.PARAM_ERROR, f"chunk_size 必须在 {MIN_CHUNK_SIZE}~{MAX_CHUNK_SIZE} 之间"
        )
    if not MIN_SECTION_PART_MAX_CHARS <= new_part_max <= MAX_SECTION_PART_MAX_CHARS:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"section_part_max_chars 必须在 {MIN_SECTION_PART_MAX_CHARS}~"
            f"{MAX_SECTION_PART_MAX_CHARS} 之间",
        )

    if name is not None:
        kb.name = name
    if description is not None:
        kb.description = description
    kb.split_strategy = new_strategy
    kb.settings = {**values, "chunk_size": new_chunk, "section_part_max_chars": new_part_max}
    await append_audit(
        db,
        actor=actor,
        action=ACTION_KB_SETTINGS_UPDATE,
        target=f"kb:{kb_id}",
        before=before,
        after=_kb_snapshot(kb),
    )

    # 逐篇投递重建：新设置在这批篇被 Worker 抢到的那一刻生效（它们写新一代，旧代照旧
    # 服务——这就是代模型对「改设置」这件事的回答，不需要「等整库切过去」）。
    dispatched, total = await _dispatch_kb_rebuild(
        db,
        kb,
        tenant_id=tenant_id,
        request_id=request_id,
        # 空库 / 全失败库上先配好切块策略是正常操作：没有可重建的篇不该让整笔请求回滚。
        # 「点重建却无事可做」仍然照抛（那条路径见 ``rebuild_kb``）。
        raise_when_empty=False,
    )
    return {
        "kb_id": kb_id,
        "name": kb.name,
        "split_strategy": kb.split_strategy,
        "allowed_formats": formats_of(kb),
        "chunk_size": new_chunk,
        "section_part_max_chars": new_part_max,
        "documents_dispatched": dispatched,
        "documents_total": total,
    }


async def rebuild_kb(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    actor: ConsoleIdentity,
    request_id: str | None,
) -> dict[str, Any]:
    """按**当前的切块设置**重建整个库 = 逐篇投递单篇重建任务（第 17 步）。

    ## 它解决的是「设置改了但没生效」

    切块参数是**索引期**的旋钮，而每篇的产物记着它当时用的参数指纹。两者不一致时列表上会
    挂「N 篇待更新」。让用户自己去猜「怎么才能让它生效」是不合理的，所以给一个显式的入口。

    ## 为什么是「按篇投递」而不是「起一个整库构建」

    库级构建的版本行状态机（building → ready → active / failed、四计数对账、激活、别名
    切换）已经整层删除：一次全库重建不再有「一次操作」这个实体，它就是 N 条消息。这样做的
    代价写在明处：**进度不再由服务端记账**，看的是每篇自己的 ``status``；一次点击就是 N 条
    消息（``_dispatch_kb_rebuild`` 有单次上限，超出的分批再点）；两条消息之间没有「这一批
    何时算完成」的结论，只有每篇各自的成败。

    换来的是「重建期间照旧服务旧内容」不再需要等整库切换：每篇重建完就按新一代服务（这是
    代模型的性质，不是这条入口的功劳）。

    ## 判据与 ``force``

    只投 ``SUCCESS`` 的篇里**产物落后**的那批（判据与夜间清扫器同一份，见
    :func:`_dispatch_kb_rebuild`）。不投已建好的篇：「全库重建」在这个改造之后唯一的含义是
    **让每篇的产物变新**，不是「把所有篇重算一遍」——后者会白烧一轮 embedding，而结果与
    现在完全一样。旧版的 ``force``（「连有效处理中的也重建」）随整库构建的并发闸门一起
    删除：那时它绕的是「同一库只能有一个构建在跑」；现在没有闸门，非 SUCCESS 的篇（失败篇
    没有可切的正文、排队 / 索引中的篇本来就在队列里）再投一次只会白跑或撞 CAS。想让失败篇
    重来，走单篇重建（``doc/reindex``，那条入口有 force 语义）。

    ## 上限与「再点一次」

    一次最多投 ``MAX_REBUILD_DOCUMENTS_PER_REQUEST`` 篇落后篇（理由见那个常量）。超出的篇
    这一轮不投，回执里的 ``documents_total`` 仍是**落后篇总数**——再点一次会从剩下的落后篇
    继续，因为投出去的篇已经不在落后集合里了（不需要游标 / 偏移，理由见
    :func:`_dispatch_kb_rebuild`）。库里一篇落后都没有时回 ``20038``（没有需要重建的篇），
    不是一个看起来成功的 ``0 / 0``。
    """

    kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    dispatched, total = await _dispatch_kb_rebuild(
        db, kb, tenant_id=tenant_id, request_id=request_id
    )
    await append_audit(
        db,
        actor=actor,
        action=ACTION_KB_REBUILD,
        target=f"kb:{kb_id}",
        # 设置本身没变，所以只记「这一次投了多少、落后篇一共多少」——事后能回答「那次点了
        # 多少篇、还剩多少落后篇」，而 total 是落后篇数（不是全库成功篇数，理由见
        # `_dispatch_kb_rebuild` 的回执语义）。
        before=None,
        after={
            "kb_id": kb_id,
            "documents_dispatched": dispatched,
            "documents_total": total,
            "batch_limit": MAX_REBUILD_DOCUMENTS_PER_REQUEST,
        },
    )
    return {
        "kb_id": kb_id,
        "documents_dispatched": dispatched,
        "documents_total": total,
    }


async def delete_empty_kb(
    db: AsyncSession, *, tenant_id: str, kb_id: str, actor: ConsoleIdentity
) -> dict[str, Any]:
    """删库：仅空库。库内仍有文档 → ``20015``（提示先删文档）；缺失/跨租户 → ``20008``。"""

    kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
    if kb is None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    if await document_repository.count_by_kb(db, kb_id, tenant_id=tenant_id) > 0:
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
    """上传文档：登记 + 投递复用 ``document_service.upload``，立即回 ``status=4``（排队中）。

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
    """单文档重建：守卫 → 准备入队 → 审计（同一笔事务）→ 投递，立即回 ``status=4``。

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

    # 没有配额门禁：重建跳过「单库文档数」（文档本来就在库里），也不再数「同时处理中篇数」
    # ——那条配额已随 #218 删除，理由与代价见 ADR-0034 决策八（没有按租户的公平调度，
    # 批量提交会占满常规 Worker；缓解是**批量重建**走专用队列 ``build``
    # （``_dispatch_kb_rebuild`` 那条路径），单篇重建与上传仍走常规队列——这里就是单篇，
    # 所以它只和「另一篇重建 / 一次上传」抢槽位，不会被一整库的重建压住）。
    # ── 入口预检（#220）：库已经装满了就不再收新的重建请求 ──
    #
    # 重建是「重新算这一篇」，不新增容量，所以**不排除它自己**——它占的空间要算它自己的
    # （`excluding_document_id`）。但一条重建也可能落到一个早就超限的存量库上（那是 #220
    # 允许存在的状态：超限的库仍然能检索），此时放它进来只会白解析一次、然后必然失败。
    #
    # 真值判据（解析后、知道本篇多少 token 那一次）仍在 Worker 里，这一道只是把
    # 「注定失败」挡在代价最低的地方。
    plan = await plan_service.resolve_plan(db, await _tenant_plan_id(db, tenant_id=tenant_id))
    await static_quota.ensure_kb_token_quota(
        db,
        tenant_id=tenant_id,
        kb_id=document.kb_id,
        plan=plan,
        excluding_document_id=document.id,
    )
    # 日额度入口预检（#222）：额度早用尽的租户不该为一次注定失败的解析排两分钟队。
    # 这就是「等明天重置」与「删文档」两种处置的**分口**——两件事各有各的码与两句话。
    await chunk_ledger.ensure_chunk_daily_quota(
        tenant_id=tenant_id,
        limit=plan.chunk_daily_tokens,
        plan_name=f"{plan.name}（{plan.code}）",
    )

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
            "status": DocumentStatus.QUEUED,
            "reparse": reparse,
            "force": force,
        },
    )
    after_commit(
        db,
        lambda: enqueue_document_after_commit(
            publish=enqueue_document,
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=document.kb_id,
            request_id=request_id,
            reparse=reparse,
            # 与数据面同一条约定（#173）：这次重建算哪一稿在投递那一刻从指针取下来，
            # Worker 不再二次确认，迟到的任务因此不会把别的稿写进来。
            doc_version=document.current_version,
        ),
    )
    return {
        "document_id": document_id,
        "status": DocumentStatus.QUEUED,
        "chunk_count": 0,
        "reparse": reparse,
    }


async def delete_document(
    db: AsyncSession, *, tenant_id: str, document_id: str, actor: ConsoleIdentity
) -> dict[str, Any]:
    """删除文档：守卫 → purge 切片 → 删原文件 → 删版本行 → 删行 → 审计 → 由请求边界提交。

    顺序是契约的一部分（见 ``app/api/routes/documents.py`` 的同名端点）；审计插在
    「删行」与「提交」之间，不改动五步本身。有效处理中（租约未过期）拒绝删除；
    **排队中的文档可以直接删**（等于撤回排队，迟到的任务抢占不到，ADR-0034 决策一）；
    租约过期视为 Worker 卡死，同样允许删除。

    版本行单独删一次（#172）：一张表都没有外键，库不会级联，所以与清除切片、删行
    同一笔事务里显式删（``document_version_repository.delete_for_document``）。
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
    await document_version_repository.delete_for_document(db, document_id)

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


# ── 一处刻意的「不加门」────────────────────────────────────────────────────
#
# 建版本（新稿）与回滚（切回旧版本）**没有**容量与日额度的入口预检，这不是漏的：
#
# * 建版本是「同一份内容换一个稿号」，库里的总 token 量**不变**；
# * 回滚是把**当前稿指针**拨回旧稿（不回填旧产物、也不复制旧切片），库里一个字节都没多也没少。
#
# 给它们加容量门禁的后果很具体：一个已经超限的存量库（#220 明确允许它继续存在、继续检索），
# 用户在那个时候**恰恰最需要**回滚到一个容量更小的旧版本——而门禁会把他拒在门外。这不是
# 「更严格」，是把一条救援路径封死了。
#
# 真正会新增内容量的入口只有两个：**上传**（`ensure_upload_quota`）与**重建**
# （`reindex_document` 里的那两道门）。门只加在「会让总量变大」的地方。


async def _tenant_plan_id(db: AsyncSession, *, tenant_id: str) -> int:
    """会话租户 → 它当前的套餐 id；租户不在就 ``20010``（与数据面同一句提示）。"""

    tenant = await tenant_repository.get_by_id_async(db, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_TENANT_NOT_FOUND)
    return tenant.plan_id


async def _plan_for_tenant(db: AsyncSession, *, tenant_id: str, cache: PlanCache) -> PlanSpec:
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
        "section_part_max_chars": settings_of(kb)["section_part_max_chars"],
    }


def _kb_snapshot_of_data(data: dict[str, Any]) -> dict[str, Any]:
    """建库回执 → 与 :func:`_kb_snapshot` 同一形状的审计快照。"""

    return {
        "kb_id": data["kb_id"],
        "name": data["name"],
        "split_strategy": data["split_strategy"],
        "allowed_formats": list(data["allowed_formats"]),
        "chunk_size": data["chunk_size"],
        "section_part_max_chars": data["section_part_max_chars"],
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


async def list_document_versions(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    include_content: bool = False,
) -> dict[str, Any]:
    """列一篇文档的全部稿次（控制面**只读**：不改状态、不写审计、不碰索引）。

    租户只来自会话；按 ``(id, tenant_id)`` 取不到文档就是「不存在」（20016），跨租户
    同样如此——**不区分「没有」与「不是你的」**，可区分的拒绝会泄露资源存在性。

    出参形状交给 :func:`app.services.document_version_service.version_list_data`：数据面
    那条列表接口调的是**同一个函数**，于是「控制面多一列、数据面少一列」这种漂移没有
    机会发生。读路径不声明事务依赖（与树、原文两条读接口同一条：读不提交）。
    """

    document = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if document is None:
        raise AppError(ErrorCode.NOT_FOUND)

    rows = await document_version_repository.list_by_document(db, document_id)
    return document_version_service.version_list_data(
        document, rows, include_content=include_content
    )


async def rollback_document(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    version: int,
    actor: ConsoleIdentity,
    cache: PlanCache,
    request_id: str | None,
) -> dict[str, Any]:
    """把当前稿指针拨回第 ``version`` 稿：**整段委托** ``document_version_service``。

    控制面这一段只做三件事，一件都不多做：

    1. 会话租户 → 套餐快照（回滚也是一次重建，同样要过「并发处理数」配额）；
    2. :func:`app.services.document_version_service.rollback_to_version` —— 切指针、当前稿
       转归档、目标稿转当前稿、同步文档正文与源文件、置处理中，以及提交之后投递重建 +
       翻转关键词索引的当前稿标记，全部在那一份实现里；
    3. 写一条账本（``doc.rollback``），与业务写入同一笔事务、由请求边界提交。

    **这里不许出现第二份「切指针 + 归档 + 投递」**：那是本步最贵的一段顺序（见
    ``document_version_service`` 的模块文档），两份实现漂移的症状是「从面板回滚的文档
    检索不到」。所以状态守卫（20025）、稿号判定（20030 / 20032）与被拒时的「什么都不写」
    全部由被委托的服务给出，这里**不重复判一次**——重复判只会让两处迟早不一致。

    账本记的是**指针的变化**（before / after 各一个稿号），不重复记标题与库：标题属于
    文档本身，不在这次改动范围内；回执里的 ``previous_version`` 正好是「改前」的真值，
    也就不必为了账本再读一次文档行。
    """

    # 回滚不再取套餐快照：它的那道门禁（同时处理中篇数）已随 #218 删除（ADR-0034 决策八）。
    data = await document_version_service.rollback_to_version(
        db,
        tenant_id=tenant_id,
        document_id=document_id,
        version=version,
        request_id=request_id,
    )
    await append_audit(
        db,
        actor=actor,
        action=ACTION_DOC_ROLLBACK,
        target=f"doc:{document_id}",
        before={"document_id": document_id, "current_version": data["previous_version"]},
        after={
            "document_id": document_id,
            "current_version": data["version"],
            "status": data["status"],
        },
    )
    return data


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
