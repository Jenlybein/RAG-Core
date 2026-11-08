"""知识库数据访问：唯一能碰 knowledge_bases 表的一层。

只 flush，不 commit——事务由 Service 收口。
"""

from sqlalchemy.orm import Session

from app.models.knowledge_base import (
    DEFAULT_FORMATS_BY_STRATEGY,
    SPLIT_STRUCTURE,
    KnowledgeBase,
    formats_of,
    settings_of,
)


def create(db: Session, kb: KnowledgeBase) -> KnowledgeBase:
    db.add(kb)
    db.flush()
    return kb


def get_by_id(db: Session, kb_id: str) -> KnowledgeBase | None:
    """按主键取；租户是否匹配由 Service 判断（跨租户统一按「不存在」处理）。"""

    return db.get(KnowledgeBase, kb_id)


def get_split_config(
    db: Session, kb_id: str, *, tenant_id: str
) -> tuple[str, int, list[str]]:
    """取该库的切块配置 (strategy, chunk_size, allowed_formats)。

    库不存在或跨租户时回落到默认值，而不是抛错：索引任务是异步的，库在这期间
    被删掉时应当按默认配置继续尝试，让后续流程报出「库不存在」这个真正的原因；
    在取配置这一步先炸只会把原因盖掉。

    策略列非空，这里不再有「缺省就是 structure」的回退——那种回退会让
    「没有策略」这个本该不存在的情况被悄悄掩盖。
    """

    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        values = settings_of(None)
        return (
            SPLIT_STRUCTURE,
            values["chunk_size"],
            list(DEFAULT_FORMATS_BY_STRATEGY[SPLIT_STRUCTURE]),
        )
    values = settings_of(kb)
    return kb.split_strategy, values["chunk_size"], formats_of(kb)


def list_by_tenant(db: Session, tenant_id: str) -> list[KnowledgeBase]:
    """列这把 Key 可见的库（第 1 步占位期：默认租户下的全部库）。"""

    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.tenant_id == tenant_id)
        .order_by(KnowledgeBase.created_at)
        .all()
    )
