"""审计数据访问：唯一能碰 audit_logs 表的一层。

只写不改：审计是账本，**没有 update 也没有 delete**——能被改的账本不算账本。
"""

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def create(db: Session, entry: AuditLog) -> AuditLog:
    db.add(entry)
    db.flush()
    return entry


def list_recent(db: Session, *, limit: int) -> list[AuditLog]:
    """按时间倒序列最近若干条；``limit`` 由调用方（路由）给，仓储不写默认值。"""

    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
