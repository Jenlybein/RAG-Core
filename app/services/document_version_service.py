"""建版本与回滚：把一份新正文变成下一个稿次，或把当前稿指针拨回某一旧稿
（#175 / #176，ADR-0005 D2 / D3 / D5）。

建版本是文档版本管理的第一条**用户可见**写路径：提交一份正文（JSON 或文件），得到一个新
稿次；提交与当前稿相同的东西被判「内容未变」；两个提交撞上同一个稿号时回「冲突，请重试」。
回滚是第二条：把指针拨回任意仍在保留窗口内的稿次，并让这篇文档**立刻能被检索到**。

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

按版本直查（#176 的读侧，见 :mod:`app.services.version_search`）、保留策略（#178）。
本模块只负责「往前长一稿」与「指针拨回旧稿」。
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


async def rollback_to_version(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    version: int,
    plan: PlanSpec,
    request_id: str | None = None,
) -> dict:
    """把当前稿指针拨回第 ``version`` 稿，并让这篇文档立刻回到「等重建」的状态。

    ## 回滚 = 一笔事务里的四件事，外加提交之后的两件

    1. **当前稿转归档**：时间轴只有一条「当前」，两行同时 ``current`` 会让「这篇文档的
       当前稿切片」有两个答案（检索按 ``chunks.doc_version = documents.current_version``
       过滤，指针只有一个，但版本行是派生真值——它必须与指针一致）；
    2. **目标稿转当前稿**：目标是一行**已存在的归档稿**，不是新写一行。回滚不产生新稿
       次：稿号是「这一稿存在过」的身份，回滚是「重新选中它」，时间轴因此不增长
       （与「重放旧正文要建新稿次」是两件事）；
    3. **指针前移（这里其实是后移）**：``documents.current_version = version``。指针是
       唯一真值（见 ``app.models.chunk.current_draft_filter``）；
    4. **文档正文同步为目标稿正文 + 置处理中**：文档行是当前稿的热路径快照（D6）。源文件
       路径也一起换（见下）。置处理中是因为切片要重建——此刻库里的切片还是上一稿的。
    5. **提交之后**：投递重建（载荷带**目标稿号**）+ 翻转关键词索引的「当前稿」标记。
       两件都在 :func:`_queue_side_effects` 里登记。

    ## 为什么一律重建（不做「条件重建」）

    第一版正确优先：切片还在、索引版本也一致时跳过重建可以省一次解析，但判据本身要读
    切片表 + 索引版本 + 稿号三处状态，而任何一处判错都表现为「回滚成功、检索里还是旧
    内容」——那正是本票要消灭的症状。代价只是多一次重建。

    ## 为什么源文件路径跟着换

    ``documents.source_file_path`` 是**当前稿**的源文件（#175 起按稿号落盘：
    ``<doc_id>.v<N><ext>``）。回滚不换它，之后一次 ``reparse=true`` 的重建会拿**上一稿**
    的字节重新解析，而那份字节看起来完全正常、错误要通过内容差异才能发现。

    ``source_filename`` 不换：它是文档级别的「原始上传时叫什么」，版本行只记路径，
    没有可回填的真值。

    ## 守卫与锁的顺序

    先按 ``(id, tenant_id)`` 取文档（缺失与跨租户统一「不存在」，20016）→ 状态守卫
    （有效处理中 20025）→ 静态配额（回滚同样往队列塞一份解析任务）→ **锁文档行**。
    锁内复核一次守卫与指针：守卫与锁之间的窗口里，Worker 可能刚好开工、另一个请求可能
    刚好切了稿。目标稿的读取也在锁内——锁外读到的那一行可能正被保留策略删掉。

    """  # noqa: D401 - 句型统一

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    # 处理中则拒：Worker 正在写这篇文档的切片，这时切指针会让「哪一稿的切片」变成掷骰子。
    ensure_versionable(doc)
    # 静态配额：回滚也是一次重建，同样要过「并发处理数」——与建版本 / 重建同一条口径。
    await static_quota.ensure_processing_quota(db, tenant_id=tenant_id, plan=plan)

    # 锁内的一切才是真值（守卫与锁之间可能有人开工、有人切了指针）。
    locked = await document_repository.lock_for_version(
        db, document_id, tenant_id=tenant_id
    )
    if locked is None:
        raise AppError(ErrorCode.NOT_FOUND)
    ensure_versionable(locked)

    target = await document_version_repository.get_by_version(db, document_id, version)
    if target is None:
        # 已经被保留策略物理删除（#178）：「这一稿不存在」，**不做墓碑**。
        # 补一行墓碑会把「删过」变永久数据，而墓碑本身还要参与稿号 / 保留窗口的判定。
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_NOT_FOUND,
            "要回滚到的稿次不存在",
            data={"version": version},
        )

    previous_version = locked.current_version
    if version == previous_version:
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_ALREADY_ACTIVE,
            "这一稿已经是当前稿",
            data={"version": version},
        )

    # ① 当前稿转归档 ② 目标稿转当前稿。顺序不能反：先归档会把「除 keep 之外」的当前稿
    # 全打掉，再选中目标；反过来的话目标行在中间那一刻是孤儿（两行都不是当前稿）。
    await document_version_repository.archive_current_except(
        db, document_id, keep=version
    )
    await document_version_repository.mark_current(db, document_id, version=version)

    # ③ 指针前移 ④ 文档正文 / 源文件同步为目标稿 + 置处理中。
    locked.current_version = version
    locked.content = target.content
    locked.source_file_path = target.source_file_path
    locked.status = DocumentStatus.PROCESSING
    locked.error_message = None
    locked.index_claimed_at = None
    await db.flush()

    # ⑤ 投递重建（带目标稿号）⑥ 翻转关键词索引的当前稿标记。
    _queue_side_effects(
        db,
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=locked.kb_id,
        request_id=request_id,
        doc_version=version,
    )

    logger.info(
        "document version rolled back",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=locked.kb_id,
        doc_version=version,
        previous_version=previous_version,
    )
    return {
        "document_id": document_id,
        "version": version,
        "previous_version": previous_version,
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
    """登记「提交之后」的两件副作用：投递重建（带稿号）+ 翻转关键词索引的当前稿标记。

    **都排在提交之后**：库先落地、副作用后发生。反过来的话，Worker 可能去读一行还没提交
    的文档，而关键词索引会指向一个还不存在的稿次（那一刻检索按 ``is_current`` 过滤，
    命中的切片属于一份数据库里查不到的稿）。

    稿号在**投递那一刻**固定下来（#173）：Worker 不再回读指针，于是迟到的任务也不会写错稿。
    两件副作用的失败语义不同：投递失败就是这次提交失败（异常照抛）；翻转失败只降级
    （见 ``app.services.keyword_draft``）——索引里的标记是派生视图，不是真值。
    """

    from app.services.index_dispatch import enqueue_document, enqueue_document_after_commit
    from app.services.keyword_draft import flip_current_draft_after_commit

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
    after_commit(
        db,
        lambda: flip_current_draft_after_commit(
            document_id=document_id,
            tenant_id=tenant_id,
            doc_version=doc_version,
        ),
    )
