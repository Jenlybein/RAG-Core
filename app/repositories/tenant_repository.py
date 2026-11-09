"""租户数据访问：唯一能碰 tenants 表的一层。

只 flush，不 commit——事务由 Service 收口。
"""

from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.retrieval_log import RetrievalLog
from app.models.tenant import Tenant

# 「升级前就已经存在的租户」只能从既有四张业务表里认出来：那时候没有 tenants 表，
# 租户就是一个写在业务行上的字符串。四个来源都要看，不能只看 knowledge_bases——
# 手工造过文档或日志、却没有库的租户是真实存在的。
_TENANT_SOURCES = (KnowledgeBase, Document, Chunk, RetrievalLog)


def create(db: Session, tenant: Tenant) -> Tenant:
    db.add(tenant)
    db.flush()
    return tenant


def get_by_id(db: Session, tenant_id: str) -> Tenant | None:
    return db.get(Tenant, tenant_id)


def existing_tenant_ids(db: Session) -> list[str]:
    """既有四张业务表上出现过的租户标识（只读，不去重写它们的数据）。

    用 ``UNION`` 让数据库去重，不在 Python 里拉全表回来再 ``set``：既有表里
    ``tenant_id`` 的重复度极高（一行一个切片），拉回来是白白的内存与时间。
    """

    statement = union(*(select(source.tenant_id) for source in _TENANT_SOURCES))
    return [str(value) for value in db.execute(statement).scalars()]
