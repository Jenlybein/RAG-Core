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

from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
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


# ── #215：构建子任务的记账（三条都是 UPDATE，不是「读出来改再写回去」）──


async def advance_progress(db: AsyncSession, build_id: str, *, tenant_id: str, by: int = 1) -> int:
    """进度 +N，只对 ``building`` 的行生效；返回是否真的动了（``rowcount > 0``）。

    **为什么是语句而不是「读出来改再写回去」**：``documents_done`` 是并发写的——多个构建
    子任务各跑在自己的会话里，读-改-写必然丢更新，而丢的那个恰好是进度条，症状是「跑完了
    显示 37 / 500」。让数据库做加法，并发就不需要任何锁。

    它**只服务进度条**：收尾判据读真值（``chunks`` 里已有切片的文档数），不读它。这条分工
    写在 ``decide_convergence`` 的注释里，别处不许拿它冒充判据。
    """

    statement = (
        update(IndexBuildVersion)
        .where(
            IndexBuildVersion.id == build_id,
            IndexBuildVersion.tenant_id == tenant_id,
            # 只有还在建的行才推：迟到的子任务不许把一次已经收尾的构建的进度改花。
            IndexBuildVersion.status == INDEX_BUILD_BUILDING,
        )
        .values(documents_done=IndexBuildVersion.documents_done + by)
        .execution_options(synchronize_session=False)
    )
    return int((await db.execute(statement)).rowcount or 0) > 0


async def record_failure(db: AsyncSession, build_id: str, *, tenant_id: str, entry: dict) -> bool:
    """把一条「未入新版本」追加进明细，并把进度 +1；返回是否真的动了。

    ## 为什么要 +1

    失败的篇**也是跑完了的**一篇：进度条数的是「还剩多少没结果」，把失败排除在外会让
    「37 / 500」永远到不了 500，读起来像还有 463 篇在排队。

    ## 为什么明细用 JSONB 的 ``||`` 追加

    与进度同理：并发写。读-改-写会丢掉别人的条目，而丢掉的那条恰好是运维要查的那篇。
    ``COALESCE`` 处理 NULL（迁移前的老行没有这一格）。
    """

    merge = func.jsonb_build_array(cast(entry, JSONB))
    statement = (
        update(IndexBuildVersion)
        .where(
            IndexBuildVersion.id == build_id,
            IndexBuildVersion.tenant_id == tenant_id,
            IndexBuildVersion.status == INDEX_BUILD_BUILDING,
        )
        .values(
            documents_done=IndexBuildVersion.documents_done + 1,
            failed_documents=func.coalesce(
                IndexBuildVersion.failed_documents, cast("[]", JSONB)
            ).op("||")(merge),
        )
        .execution_options(synchronize_session=False)
    )
    return int((await db.execute(statement)).rowcount or 0) > 0


async def bump_redispatch(db: AsyncSession, build_id: str, *, tenant_id: str) -> int:
    """重投次数 +1，返回新的值（行不存在或已收尾时返回 0）。

    清扫器靠它给自己设上限：一个永远救不回来的构建每轮都会数出同一个缺口，没有次数上限
    就等于把清扫器变成一台永动机（ADR-0034 决策七）。
    """

    statement = (
        update(IndexBuildVersion)
        .where(
            IndexBuildVersion.id == build_id,
            IndexBuildVersion.tenant_id == tenant_id,
            IndexBuildVersion.status == INDEX_BUILD_BUILDING,
        )
        .values(redispatch_count=IndexBuildVersion.redispatch_count + 1)
        .returning(IndexBuildVersion.redispatch_count)
        .execution_options(synchronize_session=False)
    )
    value = (await db.execute(statement)).scalar_one_or_none()
    return int(value or 0)


async def list_undone(
    db: AsyncSession,
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    limit: int = 200,
) -> list[IndexBuildVersion]:
    """所有**还在建**的构建（清扫器「缺篇补投」那条要的名单，#225）。

    与 :func:`list_overdue` 的差别是判据：那条问「谁的死线过了」，这条问「谁还没建完」。
    清扫器两样都要——过了期限的收尾，没过期限但缺篇的补投。

    ``tenant_id`` 可省略是给清扫器留的唯一一处「不带租户读」：它**只读**、每行自带
    ``tenant_id``，写动作仍由调用方逐行带明确租户去做（同一理由见 :func:`list_overdue`）。

    ``limit`` 让「一轮清扫」有可预期的时长：上千个构建同时卡住时，剩下的交给下一轮。
    """

    statement = select(IndexBuildVersion).where(IndexBuildVersion.status == INDEX_BUILD_BUILDING)
    if tenant_id is not None:
        statement = statement.where(IndexBuildVersion.tenant_id == tenant_id)
    if kb_id is not None:
        statement = statement.where(IndexBuildVersion.kb_id == kb_id)
    statement = statement.order_by(IndexBuildVersion.created_at).limit(limit)
    return list((await db.execute(statement)).scalars().all())
