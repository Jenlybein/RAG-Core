"""索引构建编排（#142）：一次构建 = 一个不可变版本的生命周期（ADR-0025）。

```text
building ──▶ ready ──(单事务切换)──▶ active ──▶ archived
    │                                  ▲            │
    │                                  └───(回滚)───┘
    └──▶ failed（线上仍查旧的 active，error_message 记原因）
```

**本模块是状态机的唯一实现点**：谁能迁到谁写在 :data:`ALLOWED_TRANSITIONS` 里，而不是
散在若干处 ``row.status = ...``。新增一条迁移必须先改那张表——那是一行会被人看见的代码，
而散在各处的赋值只会让人以为「状态怎么变的」要去猜。

**``archived`` 不是终态**（#155）：回滚就是 ``archived → active``，而它走的**不是**另一套
动作——:meth:`IndexBuildService.activate` 与 :meth:`IndexBuildService.rollback` 都进
:meth:`IndexBuildService._switch_active_version`，唯一的差别是「从哪个版本出发」。

## 构建期间线上照旧查旧版本（决策二）

构建只写**目标版本**（``chunks.index_version`` / 关键词的物理索引都带它），知识库上的
``active_index_version`` 在整个构建过程里**一个字节都不改**，所以检索 pin 的仍是旧版本
——不是「旧版本 + 降级标记」，是完全不变。本模块的每一条失败路径都不碰那一列。

## 失败只落 failed（决策三）

``error_message`` 只存**固定文案**：异常堆栈只进日志，绝不进库（失败留痕的同一条纪律，
见 ``app/tasks/indexing.py``）。``failed`` 的版本不会自己重试——重建请重新发起一次构建，
那是另一个版本号。

## 取消与进程死掉的差别（刻意不对称）

* **任务内的异常**（含 Celery 的 ``SoftTimeLimitExceeded``）→ :meth:`IndexBuildService.build`
  把它收成 ``failed`` + 原因；
* **进程被 SIGKILL / 被取消** → 那一行**留在 building**，因为「写一笔可提交的事务」这件事
  在被取消的上下文里本身就不可靠。留着的 ``building`` 行不是半成品上线（线上照旧查旧的
  active），而是运维可见的一个待裁决项：:meth:`IndexBuildService.list_stalled_builds`
  按年龄把它指出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Sequence

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.db.transaction import async_commit_now, async_rollback_now, async_transaction
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
    effective_index_version,
    next_version_name,
)
from app.providers.keyword_search import KeywordSearchProvider, get_keyword_search_provider
from app.providers.vectorstores.base import VectorStore
from app.providers.vectorstores import get_vector_store
from app.models.knowledge_base import KnowledgeBase
from app.repositories import (
    chunk_repository,
    document_repository,
    index_version_repository,
    knowledge_base_repository,
)
from app.services.indexing_service import IndexingService
from app.services.section_edge_service import rebuild_version_edges
from app.services.section_graph import EdgeScope
from app.utils.id_generator import new_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

#: ``failed`` 时落库的唯一文案（一因一码：失败原因分几类就几个常量，不拼异常文本）。
BUILD_FAILED_MESSAGE: Final[str] = "index build failed, see worker logs"
#: 另一个构建还在跑时拒绝发起新构建的话术。
BUILD_ALREADY_RUNNING_MESSAGE: Final[str] = (
    "an index build is already running for this knowledge base"
)
#: 状态迁移不合法（例如没 ready 就想 active）时的固定话术。
BUILD_INVALID_TRANSITION_MESSAGE: Final[str] = "index build status transition is not allowed"
#: 回滚的目标版本**已经没有可查的数据**（物理索引不在 / 切片为 0）时的话术。
ROLLBACK_DATA_MISSING_MESSAGE: Final[str] = (
    "index version has no physical data left; refusing to roll back"
)
#: 两条入口（激活与回滚）撞上同一次并发切换时的话术。
SWITCH_CONFLICT_MESSAGE: Final[str] = (
    "another index version switch won the race for this knowledge base"
)
#: 四项计数不相等时拒绝 ``ready`` 的话术（半成品不许上线）。
BUILD_COUNTS_MISMATCH_MESSAGE: Final[str] = (
    "index build counts do not match; refusing to mark it ready"
)
#: 一行 ``building`` 停留超过这个时长就当「有行、没任务」（运维可据此重跑或标失败）。
BUILD_STALL_SECONDS: Final[int] = 60 * 60

#: 合法迁移表。空集合 = 终态，谁也别想再动它。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    INDEX_BUILD_BUILDING: frozenset({INDEX_BUILD_READY, INDEX_BUILD_FAILED}),
    INDEX_BUILD_READY: frozenset({INDEX_BUILD_ACTIVE, INDEX_BUILD_FAILED}),
    INDEX_BUILD_ACTIVE: frozenset({INDEX_BUILD_ARCHIVED}),
    # 回滚（#155）：归档**不是**终态。「新切法出问题就退回去」是 ADR-0025 承诺的四样收益
    # 之一；把它写成空集合，保留 v1 就只服务了评测，没服务线上。
    INDEX_BUILD_ARCHIVED: frozenset({INDEX_BUILD_ACTIVE}),
    INDEX_BUILD_FAILED: frozenset(),
}


class IndexBuildAlreadyRunning(AppError):
    """该库已经有一条 ``building`` 行：第二次构建在开始处就被拒。

    错误码用的是既有的 ``SYSTEM_BUSY``（50001），**没有新增码**：错误码全表是契约，
    为一条内部编排路径加码要连带改文档与门禁，而那属于「新增入口」的票。调用方要区分
    的话，本类本身就是那个区分点。
    """

    def __init__(self, running: IndexBuildVersion | None = None) -> None:
        self.running = running
        super().__init__(ErrorCode.SYSTEM_BUSY, BUILD_ALREADY_RUNNING_MESSAGE)


class IndexBuildInvalidTransition(AppError):
    """不合法的一次状态迁移（例如 ``building`` 直接跳 ``active``）。"""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_INVALID_TRANSITION_MESSAGE)


class IndexBuildRollbackRefused(AppError):
    """回滚的目标版本**没有可查的数据**：拒绝上线。

    这是本票最容易做错的那条守卫（#155）：库列一改、别名一切，检索就按目标版本过滤，
    而那个版本的数据已经不在了——于是每一次检索都是一次「诚实的空结果」，被包装成
    「本次没命中」。宁可在这里显式拒绝，也不要让运维在几天后从「怎么搜不到了」开始查。

    ``keyword_index`` 为 ``None`` 表示关键词能力不存在（没部署 ES）——这一维不参与判定
    （与 #142 的四项计数同一口径：把「没有这项能力」当成 0 会把能力缺失伪装成数据不一致）。
    """

    def __init__(self, *, version: str, keyword_index: bool | None, chunks: int) -> None:
        self.version = version
        self.keyword_index = keyword_index
        self.chunks = chunks
        super().__init__(ErrorCode.SERVER_ERROR, ROLLBACK_DATA_MISSING_MESSAGE)


class IndexBuildSwitchConflict(AppError):
    """两条切换撞在一起，这条输了（部分唯一索引把它挡在提交处）。

    **两条入口抛同一个类**：激活与回滚都是「把某个版本变成 active」，撞上并发时调用方
    该看到的是同一句话，而不是「回滚有一句错误、激活另有一句」。

    错误码沿用既有的 ``SYSTEM_BUSY``（50001），**没有新增码**——理由与
    :class:`IndexBuildAlreadyRunning` 相同：错误码全表是契约，为一条内部编排路径加码要
    连带改文档与门禁，那属于「新增入口」的票。
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.SYSTEM_BUSY, SWITCH_CONFLICT_MESSAGE)


@dataclass(frozen=True)
class BuildCounts:
    """四项计数的**读数快照**（ADR-0025 决策一：相等才允许 ``ready``）。

    | 维度 | 从哪来 |
    | --- | --- |
    | ``knowledge_bases`` | 本次构建的作用域，恰好一个库（库行读得到才是 1） |
    | ``chunks`` | ``chunks`` 表里 ``(租户, 库, 目标版本)`` 的行数 |
    | ``vectors`` | 向量存储里同一范围内的可检索条数 |
    | ``keyword_documents`` | 关键词引擎里目标版本的物理索引中的文档数 |

    **「相等」只能是这个意思**：知识库数是**范围计数**（一个库 = 1），另外三项是数据
    计量（每库 N 片）。字面意义的「四个数字彼此相等」在 N>1 时永远不成立，所以判据是
    「范围计数 == 1，且三项数据计数彼此相等」——ADR 想挡的是「切片的数量与向量 / 关键词
    的数量对不上」，那种版本上线后的表现是「有些文档搜不到」，且不报错。

    ``keyword_documents`` 为 ``None`` 表示**关键词能力不存在**（没部署 ES，Provider 是空
    实现）：这一维不参与判定。否则没有 ES 的部署永远到不了 ``ready``——把「没有这项能力」
    当成「这一维是 0」，会把能力缺失伪装成数据不一致。
    """

    knowledge_bases: int
    chunks: int
    vectors: int
    keyword_documents: int | None

    @property
    def matched(self) -> bool:
        """四项计数是否对得上（口径见类文档）。"""

        if self.knowledge_bases != 1:
            return False
        if self.chunks != self.vectors:
            return False
        return self.keyword_documents is None or self.keyword_documents == self.chunks

    def as_dict(self) -> dict[str, int | None]:
        """落日志用的扁平形状（`关键词文档数` 可能是 ``None``）。"""

        return {
            "knowledge_bases": self.knowledge_bases,
            "chunks": self.chunks,
            "vectors": self.vectors,
            "keyword_documents": self.keyword_documents,
        }


class IndexBuildCountsMismatch(AppError):
    """四项计数不相等：**拒绝**标记 ready（半成品上线后表现为「有些文档搜不到」）。"""

    def __init__(self, counts: BuildCounts) -> None:
        self.counts = counts
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_COUNTS_MISMATCH_MESSAGE)


def can_transition(current: str, target: str) -> bool:
    """``current`` 能否直接迁到 ``target``（纯函数，状态机的真值表只有这一份）。"""

    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(row: IndexBuildVersion, target: str) -> None:
    """迁移不合法就抛 :class:`IndexBuildInvalidTransition`，合法则什么都不做。"""

    if not can_transition(row.status, target):
        raise IndexBuildInvalidTransition(row.status, target)


def ensure_activation_origin(row: IndexBuildVersion, *, origin_status: str) -> None:
    """这一行能不能从 ``origin_status`` 出发变成 ``active``。

    两件事各司其职：真值表（:data:`ALLOWED_TRANSITIONS`）回答「这个目标状态合不合法」，
    ``origin_status`` 回答「这条入口认不认它当起点」。分开之后，激活（``ready``）与回滚
    （``archived``）不会互相冒充——尤其不会让 ``activate`` 顺路激活一个归档版本，
    从而绕过回滚特有的物理数据守卫。
    """

    ensure_transition(row, INDEX_BUILD_ACTIVE)
    if row.status != origin_status:
        raise IndexBuildInvalidTransition(row.status, INDEX_BUILD_ACTIVE)


def _is_failure_allowed(row: IndexBuildVersion) -> bool:
    """这条行还允许被标记失败吗。

    ``failed`` 只从 ``building`` / ``ready`` 来。``active`` / ``archived`` 收到一次迟到
    的失败上报时**什么都不改**（只记一条 warning）：生效版本不该被一次收尾覆盖，否则
    线上会在一瞬间「没有生效版本」——那正是 ADR-0025 决策三要防的那种中间态。
    """

    return can_transition(row.status, INDEX_BUILD_FAILED)


class IndexBuildService:
    """一次构建的编排：起版本行、逐文档重建、校验、激活。

    **依赖全部从构造参数注入**（与 ``IndexingService`` 同一条纪律）：离线用例注入
    记录型替身即可跑完整条编排，不连 PostgreSQL、不连 ES、不发网络。
    """

    def __init__(
        self,
        *,
        indexing: IndexingService | None = None,
        vector_store: VectorStore | None = None,
        keyword_search: KeywordSearchProvider | None = None,
    ) -> None:
        self._indexing = indexing if indexing is not None else IndexingService()
        self._vector_store = vector_store if vector_store is not None else get_vector_store()
        self._keyword_search = (
            keyword_search if keyword_search is not None else get_keyword_search_provider()
        )

    # ── 起一个构建 ────────────────────────────────────────────────

    async def start_build(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
        tenant_id: str,
        version: str | None = None,
    ) -> IndexBuildVersion:
        """为这个库建一行 ``building``，返回它（版本名缺省由已有版本算出）。

        **并发只允许一个**：先读一次 ``building`` 行把拒绝说得清楚，再靠那条部分唯一索引
        兜底——读与写之间的窗口里另一个请求可能刚插进去，那时 ``flush`` 的
        ``IntegrityError`` 会被翻译成同一个 :class:`IndexBuildAlreadyRunning`。只做前者是
        假守卫（TOCTOU），只做后者则拿不到「已经在建的是哪一行」这个可读的信息。
        """

        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        existing = await index_version_repository.list_for_kb(
            db, kb_id, tenant_id=tenant_id
        )
        running = next(
            (row for row in existing if row.status == INDEX_BUILD_BUILDING), None
        )
        if running is not None:
            raise IndexBuildAlreadyRunning(running)

        # 建库只写了一列、没写行（#141 的落法）：激活时要能归档「上一版」，就必须先把
        # 当前生效版本补成一行。缺了它，第一轮构建的激活会「找不到旧 active 可归档」，
        # 而库列被改掉之后旧版本的数据就永远停在「无人认领」的状态。
        adopted = await self._adopt_live_version(db, kb, existing)
        names = [row.version for row in existing] + ([adopted] if adopted else [])

        row = IndexBuildVersion(
            id=new_id(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            version=version or next_version_name(names),
            status=INDEX_BUILD_BUILDING,
            created_at=now(),
        )
        try:
            await index_version_repository.create(db, row)
        except IntegrityError as exc:
            # 另一个请求在「读过之后、插之前」抢先占了 building 位：回滚掉这笔半成品，
            # 抛同一个业务异常（调用方看到的两种拒绝必须一样）。
            await async_rollback_now(db)
            raise IndexBuildAlreadyRunning(None) from exc
        await async_commit_now(db)

        logger.info(
            "index build started",
            build_id=row.id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            version=row.version,
        )
        return row

    async def _adopt_live_version(
        self, db: AsyncSession, kb: KnowledgeBase, rows: Sequence[IndexBuildVersion]
    ) -> str | None:
        """把库里「当前生效版本」补成一行 ``active``；补了就返回那个版本名。

        ``rows`` 是调用方刚读到的版本行（不再查一次：同一条语句读两遍只会在两次读之间
        留下一个新的窗口）。
        """

        live = effective_index_version(kb)
        if live is None:
            return None
        if any(row.version == live for row in rows):
            return None
        if any(row.status == INDEX_BUILD_ACTIVE for row in rows):
            # 库里已经有一行 active，却与库列指向的版本名不一致：这是**人手改库**
            # 才会出现的状态，不自作主张再插一行（会撞部分唯一索引），只记一条 warning。
            logger.warning(
                "effective index version disagrees with the build table",
                kb_id=kb.id,
                tenant_id=kb.tenant_id,
                effective_version=live,
            )
            return None
        await index_version_repository.create(
            db,
            IndexBuildVersion(
                id=new_id(),
                tenant_id=str(kb.tenant_id),
                kb_id=str(kb.id),
                version=live,
                status=INDEX_BUILD_ACTIVE,
                created_at=now(),
                activated_at=now(),
            ),
        )
        return live

    # ── 跑一次构建 ────────────────────────────────────────────────

    async def build(
        self, db: AsyncSession, build_id: str, *, tenant_id: str
    ) -> IndexBuildVersion:
        """跑完一次构建：逐文档重建 → 四项计数核对 → ``ready``（激活是另一步）。

        激活**不**在这里做：它要改线上（库上的生效版本列），必须是一个能被单独调起、
        单独重试、单独审计的动作。把两者塞进一个函数，运维就没有「先 ready、观察一会儿
        再切」的余地，而 ADR-0025 把 ``ready`` 明确写成了**运维可见的一步**。

        **一次构建 = 若干次「一篇文档一笔事务」**：整库包成一笔会把行锁持有到最后一篇
        （上传、删除、检索都要等它）；包成一笔也不换来什么，因为检索**不靠**构建的
        事务边界来保持一致——它 pin 在旧版本上（ADR-0025 决策二）。所以每一篇文档自己
        提交，失败的那一篇让整次构建落 ``failed``：部分版本永远不许上线。

        哪些文档参与：``SUCCESS`` 的那些。失败 / 处理中的文档没有可切的正文，把它们的
        失败再重放一遍只是把构建拖成失败；它们该由单篇重试解决，不属于构建。
        """

        row = await index_version_repository.get_for_tenant(
            db, build_id, tenant_id=tenant_id
        )
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        if row.status != INDEX_BUILD_BUILDING:
            # 重复投递 / 迟到的任务：不是 building 就别再动它（尤其别动 active）。
            logger.warning(
                "index build is not building, skipping",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return row

        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, row.kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        try:
            documents = await document_repository.list_by_kb_async(
                db, row.kb_id, tenant_id=tenant_id
            )
            indexed = 0
            for document in documents:
                if document.status != DocumentStatus.SUCCESS:
                    continue
                await self._indexing.index_existing_document(
                    db,
                    document.id,
                    # 目标版本是本票的唯一输入：写错版本等于「构建成功但没人看得见」。
                    index_version=row.version,
                    # 重建的输入本来就是成功文档，必须允许抢占它们。
                    allow_success=True,
                )
                indexed += 1
            # 跨文档引用有一个天然的先后问题：A 引用 B 的 2.4 节时，B 可能还没被这一轮
            # 构建写入——那一刻目录里没有 B，按「悬空边一律不建」的规则这条边不会建。
            # 所以逐篇重建之后再**整版重算一遍**（幂等，同一份抽取跑第二遍）：现在目录
            # 才是完整的目录。不做这一步的后果是「引用边只在某些文档顺序下才出现」。
            await rebuild_version_edges(
                db,
                scope=EdgeScope(
                    tenant_id=tenant_id, kb_id=row.kb_id, index_version=row.version
                ),
            )
            # 计数快照里的文档数：**成功重建的篇数**（不是库里总行数——失败 / 处理中的
            # 文档不参与构建，把它们算进去会让快照看起来「少了几篇」却查不出原因）。
            row.document_count = indexed
            counts = await self.collect_counts(db, kb=kb, version=row.version)
            await self.mark_ready(db, build_id, tenant_id=tenant_id, counts=counts)
        except Exception as exc:  # noqa: BLE001 - 收成 failed，原因只留固定文案
            await async_rollback_now(db)
            await self.mark_failed(
                db,
                build_id,
                tenant_id=tenant_id,
                # 一因一码：计数对不上与「跑崩了」是两件事，运维处置完全不同。
                reason=(
                    BUILD_COUNTS_MISMATCH_MESSAGE
                    if isinstance(exc, IndexBuildCountsMismatch)
                    else BUILD_FAILED_MESSAGE
                ),
            )
            logger.exception(
                "index build aborted",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
            )
            raise

        logger.info(
            "index build ready",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            document_count=indexed,
        )
        return row

    # ── 四项计数 ─────────────────────────────────────────────────

    async def collect_counts(
        self, db: AsyncSession, *, kb: KnowledgeBase, version: str
    ) -> BuildCounts:
        """把四项计数的读数取回来（各自从它真正的存储里读，见 :class:`BuildCounts`）。

        为什么分四次读而不是写一条大 SQL：它们**不在同一个存储里**。切片在
        PostgreSQL，向量在向量存储（本实现同表，换实现就不同），关键词文档在 ES。
        想「一条语句查完」就只能假装它们同源，而那道闸门要挡的正是它们不同源时的偏差。
        """

        tenant_id = str(kb.tenant_id)
        kb_id = str(kb.id)
        return BuildCounts(
            knowledge_bases=1,
            chunks=await chunk_repository.count_by_index_version(
                db, kb_id, tenant_id=tenant_id, index_version=version
            ),
            vectors=await self._vector_store.count_by_scope(
                db, tenant_id=tenant_id, kb_id=kb_id, index_version=version
            ),
            keyword_documents=(
                await self._keyword_search.count_documents(
                    tenant_id=tenant_id, kb_id=kb_id, index_version=version
                )
                if self._keyword_search.available
                else None
            ),
        )

    # ── ready / 激活 ──────────────────────────────────────────────

    async def mark_ready(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        counts: BuildCounts,
    ) -> IndexBuildVersion:
        """四项计数相等才允许 ``ready``；不相等就**拒绝**（抛异常，不改状态）。

        为什么是「拒绝」而不是「顺手标成 failed」：这个函数只回答「能不能标记为可用」，
        把失败留痕留给调用方（:meth:`build` 会带着具体原因去落 ``failed``）。混在一起
        之后，用例就没法单独断言「计数不相等时 ready 被拒」这条验收。
        """

        row = await index_version_repository.get_for_tenant(
            db, build_id, tenant_id=tenant_id
        )
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        ensure_transition(row, INDEX_BUILD_READY)
        if not counts.matched:
            logger.warning(
                "index build counts do not match",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
                **counts.as_dict(),
            )
            raise IndexBuildCountsMismatch(counts)

        row.chunk_count = counts.chunks
        row.status = INDEX_BUILD_READY
        await async_commit_now(db)
        logger.info(
            "index build marked ready",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            **counts.as_dict(),
        )
        return row

    async def activate(
        self, db: AsyncSession, build_id: str, *, tenant_id: str
    ) -> IndexBuildVersion:
        """**单事务**激活：旧版本 ``archived`` + 新版本 ``active`` + 库列更新。

        三者同成同败（ADR-0025 决策三）。分开做的中间态是「库指向新版本、新版本还没
        active」，检索会拿到一个不存在的生效版本 → 一条都匹配不上 → 一次「诚实的空结果」
        被包装成「本次没命中」，于是故障看起来像查询问题。

        关键词别名的切换**排在提交之前**，并且不参与数据库事务（ES 不在 PG 的事务里）：

        * 先切别名的窗口里，别名指向新物理索引而库列仍是旧版本——关键词那一路按
          ``(库, 旧版本)`` 过滤新索引里的行，**一条都不匹配**，于是这一路诚实返回空，
          而不是混排两个版本（那正是决策二要防的）；
        * 反过来（先提交、后切别名）留下的窗口更糟：库说新版本、关键词仍查旧索引，
          两路数据**对不上**，而且如果切换失败就一直错下去。

        于是顺序是：切别名 → 提交事务；提交失败就把别名切回去（尽力而为，失败只记
        warning——那时真正该看的是提交失败本身）。

        这条入口只服务 ``ready → active``。``archived → active`` 是
        :meth:`rollback`（它多一条物理数据守卫，不能让激活顺路把它绕过去）。
        """

        return await self._switch_active_version(
            db, build_id, tenant_id=tenant_id, origin_status=INDEX_BUILD_READY
        )

    async def rollback(
        self, db: AsyncSession, build_id: str, *, tenant_id: str
    ) -> IndexBuildVersion:
        """**回滚**：把 ``archived`` 的那一版切回 active（#155，spec 用户故事 33）。

        动作与 :meth:`activate` 逐条相同——两条入口都进
        :meth:`_switch_active_version`，差别只有「从哪个版本出发」。**为什么不能抄一份**：
        抄出来的第二份会漏掉「归档当前 active」「回切别名」中的某一步，而两种遗漏在检索
        侧都表现为「库说 A、数据在 B」，且不报错。

        回滚多一条激活不需要的守卫：目标版本的**物理数据必须还在**。``ready`` 那一侧
        刚被四项计数验过（#142），而归档版本可能在很久以前就被清理过——它是不是空的，
        只有在这里问一句才知道。
        """

        row = await index_version_repository.get_for_tenant(
            db, build_id, tenant_id=tenant_id
        )
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        # 先裁决「这一行配不配当回滚目标」，再问「它的数据还在不在」：状态不对的行
        # （building / failed）要用「从哪到哪」那条话术拒绝，不该被数据守卫抢答成
        # 「数据没了」——那是两种运维处置。共用那一段会再验一次真值表（纯函数，重复无害）。
        ensure_activation_origin(row, origin_status=INDEX_BUILD_ARCHIVED)
        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, row.kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        await self._ensure_rollback_has_data(db, kb=kb, row=row)
        return await self._switch_active_version(
            db,
            build_id,
            tenant_id=tenant_id,
            origin_status=INDEX_BUILD_ARCHIVED,
            kb=kb,
            row=row,
        )

    async def _ensure_rollback_has_data(
        self, db: AsyncSession, *, kb: KnowledgeBase, row: IndexBuildVersion
    ) -> None:
        """回滚守卫：目标版本的**物理数据仍在**（物理索引在、切片数 > 0）。

        两个读数来自两个存储（ES 与 PostgreSQL），哪一个为「没有」都拒绝：向量那一路要
        按版本从 ``chunks`` 里召回，关键词那一路要按别名查物理索引。缺任何一半，切过去之后
        对应的那一路就静默返空。

        「能力不存在」与「数据不存在」在这里**必须分开**：没部署 ES 的部署里物理索引永远
        不存在（``available`` 为 ``False``），把这一维当成「数据没了」会让回滚在那种部署
        上永远不可用——与 #142 把 ``keyword_documents`` 记成 ``None`` 是同一口径。
        """

        keyword_index = (
            await self._keyword_search.physical_index_exists(row.version)
            if self._keyword_search.available
            else None
        )
        chunks = await chunk_repository.count_by_index_version(
            db, str(kb.id), tenant_id=str(kb.tenant_id), index_version=row.version
        )
        if keyword_index is False or chunks <= 0:
            logger.warning(
                "index version cannot be rolled back to",
                build_id=row.id,
                tenant_id=row.tenant_id,
                kb_id=row.kb_id,
                version=row.version,
                keyword_index_exists=keyword_index,
                chunks=chunks,
            )
            raise IndexBuildRollbackRefused(
                version=row.version, keyword_index=keyword_index, chunks=chunks
            )

    async def _switch_active_version(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        origin_status: str,
        kb: KnowledgeBase | None = None,
        row: IndexBuildVersion | None = None,
    ) -> IndexBuildVersion:
        """把这一行切成 active：别名 → 一笔事务（归档旧 active + 本行 active + 库列）。

        **激活与回滚的同一段实现**：唯一的差别是 ``origin_status``（``ready`` 还是
        ``archived``）。真值表裁决合法性、入口声明起点（见
        :func:`ensure_activation_origin`），两道门因此不能互相冒充：激活走不到回滚的物理
        数据守卫上，回滚也不会被当成「第二次激活」。

        ``row`` / ``kb`` 可以由调用方传进来（回滚为了跑守卫已经读过它们）：同一个动作不必
        为此多查两次库，而两次读之间还留着「读到的不再是同一行」的窗口。
        """

        if row is None:
            row = await index_version_repository.get_for_tenant(
                db, build_id, tenant_id=tenant_id
            )
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        ensure_activation_origin(row, origin_status=origin_status)

        if kb is None:
            kb = await knowledge_base_repository.get_by_id_for_tenant(
                db, row.kb_id, tenant_id=tenant_id
            )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        previous = await index_version_repository.find_active(
            db, row.kb_id, tenant_id=tenant_id
        )
        previous_version = previous.version if previous is not None else None

        switched = False
        try:
            await self._keyword_search.activate_version(row.version)
            switched = True
            async with async_transaction(db):
                stamp = now()
                if previous is not None:
                    previous.status = INDEX_BUILD_ARCHIVED
                    previous.archived_at = stamp
                row.status = INDEX_BUILD_ACTIVE
                row.activated_at = stamp
                # 回滚上线的版本不再有「归档于」：留着它，查「最近归档了哪几版」会说谎。
                row.archived_at = None
                # 库上的生效版本与状态一起改：检索范围从这一列读（决策四）。
                kb.active_index_version = row.version
        except IntegrityError as exc:
            # 并发的另一次切换先提交了：两条部分唯一索引在这里挡住「同一库两个 active」。
            # 回滚与激活看到的是**同一条**可读拒绝，而不是一次 500。
            if switched:
                await self._restore_alias(previous_version, row=row)
            raise IndexBuildSwitchConflict() from exc
        except BaseException:
            if switched:
                await self._restore_alias(previous_version, row=row)
            raise

        logger.info(
            "index build activated",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            previous_version=previous_version,
            origin_status=origin_status,
        )
        return row

    async def _restore_alias(
        self, previous_version: str | None, *, row: IndexBuildVersion
    ) -> None:
        """提交失败后把关键词别名切回去（尽力而为；失败只记 warning）。

        **切回的是「切换前读到的那一版」**，不是重新问一次「现在谁生效」：错误路径上再查
        一次库，会把「提交为什么失败」这条真正的原因盖进下一层异常里。并发输掉的那一方
        因此有一个已知窗口（赢家已经指向另一版时，别名会落在切换前那一版上，直到下一次
        切换把别名再挪一次）——本票登记它，不在错误路径上追加一次读。
        """

        if previous_version is None:
            # 这是这个库的第一次构建：没有旧版本可回退，别名留在了未提交的新版本上。
            # 检索仍安全（库列还是空的 → 守卫直接拦），但下一轮激活会把别名再切一次。
            logger.warning(
                "keyword alias left on an uncommitted version",
                build_id=row.id,
                tenant_id=row.tenant_id,
                version=row.version,
            )
            return
        try:
            await self._keyword_search.activate_version(previous_version)
        except Exception:  # noqa: BLE001 - 收尾路径不抛：真正的原因是那次提交失败
            logger.warning(
                "keyword alias restore failed",
                build_id=row.id,
                tenant_id=row.tenant_id,
                previous_version=previous_version,
            )

    # ── 失败 ────────────────────────────────────────────────────

    async def mark_failed(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        reason: str = BUILD_FAILED_MESSAGE,
    ) -> IndexBuildVersion | None:
        """把这个版本收成 ``failed`` + 固定原因；**不碰** ``active_index_version``。

        返回 ``None`` 表示这一行不存在（跨租户 / 被删），调用方不必区分——失败路径再多
        一层「没有这一行」的异常，只会把真正的原因盖掉。
        """

        row = await index_version_repository.get_for_tenant(
            db, build_id, tenant_id=tenant_id
        )
        if row is None:
            return None
        if not _is_failure_allowed(row):
            logger.warning(
                "index build failure arrived late, status kept",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return row
        row.status = INDEX_BUILD_FAILED
        row.error_message = reason
        await async_commit_now(db)
        logger.info(
            "index build failed",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            reason=reason,
        )
        return row

    # ── 「有行、没任务」 ─────────────────────────────────────────

    async def list_stalled_builds(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        older_than_seconds: int = BUILD_STALL_SECONDS,
        at: datetime | None = None,
    ) -> list[IndexBuildVersion]:
        """指出那些「还是 building、但很可能已经没有任务在跑」的行。

        进程被 SIGKILL、机器重启、Worker 队列被清空——这些情况下库里只剩一行 ``building``，
        没有任何东西会去收尾。它**不影响线上**（active 仍是旧版本），但会挡住下一次构建
        （一个库只允许一个 building），所以必须能被看见。判定只用年龄：库里读不到「任务
        还在不在」。
        """

        started_before = (at or now()) - timedelta(seconds=older_than_seconds)
        rows = await index_version_repository.list_stalled(
            db, tenant_id=tenant_id, started_before=started_before
        )
        if rows:
            logger.warning(
                "index build has no task",
                tenant_id=tenant_id,
                build_ids=[row.id for row in rows],
                kb_ids=[row.kb_id for row in rows],
                versions=[row.version for row in rows],
                older_than_seconds=older_than_seconds,
            )
        return rows


def describe_stalled_builds(rows: Sequence[IndexBuildVersion]) -> str:
    """把 :meth:`IndexBuildService.list_stalled_builds` 的结果拼成一行可读的话。

    给运维脚本 / 重启自检用：它要打印的是「哪个库、哪一版、什么时候开始的」，而不是一个
    列表对象的 ``repr``。
    """

    if not rows:
        return "no stalled index build"
    parts = [
        f"{row.kb_id}/{row.version} (build {row.id}, started {row.created_at.isoformat()})"
        for row in rows
    ]
    return "stalled index builds: " + "; ".join(parts)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "BUILD_ALREADY_RUNNING_MESSAGE",
    "BUILD_COUNTS_MISMATCH_MESSAGE",
    "BUILD_FAILED_MESSAGE",
    "BUILD_INVALID_TRANSITION_MESSAGE",
    "BUILD_STALL_SECONDS",
    "BuildCounts",
    "IndexBuildAlreadyRunning",
    "IndexBuildCountsMismatch",
    "IndexBuildInvalidTransition",
    "IndexBuildRollbackRefused",
    "IndexBuildService",
    "IndexBuildSwitchConflict",
    "ROLLBACK_DATA_MISSING_MESSAGE",
    "SWITCH_CONFLICT_MESSAGE",
    "can_transition",
    "describe_stalled_builds",
    "ensure_activation_origin",
    "ensure_transition",
]
