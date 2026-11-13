"""密钥配置数据访问：唯一能碰 key_policies 表的一层。

只 flush，不 commit——事务由 Service 收口。

**两种会话并存是迁移期的形状**（第 11 步票 #130）：控制面（Key 列表、改配置、改套餐的
影响面）与判定内核在异步栈上（:func:`get_by_id_async` / :func:`list_all` /
:func:`list_by_tenant`），而 ``key_policy_service``（开租户、运维脚本、签发 Key 的自洽
校验）仍在同步会话上（:func:`get_by_id` / :func:`create`）。同一个操作不会同时有两份
实现——搬一条删一条，同步面在 #134 把脚本搬完之后消失。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.key_policy import KeyPolicy


def create(db: Session, policy: KeyPolicy) -> KeyPolicy:
    """同步面：开租户与运维脚本建配置用（控制面签发走 :func:`create_async`）。"""

    db.add(policy)
    db.flush()
    return policy


async def create_async(db: AsyncSession, policy: KeyPolicy) -> KeyPolicy:
    """:func:`create` 的异步孪生（控制面签发 Key 时连带建配置，票 #130）。"""

    db.add(policy)
    await db.flush()
    return policy


def get_by_id(db: Session, policy_id: str) -> KeyPolicy | None:
    """同步面：密钥配置服务与签发脚本用。"""

    return db.get(KeyPolicy, policy_id)


async def get_by_id_async(db: AsyncSession, policy_id: str) -> KeyPolicy | None:
    """:func:`get_by_id` 的异步孪生（判定内核与控制面，票 #130）。"""

    return await db.get(KeyPolicy, policy_id)


async def list_all(db: AsyncSession) -> list[KeyPolicy]:
    """全部密钥配置。控制面「改部署能力开关的影响面」（#93）要横扫所有配置。

    按 id 排序只为让两次读的结果稳定；顺序不参与任何判定。
    """

    return list((await db.execute(select(KeyPolicy).order_by(KeyPolicy.id))).scalars().all())


async def list_by_tenant(db: AsyncSession, tenant_id: str) -> list[KeyPolicy]:
    """这个租户的全部密钥配置。控制面「改套餐影响面」要数「谁会失去档位」。

    按 id 排序只为让两次读的结果稳定；顺序不参与任何判定。
    """

    statement = (
        select(KeyPolicy).where(KeyPolicy.tenant_id == tenant_id).order_by(KeyPolicy.id)
    )
    return list((await db.execute(statement)).scalars().all())
