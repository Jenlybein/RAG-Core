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

按版本直查（#176 的读侧，见 :mod:`app.services.version_search`）。本模块负责「往前长一稿」
（:func:`create_version`）、「把指针拨回旧稿」（:func:`rollback_to_version`，#176）与「把窗口
外的稿子清掉」（:func:`purge_expired_versions`，#178）——三者是同一条时间轴的不同操作，
所以留在同一个服务里。

## 保留窗口（#178）

留最近 :data:`RETAIN_VERSIONS` 稿，窗口外的**归档**稿连切片带版本行一起清掉。三条口径：

* 窗口按**稿数**算（``当前稿号 - 保留稿数``），不按时间：一天改十稿的文档不会保留几十个
  工作日的切片；
* 只删 ``archived``，**当前稿永不删**——哪怕它是 v1 而指针在 v9（库里残留的低稿号当前行）；
* 删除走两维键：``doc_version=这一稿`` + ``index_version=None``。这一稿的切片可能散落在
  **多个索引版本**里（每重建一次写一套），只删当前索引版本会留下旧索引版本下的幽灵切片，
  而它们永远不会被任何查询命中（检索 pin 在生效版本上），只会一直占着存储。

清理是**幂等**的：跑第二遍没有候选、不发语句。触发靠运维脚本
（``scripts/purge_document_versions.py``），本模块不引入任何调度。

**与回滚的关系**（#176 的「稿不存在」正是靠它造出来的）：清理会把这些稿的版本行整行删掉，
之后拿这个稿号回滚 / 直查只会得到「稿不存在」（20030）——不做墓碑。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import after_commit
from app.models.document import Document
from app.models.document_version import DOCUMENT_VERSION_CURRENT, DocumentVersion
from app.models.plan import PlanSpec
from app.repositories import document_repository, document_version_repository
from app.services import static_quota
from app.services.chunk_store import ChunkStore
from app.services.document_ingestion import parse_uploaded_bytes
from app.services.document_state import ensure_versionable
from app.services.document_storage import store_version_file
from app.utils.checksum import checksum_of, normalize_text
from app.utils.id_generator import new_version_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

#: 默认保留稿数（#178 / D10）。这是**业务参数**，不是技术参数：它决定「审计能回查几稿」，
#: 所以是源码里的常量，不读环境变量、不进配置中心。运维脚本的 ``--retain`` 可以按库 / 按
#: 次覆盖它（一次性动作），但默认值只有这一处。
RETAIN_VERSIONS = 3


@dataclass(frozen=True)
class RetentionPlan:
    """一次保留清理的**判定结果**（要清哪几稿），不含「删没删」。

    与「执行」分开，dry-run 才有东西可打印：脚本不写库时拿到的就是这一份，于是
    「dry-run 看到什么」与「``--apply`` 删什么」由同一段计算回答，不会两处漂移。
    """

    document_id: str
    #: 这篇文档的当前稿指针（窗口的起点）。
    current_version: int
    #: 这次生效的保留稿数（默认 :data:`RETAIN_VERSIONS`，脚本 ``--retain`` 可覆盖）。
    retain: int
    #: 窗口下界（含）：稿号 <= 它的归档稿在窗口外。
    floor: int
    #: 要清掉的稿号，**最新的排在前面**（稿号倒序；dry-run 先打印到近期那几稿）。
    expired_versions: tuple[int, ...]

    @property
    def is_empty(self) -> bool:
        """这一篇没有过期稿（窗口内 / 还没长到窗口外）。"""

        return not self.expired_versions

    @property
    def expired_count(self) -> int:
        """要清掉几稿。"""

        return len(self.expired_versions)


def retention_floor(current_version: int, retain: int = RETAIN_VERSIONS) -> int:
    """窗口下界（含）：稿号 ``<=`` 它的归档稿在窗口外。

    按**稿数**算（``当前稿号 - 保留稿数``），与时间无关：窗口是稿号轴上的一段，不是日历上
    的一段。返回值可以 <= 0（这篇文档还没长到窗口外），此时任何稿号都不该被清——
    查询那一侧因此也不必发出去。
    """

    return current_version - retain


def retention_plan(
    *,
    document_id: str,
    current_version: int,
    archived_versions: Iterable[int],
    retain: int = RETAIN_VERSIONS,
) -> RetentionPlan:
    """由「当前稿号 + 这篇文档的归档稿号」算出要清哪几稿（纯函数，不碰库）。

    只在这里做窗口判定，调用方（服务 / 脚本）不重复实现一遍减法：两份判定迟早对不上，
    而症状是「dry-run 说要删 3 稿、``--apply`` 删了 2 稿」。

    顺序按稿号**倒序（最新的在前）**，与仓储的查询顺序（``list_archived_at_or_below`` 也是
    稿号倒序）一致：脚本的 dry-run 把这一份原样打印出来，先看到的是**最近**要清的那几稿，
    运维对着「这篇文档最近改过什么」读它，比从最老的一稿往上读更对得上直觉；``--apply``
    也按这个顺序逐稿去清切片，日志里「清到哪一稿」与 dry-run 的打印顺序逐字对得上。
    """

    floor = retention_floor(current_version, retain)
    expired = tuple(sorted((v for v in archived_versions if v <= floor), reverse=True))
    return RetentionPlan(
        document_id=document_id,
        current_version=current_version,
        retain=retain,
        floor=floor,
        expired_versions=expired,
    )


async def plan_retention(
    db: AsyncSession, *, document: Document, retain: int = RETAIN_VERSIONS
) -> RetentionPlan:
    """读出这篇文档的归档稿，算出这次要清哪几稿（**只读**，dry-run 用它）。"""

    plan = retention_plan(
        document_id=str(document.id),
        current_version=document.current_version,
        archived_versions=(),
        retain=retain,
    )
    if plan.floor < 1:
        # 窗口下界 <= 0：稿号从 1 起算，任何一稿都在窗口内。跳过那次查询。
        return plan
    rows = await document_version_repository.list_archived_at_or_below(
        db, plan.document_id, version=plan.floor
    )
    return retention_plan(
        document_id=plan.document_id,
        current_version=document.current_version,
        archived_versions=[row.version for row in rows],
        retain=retain,
    )


async def purge_expired_versions(
    db: AsyncSession,
    *,
    document: Document,
    retain: int = RETAIN_VERSIONS,
    stores: Sequence[ChunkStore] | None = None,
) -> RetentionPlan:
    """把窗口外的归档稿**连切片带版本行**清掉，返回这次清掉的判定（不提交）。

    顺序是契约，一步都不能颠倒：

    1. 先算窗口（:func:`plan_retention`）；
    2. **先删切片**：逐稿调 :func:`app.services.indexing_service.purge_document_chunks`，
       它遍历 ``stores``（默认接线是向量存储 + 关键词检索两个存储）；
    3. **再删版本行**：一条 ``DELETE ... IN (...)`` 打整批。

    反过来的顺序一旦回滚，留下的是「切片还在、版本行没了」——那批切片永远不被任何查询
    命中，也无法再被定位到属于哪一稿，比半份更坏。事务边界在调用方（脚本按**每篇文档
    一笔** ``async with async_transaction(db)``），失败时整笔回滚，不留半份。

    **删除键是两维的**：``doc_version=这一稿`` 且 ``index_version=None``。这一稿的切片
    可能散落在多个索引版本里（每重建一次写一套），只删当前索引版本会留下旧索引版本下的
    幽灵切片——检索 pin 在生效版本上，它们永远不会被召回，却会一直占着向量库与关键词
    索引。反过来「稿号不收窄」更不行：那会把还在窗口内的稿一起清掉。

    ``stores`` 只在测试里注入（默认按 ``build_chunk_stores`` 现建）。没有候选稿时连存储
    都不建：清理脚本在「没什么可清」的库上不该去连向量库 / 关键词引擎。
    """

    plan = await plan_retention(db, document=document, retain=retain)
    if plan.is_empty:
        return plan

    from app.services.indexing_service import purge_document_chunks

    resolved = list(stores) if stores is not None else _default_chunk_stores()
    tenant_id = str(document.tenant_id)
    for version in plan.expired_versions:
        await purge_document_chunks(
            db,
            plan.document_id,
            tenant_id=tenant_id,
            stores=resolved,
            # 见上面的「删除键是两维的」：索引版本放开，只收窄稿号。
            index_version=None,
            doc_version=version,
        )
    await document_version_repository.delete_versions(
        db, plan.document_id, plan.expired_versions
    )

    logger.info(
        "document versions purged",
        document_id=plan.document_id,
        tenant_id=tenant_id,
        current_version=plan.current_version,
        retain=plan.retain,
        expired_versions=list(plan.expired_versions),
    )
    return plan


def _default_chunk_stores() -> list[ChunkStore]:
    """默认接线：与索引流水线同一份 ``build_chunk_stores``（向量 + 关键词）。

    惰性 import：``app.providers`` 那一批会按配置构造客户端，服务模块在导入时不该顺手
    把 Provider 全装一遍（脚本「导入即安全」这条也靠它）。
    """

    from app.providers.keyword_search import get_keyword_search_provider
    from app.providers.vectorstores import get_vector_store
    from app.services.chunk_store import build_chunk_stores

    return build_chunk_stores(get_vector_store(), get_keyword_search_provider())


def version_list_data(
    document: Document,
    rows: Sequence[DocumentVersion],
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """版本行 + 文档行 → 版本列表出参（**唯一**一处决定「一行给哪些字段」）。

    两个入口共用这一份：数据面（API Key，``GET /documents/{id}/versions``）与控制面
    （会话，``GET /console/documents/{id}/versions``）。两边各写一遍的下场是「控制面多一列、
    数据面少一列」，而前端共用一个版式——那种漂移不会报错，只会让某个页面上的数字缺口。

    ``char_count`` 是这一稿正文的**字符数**（``len``），与切块窗口那个 token 口径无关：
    正文默认不返回，于是「这一稿有多长」只能靠它回答。

    正文按需（``include_content``）：一批整篇正文的响应既大又慢，而列表的常见用途是
    「这篇文档改过几稿、哪一稿是哪天」。

    当前稿指针取**文档行**上的那一列，不取「状态是 current 的那一行」：指针是唯一真值，
    版本行的状态是它的派生（写路径保证两者一致，但读的时候只认指针）。
    """

    return {
        "document_id": str(document.id),
        "current_version": document.current_version,
        "items": [
            {
                "version": row.version,
                "status": row.status,
                "checksum": row.checksum,
                "char_count": len(row.content or ""),
                "created_at": row.created_at,
                "content": (row.content or "") if include_content else None,
            }
            for row in rows
        ],
    }


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