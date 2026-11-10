"""反馈表的数据访问：唯一能碰 ``retrieval_feedback`` 的一层。

只有两个动作：按 ``log_id`` 取那一行（覆盖判断要用）、写入或覆盖。事务由 Service 收口
（与其它仓储一致：这里只 ``flush``，不 ``commit``）。
"""

from sqlalchemy.orm import Session

from app.db.base import now
from app.models.retrieval_feedback import RetrievalFeedback
from app.utils.id_generator import new_id


def get_by_log_id(db: Session, log_id: str) -> RetrievalFeedback | None:
    """按 ``log_id`` 取那一行；没有就是 ``None``（第一次打分）。"""

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

    row = get_by_log_id(db, log_id)
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
