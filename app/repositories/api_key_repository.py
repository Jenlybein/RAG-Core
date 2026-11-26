"""API Key 数据访问：唯一能碰 api_keys 表的一层。

只 flush，不 commit——事务由 Service 收口。

**两种会话并存是迁移期的形状**（第 11 步票 #130）：判定内核与控制面（Key 列表 / 删 Key）
在异步栈上（:func:`get_by_id` / :func:`list_by_tenant` / :func:`delete`），而
``api_key_service.create_api_key_row`` 仍有一个同步调用方——运维脚本
``scripts/issue_api_key.py``。于是「建一行」有同步 :func:`create` 与异步
:func:`create_async` 两份长期并存：同步那份服务运维脚本，异步那份服务请求路径。
"""

from datetime import datetime

from sqlalchemy import select, update
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


async def get_by_id(db: AsyncSession, key_id: str) -> ApiKey | None:
    """**鉴权路径与控制面共用的唯一读法**：按主键找一行。

    两个调用方各自关心的东西不同——鉴权要 ``key_hash`` / ``status`` / ``tenant_id``，
    控制面要 ``label`` / ``policy_id``——但「怎么找到这一行」只有一条路。以前这里是
    「按明文等值查」（``get_by_value``），库里只存哈希之后那条路不复存在：哈希不能做索引，
    行身份证由 Key 串自己携带（``rc_live_<id>.<secret>``）。

    不存在与「不属于本租户」由服务层合成同一个答案。
    """

    return await db.get(ApiKey, key_id)


async def list_by_tenant(db: AsyncSession, tenant_id: str) -> list[ApiKey]:
    """本租户的全部 Key，按签发时间排序。

    排序只为让面板与两次读的结果稳定；它不参与任何判定。**故意不提供跨租户列表**：
    「看别人的 Key」不是本票给管理员的能力（管理员连这个接口都进不去，见服务层）。
    """

    statement = (
        select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at, ApiKey.id)
    )
    return list((await db.execute(statement)).scalars().all())


async def delete(db: AsyncSession, api_key: ApiKey) -> None:
    """只删 Key 这一行；配置行不在这里出现——删 Key 不删配置（结构上独立）。"""

    await db.delete(api_key)
    await db.flush()


async def touch_last_used(db: AsyncSession, key_id: str, used_at: datetime) -> None:
    """把「最后一次调用时间」写到这把 Key 上（第 14 步票 #200）。

    这一列从建表那天起就存在，却**从来没有被任何代码写过**——面板上「最后一次调用」
    一直是空的。用量记账顺手补它，是因为记账点恰好知道「这一刻这把 Key 用了一次」，
    再加一次 SELECT 去读这一行没有意义：直接 UPDATE。

    用 UPDATE 而不是读出来改：这一列只关心最后一次，谁先谁后都对；读改写会在并发下
    互相覆盖，还要多承担一次往返。``synchronize_session=False`` 是有意的——
    调用方（独立短事务）用完就关会话，不需要在 identity map 里同步这个值。
    """

    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=used_at)
        .execution_options(synchronize_session=False)
    )
