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