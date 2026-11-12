"""API Key 数据访问：唯一能碰 api_keys 表的一层。

只 flush，不 commit——事务由 Service 收口。

**两种会话并存是迁移期的形状**（第 11 步票 #130）：判定内核与控制面（Key 列表 / 删 Key）
在异步栈上（:func:`get_by_value` / :func:`get_by_id` / :func:`list_by_tenant` /
:func:`delete`），而 ``api_key_service.create_api_key_row`` 仍有一个同步调用方——运维脚本
``scripts/issue_api_key.py``。于是「建一行」有同步 :func:`create` 与异步
:func:`create_async` 两份，等脚本随 #134 搬完，同步那一份消失。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey


def create(db: Session, api_key: ApiKey) -> ApiKey:
    """同步面：运维脚本签发 Key 用（请求路径走 :func:`create_async`）。"""

    db.add(api_key)
    db.flush()
    return api_key


async def create_async(db: AsyncSession, api_key: ApiKey) -> ApiKey:
    """:func:`create` 的异步孪生（控制面签发 Key，票 #130）。"""

    db.add(api_key)
    await db.flush()
    return api_key


async def get_by_value(db: AsyncSession, key_value: str) -> ApiKey | None:
    """鉴权路径的唯一查询：按原文一击命中（本期明文，见 ADR-0004）。"""

    statement = select(ApiKey).where(ApiKey.key_value == key_value)
    return (await db.execute(statement)).scalars().first()


async def get_by_id(db: AsyncSession, key_id: str) -> ApiKey | None:
    """控制面按 key_id 找一行；不存在与「不属于本租户」由服务层合成同一个答案。"""

    return await db.get(ApiKey, key_id)


async def list_by_tenant(db: AsyncSession, tenant_id: str) -> list[ApiKey]:
    """本租户的全部 Key，按签发时间排序。

    排序只为让面板与两次读的结果稳定；它不参与任何判定。**故意不提供跨租户列表**：
    「看别人的 Key」不是本票给管理员的能力（管理员连这个接口都进不去，见服务层）。
    """

    statement = (
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at, ApiKey.id)
    )
    return list((await db.execute(statement)).scalars().all())


async def delete(db: AsyncSession, api_key: ApiKey) -> None:
    """只删 Key 这一行；配置行不在这里出现——删 Key 不删配置（结构上独立）。"""

    await db.delete(api_key)
    await db.flush()
