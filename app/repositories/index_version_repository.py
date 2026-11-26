"""索引构建版本的数据访问：唯一能碰 ``index_build_versions`` 的一层。

只 flush，不 commit——事务由 Service 收口（与其余仓储同一口径）。

**这里没有「按 id 取一行」的函数**，只有 :func:`get_for_tenant`：租户是**无默认值的
强制参数**。版本行的状态机是「一件构建」的全部证据，跨租户读到它等于读到别人库的
重建历史（ADR-0006）。

**并发约束靠库、不靠这里**：同一库同一时刻最多一个 ``building`` / 一个 ``active`` 是
两条部分唯一索引（迁移 ``e41b7c9d2a63``）。本模块的 :func:`find_building` 只是让拒绝
变得**可读**（能告诉调用方「已经在建了」）；真正兜住并发的是那条索引，命中时由
``app.services.index_build_service`` 把 ``IntegrityError`` 翻译成业务异常。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_BUILDING,
    IndexBuildVersion,
)


async def create(db: AsyncSession, row: IndexBuildVersion) -> IndexBuildVersion:
    """建一行版本（只 flush，不提交）。

    ``flush`` 会**立刻**把 INSERT 发出去，于是两条部分唯一索引当场生效：并发的第二次
    构建在这里就撞上 ``IntegrityError``，而不是等到任务跑完才发现自己建了两份。
    """

    db.add(row)
    await db.flush()
    return row


async def get_for_tenant(
    db: AsyncSession, build_id: str, *, tenant_id: str
) -> IndexBuildVersion | None:
    """按 ``id`` **且** ``tenant_id`` 取一行；缺失与跨租户一律 ``None``（ADR-0006）。"""

    statement = select(IndexBuildVersion).where(
        IndexBuildVersion.id == build_id,
        IndexBuildVersion.tenant_id == tenant_id,
    )
    result = await db.execute(statement)
    row: IndexBuildVersion | None = result.scalar_one_or_none()
    return row


async def list_for_kb(db: AsyncSession, kb_id: str, *, tenant_id: str) -> list[IndexBuildVersion]:
    """该库的全部版本行（按创建时间升序）——「还能回到哪一版」的清单。"""

    statement = (
        select(IndexBuildVersion)
        .where(
            IndexBuildVersion.kb_id == kb_id,
            IndexBuildVersion.tenant_id == tenant_id,
        )
        .order_by(IndexBuildVersion.created_at)
    )
    return list((await db.execute(statement)).scalars().all())


async def find_active(db: AsyncSession, kb_id: str, *, tenant_id: str) -> IndexBuildVersion | None:
    """该库当前生效的那一行（部分唯一索引保证最多一行）。"""

    statement = select(IndexBuildVersion).where(
        IndexBuildVersion.kb_id == kb_id,
        IndexBuildVersion.tenant_id == tenant_id,
        IndexBuildVersion.status == INDEX_BUILD_ACTIVE,
    )
    result = await db.execute(statement)
    return result.scalars().first()


async def list_stalled(
    db: AsyncSession, *, tenant_id: str, started_before: datetime
) -> list[IndexBuildVersion]:
    """``building`` 且开始得比 ``started_before`` 还早的行 = **可能有行、没有任务**。

    「有没有任务」在库里读不到（代理是外部的）：一行 ``building`` 可能是正在跑，
    也可能是进程被杀了。唯一可用的信号是**年龄**——超过一个构建的合理时长仍然是
    ``building``，就当它没人管，交给运维裁决（重跑或标失败）。
    """

    statement = (
        select(IndexBuildVersion)
        .where(
            IndexBuildVersion.tenant_id == tenant_id,
            IndexBuildVersion.status == INDEX_BUILD_BUILDING,
            IndexBuildVersion.created_at < started_before,
        )
        .order_by(IndexBuildVersion.created_at)
    )
    return list((await db.execute(statement)).scalars().all())


async def list_overdue(
    db: AsyncSession,
    *,
    at: datetime,
    tenant_id: str | None = None,
    kb_id: str | None = None,
) -> list[IndexBuildVersion]:
    """过了**收敛期限**仍是 ``building`` 的行 = 「该收敛却没收敛」（#214）。

    与 :func:`list_stalled` 的区别是判据从「年龄」换成了「构建行自己的租约」：期限是按
    参与篇数估出来的（见 ``index_build_service.converge_deadline_for``），一篇一小时的库
    与一篇八秒的库不该用同一个阈值。清扫器（后续票）要问的正是这个：**谁的死线过了**。

    ``tenant_id`` 可省略，是给跨租户的清扫器留的唯一一处「不带租户读」：它**只读**、不改
    任何行，每一行都自己带着 ``tenant_id``，真正的写动作仍由调用方带着明确租户去做
    （ADR-0006 要求的是「写路径不许跨租户」，不是「读不到自己的名单」）。运维脚本给
    ``--tenant-id`` 时照旧收窄。
    """

    statement = select(IndexBuildVersion).where(
        IndexBuildVersion.status == INDEX_BUILD_BUILDING,
        IndexBuildVersion.converge_deadline < at,
    )
    if tenant_id is not None:
        statement = statement.where(IndexBuildVersion.tenant_id == tenant_id)
    if kb_id is not None:
        statement = statement.where(IndexBuildVersion.kb_id == kb_id)
    statement = statement.order_by(IndexBuildVersion.converge_deadline)
    return list((await db.execute(statement)).scalars().all())
