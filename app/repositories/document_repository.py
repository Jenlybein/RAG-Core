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
    return db.get(Document, document_id)


def list_by_kb(db: Session, kb_id: str) -> list[Document]:
    """该知识库下的全部文档，按创建时间升序（列表即轮询进度的时间线）。"""

    stmt = select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at)
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
