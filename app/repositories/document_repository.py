"""文档数据访问：创建、状态回写、索引抢占（CAS）与租约判定。

并发去重靠数据库，不靠 Celery：``try_claim_for_indexing`` 是一条
``UPDATE ... WHERE``，两个 Worker 同时抢时，Postgres 的行锁 + READ COMMITTED
重评估会让后到者 ``rowcount=0``，于是它直接跳过。
"""

from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.db.base import now
from app.models.document import Document
from app.utils.status import DocumentStatus

# 索引租约秒数（运维策略，不是部署配置）：超过这个时长仍停在 PROCESSING 的文档
# 视为「Worker 卡死」，允许被重新抢占 / 删除 / 重建。否则 Worker 崩一次就永久
# 锁死——正是教程里「永远卡在处理中且不报错」的坑。
DOCUMENT_LEASE_SECONDS = 600


def create(db: Session, doc: Document) -> Document:
    db.add(doc)
    db.flush()
    return doc


def get_by_id(db: Session, document_id: str) -> Document | None:
    """按主键取，**不带租户过滤**：只给 Worker 任务用。

    Worker 收到的 ``document_id`` 来自已经过租户校验的投递（上传 / 重建端点），
    任务内部再查一次只认 id；任何「调用方直传 id」的数据面入口都必须改用
    :func:`get_by_id_for_tenant`，否则会跨租户读到文档（ADR-0006）。
    """

    return db.get(Document, document_id)


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
    """该租户在该库下、处于指定状态的文档，按创建时间升序。

    运维脚本「全库重建」（``scripts/reindex_knowledge_base.py``）用它取出所有
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

    判定与 CAS 的可抢占条件完全一致：``index_claimed_at IS NULL`` 是「已入队但
    Worker 还没开工」，早于 ``now - lease_seconds`` 是「Worker 崩了 / 卡住」。
    有效租约内的处理中不算卡死，不能被这条查询救回。

    这是运维兜底脚本 ``scripts/requeue_stuck_documents.py`` 的查询口：默认扫全库
    （``tenant_id`` 缺省为 None，仅有运维权限的本地脚本会这样用），``kb_id`` 只
    缩小范围，不改变「卡死」的定义。
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


def update_status(db: Session, doc: Document, status: int, error_message: str | None = None) -> None:
    doc.status = status
    doc.error_message = error_message
    db.flush()


def try_claim_for_indexing(
    db: Session,
    document_id: str,
    *,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """单条 ``UPDATE ... WHERE`` 抢占索引权；``rowcount > 0`` 即抢到。

    可抢占的条件全部写在同一个 WHERE 里：失败可重试；处理中但 ``index_claimed_at``
    为空（已入队未开工）或早于 ``now - lease_seconds``（Worker 卡死）可抢占；
    ``allow_success`` 时成功文档也允许显式 force 重建。抢到即写入 ``status=3`` 与
    新的抢占时间，并清掉上一次的 ``error_message``。
    调用方提交事务后立刻释放行锁，不要在这里包住 embedding。
    """

    claimed_at = now()
    lease_before = claimed_at - timedelta(seconds=lease_seconds)

    claimable = [
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

    stmt = (
        update(Document)
        .where(Document.id == document_id, or_(*claimable))
        .values(
            status=DocumentStatus.PROCESSING,
            index_claimed_at=claimed_at,
            error_message=None,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    return int(db.execute(stmt).rowcount or 0) > 0


def is_lease_alive(document: Document, *, lease_seconds: int = DOCUMENT_LEASE_SECONDS) -> bool:
    """纯函数：这份文档是否仍处于「有效处理中」。

    ``status=3`` 且 ``index_claimed_at`` 在 ``[now - lease_seconds, now]`` 窗口内才算
    有人真的在跑。``index_claimed_at is None``（已入队未开工）与租约过期（Worker
    卡死）都不算，因此允许被删除 / 重建 / 重新抢占。
    """

    if document.status != DocumentStatus.PROCESSING:
        return False
    if document.index_claimed_at is None:
        return False
    return document.index_claimed_at >= now() - timedelta(seconds=lease_seconds)
