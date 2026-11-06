"""知识库数据访问：唯一能碰 knowledge_bases 表的一层。

只 flush，不 commit——事务由 Service 收口。
"""

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase


def create(db: Session, kb: KnowledgeBase) -> KnowledgeBase:
    db.add(kb)
    db.flush()
    return kb


def get_by_id(db: Session, kb_id: str) -> KnowledgeBase | None:
    """按主键取；租户是否匹配由 Service 判断（跨租户统一按「不存在」处理）。"""

    return db.get(KnowledgeBase, kb_id)


def list_by_tenant(db: Session, tenant_id: str) -> list[KnowledgeBase]:
    """列这把 Key 可见的库（第 1 步占位期：默认租户下的全部库）。"""

    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.tenant_id == tenant_id)
        .order_by(KnowledgeBase.created_at)
        .all()
    )
