"""反馈表的数据访问：唯一能碰 ``retrieval_feedback`` 的一层。

只有两个动作：按 ``log_id`` 取那一行（覆盖判断要用）、写入或覆盖。事务由 Service 收口
（与其它仓储一致：这里只 ``flush``，不 ``commit``）。
"""

from sqlalchemy.orm import Session

from app.db.base import now
from app.models.retrieval_feedback import RetrievalFeedback
from app.utils.id_generator import new_id


def _get_by_log_id(db: Session, log_id: str) -> RetrievalFeedback | None:
    """按 ``log_id`` 取那一行；没有就是 ``None``（第一次打分）。

    **私有，且只给 ``upsert`` 用**：它不带 ``tenant_id``，而 ADR-0006 决策 1 要求
    「Repository 方法签名强制带 ``tenant_id``」。把它留在公开面上，下一个调用者就可能
    拿它去读别的租户的行；姊妹模块 ``retrieval_trace_repository`` 因此干脆不提供
    「无租户的按 id 读取」。

    这里能不给租户，是因为 ``upsert`` 的**唯一**调用方 ``submit_feedback`` 已经先按
    ``retrieval_traces`` 校验过归属（不存在 / 跨租户都返回 ``20021``）；而 ``log_id``
    是 UUID v4、全局唯一，不存在「同一 log_id 属于两个租户」。这条不变量由
    ``tests/test_feedback.py`` 的越权用例守着。
    """

    return db.query(RetrievalFeedback).filter(RetrievalFeedback.log_id == log_id).first()


def upsert(
    db: Session,
    *,
    log_id: str,
    tenant_id: str,
    score: int,
    comment: str | None,
) -> RetrievalFeedback:
    """写入或覆盖：同一个 ``log_id`` 第二次打分**改这一行**，不再新增。

    后写覆盖是刻意的（#105 设计决定 2）：调用方重试不该变成两条互相矛盾的行。行的 ``id``
    与 ``created_at`` 保持第一次的值——它们回答的是「这份反馈什么时候开始存在」，
    而 ``score`` / ``comment`` / ``updated_at`` 回答「最后一次说了什么」。

    ``updated_at`` 在这里显式写一次，而不是只靠列的 ``onupdate``：``onupdate`` 要到 flush
    时才生效，而「覆盖之后这一行马上就是新的」不该取决于事务什么时候落盘。
    """

    row = _get_by_log_id(db, log_id)
    if row is None:
        row = RetrievalFeedback(
            id=new_id(),
            log_id=log_id,
            tenant_id=tenant_id,
            score=score,
            comment=comment,
        )
        db.add(row)
    else:
        row.score = score
        row.comment = comment
        row.updated_at = now()
    db.flush()
    return row
