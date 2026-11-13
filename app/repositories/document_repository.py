"""文档数据访问：创建、状态回写、索引抢占（CAS）与租约判定。

并发去重靠数据库，不靠 Celery：``try_claim_for_indexing`` 是一条
``UPDATE ... WHERE``，两个 Worker 同时抢时，Postgres 的行锁 + READ COMMITTED
重评估会让后到者 ``rowcount=0``，于是它直接跳过。

**索引权的归属只有一处**：入队侧（重建端点 / 两个运维脚本）只调用
:func:`prepare_for_indexing` 把文档置成「已排队、未开工」（``index_claimed_at=NULL``，
不盖租约）；Worker 才是唯一的抢占者，调用 :func:`try_claim_for_indexing` 盖租约。
若入队侧也抢先盖租约，Worker 的 WHERE 会因「租约新鲜」而拒绝它，重建与重试都会
变成静默 no-op。

## 异步孪生（第 11 步票 #132 / #130）

入库与索引链路（上传登记、Worker 抢占 / 回写 / 释放租约）与控制面（树、重建、删除、读原文）
都走 ``AsyncSession``，所以本模块的 ``*_async`` 孪生覆盖这两条路径上的全部读与写（后缀与
检索链路的 ``get_by_id_for_tenant`` / ``fetch_meta_by_chunk_ids_async`` 一致）。
**语句本身只写一遍**（``_claim_statement`` / ``_release_statement`` / ``_prepare_statement`` /
两个计数的 statement builder），同步与异步两个入口执行的是同一条 SQL——CAS 的判定条件与
租约语义因此不可能在两套入口之间漂移。

同步的那几个**仍有读者，但不与上面的异步路径共用**：运维脚本
（``reindex_knowledge_base`` / ``requeue_stuck_documents``）与启动期装载是独立进程 /
线程池里的同步读；``get_by_id_for_tenant`` / ``list_by_kb`` 两个同步孪生现在只剩测试在用
（生产零调用点），留着是为了让状态机的 SQL 级用例能直接读行，不作为第二套生产写法。
"""

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Update

from app.db.base import now
from app.models.document import Document
from app.utils.status import DocumentStatus

# 索引租约秒数（运维策略，不是部署配置）：超过这个时长仍停在 PROCESSING 的文档
# 视为「Worker 卡死」，允许被重新抢占 / 删除 / 重建。否则 Worker 崩一次就永久
# 锁死——正是教程里「永远卡在处理中且不报错」的坑。
DOCUMENT_LEASE_SECONDS = 600


def get_by_id_for_tenant(db: Session, document_id: str, *, tenant_id: str) -> Document | None:
    """按 ``id`` **且** ``tenant_id`` 取文档，缺失与跨租户一律返回 ``None``。

    ADR-0006（不做文档级 ACL，隔离只到租户 + Key 的数据范围）要求所有按 id 取
    文档的数据面路径都强制带租户过滤，调用方拿到 ``None`` 后统一按
    ``NOT_FOUND``（20016）处理，不泄露存在性。租户是无默认值的强制参数，不是
    可选过滤器；无租户的 :func:`get_by_id` 只留给 Worker 任务路径。
    """

    stmt = select(Document).where(
        Document.id == document_id, Document.tenant_id == tenant_id
    )
    return db.execute(stmt).scalar_one_or_none()


def list_by_kb_and_status(
    db: Session, kb_id: str, *, tenant_id: str, status: int
) -> list[Document]:
    """该租户在该知识库下、处于指定状态的文档，按创建时间升序。

    运维脚本「全知识库重建」（``scripts/reindex_knowledge_base.py``）用它取出所有
    ``SUCCESS`` 的文档；``tenant_id`` 同样是强制参数（ADR-0006），脚本在扫描时
    显式传入，绝不跨租户。
    """

    stmt = (
        select(Document)
        .where(
            Document.kb_id == kb_id,
            Document.tenant_id == tenant_id,
            Document.status == status,
        )
        .order_by(Document.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def list_stuck(
    db: Session,
    *,
    tenant_id: str | None = None,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    kb_id: str | None = None,
) -> list[Document]:
    """扫出「卡死」的文档：``PROCESSING`` 且租约缺失或已过期。

    运维扫描：显式跨租户；只有 ``scripts/requeue_stuck_documents.py`` 调用，
    数据面禁止使用。``tenant_id`` 一旦给出就强制过滤。

    判定与 CAS 的可抢占条件完全一致：``index_claimed_at IS NULL`` 是「已入队但
    Worker 还没开工」，早于 ``now - lease_seconds`` 是「Worker 崩了 / 卡住」。
    有效租约内的处理中不算卡死，不能被这条查询救回。``kb_id`` 只缩小范围，
    不改变「卡死」的定义；缺省扫全部知识库（``tenant_id`` 缺省为 None，仅有
    运维权限的本地脚本会这样用）。
    """

    lease_before = now() - timedelta(seconds=lease_seconds)
    stmt = select(Document).where(
        Document.status == DocumentStatus.PROCESSING,
        or_(
            Document.index_claimed_at.is_(None),
            Document.index_claimed_at < lease_before,
        ),
    )
    if tenant_id is not None:
        stmt = stmt.where(Document.tenant_id == tenant_id)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    return list(db.execute(stmt.order_by(Document.created_at)).scalars().all())


def list_by_kb(db: Session, kb_id: str, *, tenant_id: str) -> list[Document]:
    """该租户在该知识库下的全部文档，按创建时间升序（列表即轮询进度的时间线）。

    ``tenant_id`` 是强制参数，不是可选过滤器：漏掉它就会跨租户泄露文档（ADR-0006
    要求 Repository 方法签名强制带租户）。这里不提供无租户的变体。
    """

    stmt = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
        .order_by(Document.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def _count_by_kb_statement(kb_id: str, *, tenant_id: str):
    """「这个库有多少份文档」的那一条计数（同步与异步孪生共用）。"""

    return (
        select(func.count())
        .select_from(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
    )


def _count_processing_statement(*, tenant_id: str, lease_before: datetime):
    """「这个租户真的在占用 Worker 的文档数」的那一条计数（同步与异步孪生共用）。"""

    return (
        select(func.count())
        .select_from(Document)
        .where(
            Document.tenant_id == tenant_id,
            Document.status == DocumentStatus.PROCESSING,
            or_(
                Document.index_claimed_at.is_(None),
                Document.index_claimed_at >= lease_before,
            ),
        )
    )


def _claimable_conditions(
    lease_before: datetime, *, allow_success: bool
) -> list[ColumnElement[bool]]:
    """可抢占的行条件（纯条件，不含语句）：抢占与「准备入队」共用同一份。

    失败可重试；处理中但 ``index_claimed_at`` 为空（已入队未开工）或早于
    ``now - lease_seconds``（Worker 卡死）可抢占；``allow_success`` 时成功文档也允许
    显式 force 重建。抽成一处，防止两个函数的判定漂移。
    """

    claimable: list[ColumnElement[bool]] = [
        Document.status == DocumentStatus.FAILED,
        and_(
            Document.status == DocumentStatus.PROCESSING,
            or_(
                Document.index_claimed_at.is_(None),
                Document.index_claimed_at < lease_before,
            ),
        ),
    ]
    if allow_success:
        claimable.append(Document.status == DocumentStatus.SUCCESS)
    return claimable


def _prepare_statement(
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int,
    allow_success: bool,
    prepared_at: datetime,
) -> Update:
    """「准备入队」那条 ``UPDATE``：同步与异步两个入口执行的是同一条语句。"""

    lease_before = prepared_at - timedelta(seconds=lease_seconds)
    return (
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            or_(*_claimable_conditions(lease_before, allow_success=allow_success)),
        )
        .values(
            status=DocumentStatus.PROCESSING,
            index_claimed_at=None,
            error_message=None,
            updated_at=prepared_at,
        )
        .execution_options(synchronize_session=False)
    )


def prepare_for_indexing(
    db: Session,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """入队侧：把可重建的文档标记为「已排队、未开工」；``rowcount > 0`` 即成功。

    与 :func:`try_claim_for_indexing` 用同一份可重建条件，但**不盖租约**：
    ``index_claimed_at=NULL`` 才允许 Worker 之后抢占。这里只做状态准备（
    ``status=PROCESSING``、清 ``error_message``、抬 ``updated_at``），真正的
    索引权由 Worker 唯一负责。``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    stmt = _prepare_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
        prepared_at=now(),
    )
    return int(db.execute(stmt).rowcount or 0) > 0


def try_claim_for_indexing(
    db: Session,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """Worker 侧的单条 ``UPDATE ... WHERE`` 抢占索引权；``rowcount > 0`` 即抢到。

    可抢占的条件全部写在同一个 WHERE 里（见 :func:`_claimable_conditions`）；
    抢到即写入 ``status=PROCESSING`` 与新的抢占时间，并清掉上一次的
    ``error_message``。``tenant_id`` 是强制过滤参数（ADR-0006）。
    调用方提交事务后立刻释放行锁，不要在这里包住 embedding。

    Worker 一律用 ``allow_success=False``：重建端点已经用
    :func:`prepare_for_indexing` 把 SUCCESS 文档置成 PROCESSING，ACK 丢失导致的
    迟到重复投递必须抢不到，否则会在 SUCCESS 之后重写切片。
    """

    stmt = _claim_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
    )
    return int(db.execute(stmt).rowcount or 0) > 0


def _claim_statement(
    document_id: str, *, tenant_id: str, lease_seconds: int, allow_success: bool
) -> Update:
    """抢占索引权的那一条 ``UPDATE``（同步与异步孪生共用，见模块文档「异步孪生」）。"""

    claimed_at = now()
    lease_before = claimed_at - timedelta(seconds=lease_seconds)
    return (
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            or_(*_claimable_conditions(lease_before, allow_success=allow_success)),
        )
        .values(
            status=DocumentStatus.PROCESSING,
            index_claimed_at=claimed_at,
            error_message=None,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )


def release_index_claim(db: Session, document_id: str, *, tenant_id: str) -> None:
    """释放索引租约：``index_claimed_at=NULL``，``status`` 原样保留。

    可重试失败在 ``self.retry`` 之前调用它：不释放的话，重试的下一次
    :func:`try_claim_for_indexing` 会看到仍然新鲜的租约而抢不到，重试永远不执行。
    只清租约、不改状态，失败文档因此走「失败可重试」分支，处理中文档则回到
    「已排队未开工」。``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    db.execute(_release_statement(document_id, tenant_id=tenant_id))


def _release_statement(document_id: str, *, tenant_id: str) -> Update:
    """释放租约的那一条 ``UPDATE``（同步与异步孪生共用）。"""

    return (
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .values(index_claimed_at=None)
        .execution_options(synchronize_session=False)
    )


def is_lease_alive(document: Document, *, lease_seconds: int = DOCUMENT_LEASE_SECONDS) -> bool:
    """纯函数：这份文档是否仍处于「有效处理中」。

    ``status=PROCESSING`` 且 ``index_claimed_at`` 在 ``[now - lease_seconds, now]``
    窗口内才算有人真的在跑。``index_claimed_at is None``（已入队未开工）与租约过期
    （Worker 卡死）都不算，因此允许被删除 / 重建 / 重新抢占。
    """

    if document.status != DocumentStatus.PROCESSING:
        return False
    if document.index_claimed_at is None:
        return False
    return document.index_claimed_at >= now() - timedelta(seconds=lease_seconds)


# ──────────────── 异步孪生（第 11 步票 #132 / #130：入库索引与控制面）
#
# 每一条都与上面的同步版**执行同一条语句**（共用 statement builder），差别只有「等不等」。
# 同步版只剩两个运维脚本在用，删除条件见模块文档。


async def create(db: AsyncSession, doc: Document) -> Document:
    """登记一份文档（只 flush，不提交——提交点是 ``app.db.transaction``）。"""

    db.add(doc)
    await db.flush()
    return doc


async def get_by_id_async(db: AsyncSession, document_id: str) -> Document | None:
    """按主键取，**不带租户过滤**：只给 Worker 任务用（语义同 :func:`get_by_id`）。"""

    document: Document | None = await db.get(Document, document_id)
    return document


async def get_by_id_for_tenant_async(
    db: AsyncSession, document_id: str, *, tenant_id: str
) -> Document | None:
    """按 id + 租户取（语义同同步版 :func:`get_by_id_for_tenant`；控制面与数据面都走它）。"""

    stmt = select(Document).where(
        Document.id == document_id, Document.tenant_id == tenant_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_by_kb_async(db: AsyncSession, kb_id: str, *, tenant_id: str) -> list[Document]:
    """该租户在该知识库下的全部文档（语义同 :func:`list_by_kb`，控制面树与数据面列表）。"""

    stmt = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
        .order_by(Document.created_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def update_status(
    db: AsyncSession, doc: Document, status: int, error_message: str | None = None
) -> None:
    """回写状态与错误文案（只 flush，不提交）。"""

    doc.status = status
    doc.error_message = error_message
    await db.flush()


async def prepare_for_indexing_async(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """入队侧的状态准备（语义同 :func:`prepare_for_indexing`，控制面重建走它）。"""

    stmt = _prepare_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
        prepared_at=now(),
    )
    return int((await db.execute(stmt)).rowcount or 0) > 0


async def try_claim_for_indexing_async(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """Worker 侧抢占索引权；``rowcount > 0`` 即抢到（语义同 :func:`try_claim_for_indexing`）。"""

    result = await db.execute(
        _claim_statement(
            document_id,
            tenant_id=tenant_id,
            lease_seconds=lease_seconds,
            allow_success=allow_success,
        )
    )
    return int(result.rowcount or 0) > 0


async def release_index_claim_async(
    db: AsyncSession, document_id: str, *, tenant_id: str
) -> None:
    """释放索引租约（语义同 :func:`release_index_claim`）。"""

    await db.execute(_release_statement(document_id, tenant_id=tenant_id))


async def count_by_kb(db: AsyncSession, kb_id: str, *, tenant_id: str) -> int:
    """该租户在该知识库下的文档总数（静态配额「单库文档数」的唯一计数处，票 #79）。"""

    result = await db.execute(_count_by_kb_statement(kb_id, tenant_id=tenant_id))
    return int(result.scalar_one())


async def count_processing_for_tenant(
    db: AsyncSession, *, tenant_id: str, lease_seconds: int = DOCUMENT_LEASE_SECONDS
) -> int:
    """本租户真的在占用 Worker 的文档数（语义同 :func:`count_processing_for_tenant`）。"""

    lease_before = now() - timedelta(seconds=lease_seconds)
    result = await db.execute(
        _count_processing_statement(tenant_id=tenant_id, lease_before=lease_before)
    )
    return int(result.scalar_one())
