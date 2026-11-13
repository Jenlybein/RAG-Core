"""租户数据访问：唯一能碰 tenants 表的一层。

只 flush，不 commit——事务由 Service 收口。

**两种会话并存是迁移期的形状**（第 11 步票 #130）：控制面的读（账号清单、套餐影响面、
「我的视图」）与判定内核在异步栈上（:func:`get_by_id_async` / :func:`list_by_plan_id`），
而运维脚本与 ``tenant_service``（开租户）仍在同步会话上（:func:`get_by_id` /
:func:`create` / :func:`existing_tenant_ids`）。同一个操作不会同时有两份实现：搬完一条，
它的同步面就消失。
"""

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.retrieval_trace import RetrievalTrace
from app.models.tenant import Tenant

# 「升级前就已经存在的租户」只能从既有四张业务表里认出来：那时候没有 tenants 表，
# 租户就是一个写在业务行上的字符串。四个来源都要看，不能只看 knowledge_bases——
# 手工造过文档或日志、却没有库的租户是真实存在的。
_TENANT_SOURCES = (KnowledgeBase, Document, Chunk, RetrievalTrace)


def create(db: Session, tenant: Tenant) -> Tenant:
    db.add(tenant)
    db.flush()
    return tenant


def get_by_id(db: Session, tenant_id: str) -> Tenant | None:
    """同步面：开租户、签发 Key 与运维脚本用（它们手里是同步会话）。"""

    return db.get(Tenant, tenant_id)


async def get_by_id_async(db: AsyncSession, tenant_id: str) -> Tenant | None:
    """:func:`get_by_id` 的异步孪生（控制面与判定内核，票 #130）。"""

    return await db.get(Tenant, tenant_id)


async def list_by_plan_id(db: AsyncSession, plan_id: int) -> list[Tenant]:
    """这个套餐上挂着哪些租户：控制面「删除被引用的套餐」与「改套餐影响面」的入口。

    返回整行而不是只回 id：影响面预览要拿租户名给人看（「哪两个账号会当场超限」），
    而再按 id 逐个回查一遍正好是 N+1。
    """

    statement = select(Tenant).where(Tenant.plan_id == plan_id).order_by(Tenant.id)
    return list((await db.execute(statement)).scalars().all())


def existing_tenant_ids(db: Session) -> list[str]:
    """既有四张业务表上出现过的租户标识（只读，不去重写它们的数据）。

    用 ``UNION`` 让数据库去重，不在 Python 里拉全表回来再 ``set``：既有表里
    ``tenant_id`` 的重复度极高（一行一个切片），拉回来是白白的内存与时间。
    """

    statement = union(*(select(source.tenant_id) for source in _TENANT_SOURCES))
    return [str(value) for value in db.execute(statement).scalars()]
