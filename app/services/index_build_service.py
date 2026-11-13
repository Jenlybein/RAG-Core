"""索引构建编排（#142）：一次构建 = 一个不可变版本的生命周期（ADR-0025）。

```text
building ──▶ ready ──(单事务切换)──▶ active ──▶ archived
    │
    └──▶ failed（线上仍查旧的 active，error_message 记原因）
```

**本模块是状态机的唯一实现点**：谁能迁到谁写在 :data:`ALLOWED_TRANSITIONS` 里，而不是
散在若干处 ``row.status = ...``。新增一条迁移必须先改那张表——那是一行会被人看见的代码，
而散在各处的赋值只会让人以为「状态怎么变的」要去猜。

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


from datetime import datetime, timedelta
from typing import Final, Sequence

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.db.transaction import async_commit_now, async_rollback_now
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
    document_repository,
    index_version_repository,
    knowledge_base_repository,
)
from app.services.indexing_service import IndexingService
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
#: 一行 ``building`` 停留超过这个时长就当「有行、没任务」（运维可据此重跑或标失败）。
BUILD_STALL_SECONDS: Final[int] = 60 * 60

#: 合法迁移表。空集合 = 终态，谁也别想再动它。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    INDEX_BUILD_BUILDING: frozenset({INDEX_BUILD_READY, INDEX_BUILD_FAILED}),
    INDEX_BUILD_READY: frozenset({INDEX_BUILD_ACTIVE, INDEX_BUILD_FAILED}),
    INDEX_BUILD_ACTIVE: frozenset({INDEX_BUILD_ARCHIVED}),
    INDEX_BUILD_ARCHIVED: frozenset(),
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


def can_transition(current: str, target: str) -> bool:
    """``current`` 能否直接迁到 ``target``（纯函数，状态机的真值表只有这一份）。"""

    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(row: IndexBuildVersion, target: str) -> None:
    """迁移不合法就抛 :class:`IndexBuildInvalidTransition`，合法则什么都不做。"""

    if not can_transition(row.status, target):
        raise IndexBuildInvalidTransition(row.status, target)


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
        """把目标版本逐文档重建出来（任务体调它一次）。

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
        except Exception as exc:  # noqa: BLE001 - 收成 failed，原因只留固定文案
            await async_rollback_now(db)
            await self.mark_failed(db, build_id, tenant_id=tenant_id)
            logger.exception(
                "index build aborted",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
            )
            raise exc

        logger.info(
            "index build documents rebuilt",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            document_count=indexed,
        )
        return row

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
    "BUILD_FAILED_MESSAGE",
    "BUILD_INVALID_TRANSITION_MESSAGE",
    "BUILD_STALL_SECONDS",
    "IndexBuildAlreadyRunning",
    "IndexBuildInvalidTransition",
    "IndexBuildService",
    "can_transition",
    "describe_stalled_builds",
    "ensure_transition",
]
