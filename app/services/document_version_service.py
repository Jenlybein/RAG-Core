"""建版本：把一份新正文变成文档的下一个稿次（#175，ADR-0005 D2 / D3 / D5）。

这是文档版本管理的第一条**用户可见**写路径：提交一份正文（JSON 或文件），得到一个新稿次；
提交与当前稿相同的东西被判「内容未变」；两个提交撞上同一个稿号时回「冲突，请重试」。

## 顺序是契约（一步都不能颠倒）

1. **取文档**（按 id + 租户）：缺失与跨租户统一「不存在」，不泄露存在性；
2. **处理中则拒**：有效租约内的 ``PROCESSING`` 说明 Worker 正在写这篇文档的切片，
   这时切稿会让「哪一稿的切片」变成掷骰子；
3. **拿正文**：JSON 入口直接给；文件入口走**既有解析入口**（
   :func:`app.services.document_ingestion.parse_uploaded_bytes`），不在这里按扩展名分流；
4. **规范化 + 算校验和**：唯一入口 :mod:`app.utils.checksum`。判重、行里的 ``checksum``、
   之后的对账都读这一份（两套规范化迟早给出「同一份正文两个校验和」）；
5. **与当前稿比对**（在拿锁之前）：相同就回「内容未变」，**什么都不写**——不值得为一次
   没有改动的提交去锁文档行；
6. **锁文档行**（``SELECT ... FOR UPDATE``）：稿次时间轴就是文档行上的当前稿指针，锁它
   就等于锁住这条时间轴。锁内复核一次租约（守卫与锁之间的时间差里 Worker 可能刚好开工）；
7. **稿号 = max + 1**：算在锁内，后一个提交看到的一定是前一个写下的行；
8. **写新版本行（当前稿）+ 旧当前稿转归档 + 指针前移 + 文档正文同步 + 置处理中**；
9. **``flush``**：让 ``(document_id, version)`` 唯一约束在写路径里撞出来，映射成
   「冲突，请重试」（20029）。拖到提交才撞就只剩 500 了。

## 为什么判重在锁外、而写全在锁内

判重是**读**：它只决定「这次提交要不要继续」。放在锁外，一次重复提交不会排队等锁；
而锁一旦拿到，读到的一切（``max``、指针、租约）都必须是别人的提交已经落地的那个值。

## 副作用在 ``after_commit``

重建任务投递与关键词索引的「当前稿」翻转都登记在提交**之后**（见
:func:`_queue_side_effects`）：库先落地、副作用后发生。反过来的话，Worker 可能去读一行
还没提交的文档，而关键词索引会指向一个还不存在的稿次。

## 不做的事

回滚（#176）、按版本直查（#176）、保留策略（#178）。本模块只负责「往前长一稿」。
"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import after_commit
from app.models.document_version import DOCUMENT_VERSION_CURRENT, DocumentVersion
from app.models.plan import PlanSpec
from app.repositories import document_repository, document_version_repository
from app.services import static_quota
from app.services.document_ingestion import parse_uploaded_bytes
from app.services.document_state import ensure_versionable
from app.services.document_storage import store_version_file
from app.utils.checksum import checksum_of, normalize_text
from app.utils.id_generator import new_version_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)


async def create_version(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    plan: PlanSpec,
    content: str = "",
    filename: str | None = None,
    data: bytes = b"",
    request_id: str | None = None,
) -> dict:
    """提交一份新正文，返回可直接回给调用方的回执（顺序见模块文档）。

    两种模式由入口按 content-type 决定：``filename`` 非空 = 文件模式（正文由**既有解析
    入口**解析上传的字节），否则 JSON 模式（``content`` 直接用）。

    ``plan`` 是静态配额要用的套餐快照：一次提交等于往队列里塞一份解析任务，和重建那条
    路径一样必须过「并发处理数」——这是唯一一条「不超自己的上限也会伤害别人」的配额。
    """

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 处理中则拒：Worker 正在写这篇文档的切片，这时切稿会让「哪一稿的切片」变成掷骰子。
    ensure_versionable(doc)
    # 静态配额紧跟在状态守卫之后、**写任何东西之前**：被拒的提交既不建行也不入队。
    await static_quota.ensure_processing_quota(db, tenant_id=tenant_id, plan=plan)

    text = await _resolve_text(filename=filename, content=content, data=data)
    checksum = checksum_of(text)
    if not text:
        raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "提交的正文为空")

    baseline = await _current_draft_checksum(db, doc)
    if baseline == checksum:
        # 什么都不写：连指针、状态、时间戳都不动。调用方要的就是「没变」这个答案。
        logger.info(
            "document version unchanged",
            document_id=document_id,
            tenant_id=tenant_id,
            doc_version=doc.current_version,
        )
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_UNCHANGED,
            "提交的正文与当前稿相同，未创建新版本",
            data={"version": doc.current_version},
        )

    # 锁内的一切才是真值：守卫与锁之间，Worker 可能刚好开工、另一个提交可能刚好切了指针。
    locked = await document_repository.lock_for_version(
        db, document_id, tenant_id=tenant_id
    )
    if locked is None:
        raise AppError(ErrorCode.NOT_FOUND)
    ensure_versionable(locked)

    previous_version = locked.current_version
    version = await document_version_repository.max_version(db, document_id) + 1

    source_file_path: str | None = None
    if filename is not None:
        # 源文件按**稿号**落盘：两份文件抄同一个路径，重解析第 1 稿时会读到第 2 稿的字节。
        source_file_path, source_filename = store_version_file(
            tenant_id=tenant_id,
            document_id=document_id,
            version=version,
            filename=filename,
            data=data,
        )
        locked.source_file_path = source_file_path
        locked.source_filename = source_filename

    try:
        await document_version_repository.create(
            db,
            DocumentVersion(
                id=new_version_id(),
                document_id=document_id,
                version=version,
                content=text,
                source_file_path=source_file_path,
                checksum=checksum,
                status=DOCUMENT_VERSION_CURRENT,
            ),
        )
    except IntegrityError:
        # 稿号被别人抢了：这是**可重试**的业务失败，不是服务异常（ADR-0007 的分段）。
        # 事务由请求边界回滚，调用方换个号再来一次即可。
        logger.warning(
            "document version conflict",
            document_id=document_id,
            tenant_id=tenant_id,
            doc_version=version,
        )
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_CONFLICT,
            "稿号冲突，请重试",
            data={"document_id": document_id, "version": version},
        ) from None

    # 旧当前稿转归档（不复活、不删除：那是 #176 回滚与 #178 保留策略的事）。
    await document_version_repository.archive_current_except(
        db, document_id, keep=version
    )

    # 指针前移 + 文档正文同步（D6：文档行是当前稿的热路径快照）+ 置处理中。
    locked.current_version = version
    locked.content = text
    locked.status = DocumentStatus.PROCESSING
    # 清掉上一轮的失败文案与租约：「已排队、未开工」才是这次提交之后的状态，
    # Worker 仍是唯一的抢占者（它进来时才盖 index_claimed_at）。
    locked.error_message = None
    locked.index_claimed_at = None
    await db.flush()

    _queue_side_effects(
        db,
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=locked.kb_id,
        request_id=request_id,
        doc_version=version,
    )

    logger.info(
        "document version created",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=locked.kb_id,
        doc_version=version,
        previous_version=previous_version,
        is_file=filename is not None,
    )
    return {
        "document_id": document_id,
        "version": version,
        "previous_version": previous_version,
        "checksum": checksum,
        "status": DocumentStatus.PROCESSING,
    }


async def _resolve_text(*, filename: str | None, content: str, data: bytes) -> str:
    """把这次提交变成「要存进版本行的那一份正文」（规范化之后的）。

    文件模式走 :func:`app.services.document_ingestion.parse_uploaded_bytes`——**唯一解析
    入口**，不在服务里按扩展名分流：分流写两遍的下场是「同一个文件在两个入口解析出不同
    正文」，而那个差异没有任何地方会报错。
    """

    if filename is not None:
        parsed = parse_uploaded_bytes(data, filename=filename)
        return normalize_text(parsed.content)
    return normalize_text(content)


async def _current_draft_checksum(db: AsyncSession, doc) -> str:
    """当前稿的校验和：**版本行优先**，没有版本行才回落文档行的正文（#173 的同一口径）。

    回落是给 #172 之前的存量文档用的：它们还没建过版本行，``documents.content`` 是唯一
    的正文。无条件收紧成「没有版本行就报错」会打挂上传链路（刚上传的文档本来就没有
    版本行）——首个版本行由索引成功那一笔补上（见 ``IndexingService``）。
    """

    current = await document_version_repository.get_by_version(
        db, str(doc.id), doc.current_version
    )
    if current is not None:
        return current.checksum
    return checksum_of(doc.content or "")


def _queue_side_effects(
    db: AsyncSession,
    *,
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    doc_version: int,
) -> None:
    """登记「提交之后」的副作用：目前是投递重建（带稿号）。

    **排在提交之后**：库先落地、副作用后发生。反过来的话，Worker 可能去读一行还没提交
    的文档，任务跑得比事务快时看到的是旧稿。

    稿号在**投递那一刻**固定下来（#173）：Worker 不再回读指针，于是迟到的任务也不会写错稿。
    """

    from app.services.index_dispatch import enqueue_document, enqueue_document_after_commit

    after_commit(
        db,
        lambda: enqueue_document_after_commit(
            publish=enqueue_document,
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            request_id=request_id,
            reparse=False,
            # 这次重建算的是**刚建好的那一稿**：库里的指针已经前移，但迟到的读取可能还没看到，
            # 所以随载荷带走。
            doc_version=doc_version,
        ),
    )
