"""保留期清理（第 17 步现场八）：两个**人工脚本**转成每晚自动跑一次；现场九又加了第三件
（索引代回收）。

改造前这些事都要人记得敲命令：

| 脚本 / 判据 | 清什么 | 不清的后果 |
| --- | --- | --- |
| ``scripts/purge_usage_events.py`` | ``usage_events`` 90 天、``usage_daily`` 395 天 | 明细账无限增长（每次调用一行） |
| ``scripts/purge_document_versions.py`` | 每篇文档窗口外的归档稿（连切片带版本行） | 切片越积越多：每改一稿就多一套，检索永远看不到却一直占着向量库与关键词索引 |
| :func:`purge_generations` | 每篇文档保留窗口（:data:`RETAIN_GENERATIONS`）之外的旧代切片 | 重建一次就多一整份切片与向量（含 embedding），检索只看当前代却永远留着 |

「记得敲命令」不是一种可靠性：这些事的失败方式都是**安静地慢慢变坏**（磁盘涨、检索变慢），
而它们恰好又是最没人看的那一页。所以放进本来就每 60 秒醒一次的清扫线程，每天一次。

## 四条纪律

1. **先汇总、再删明细**。删 ``usage_events`` 的历史行之前，必须确认那一天的 ``usage_daily``
   格已经在——否则删掉的是**唯一**的数据（长窗口看板只读汇总表，而汇总一旦丢了，明细也没了
   就等于永久丢失）。所以删法是**按天逐天判**：有格才删，没格跳过并记账（留给补射补上）。
   汇总本身每 5 分钟会自动跑一次并带 30 天补射（见 ``index_sweeper.rollup_usage_once``）。
2. **删除是幂等的**。多跑一次不多删：判据是「早于截止日的行」，跑完再跑这一天已经没有行了。
3. **失败隔离到单篇/单天**。一篇文档清理失败不许拖累其它篇（它的切片自会留在下一轮）；一天的
   明细删失败也不许让整个清理停摆——保留期这件事每天都在，不必一次做完。
4. **旧代回收是例外，且代价明写**。它一次调用只发两条语句（先找超窗的篇、再按篇排名删），
   所以是**整批 all-or-nothing**：语句超时或锁等待失败会让这一批整晚作废。可以接受，因为
   判据是纯函数式重算、重跑幂等，丢一晚只是旧代多留一天；要更细的隔离就得按篇开事务，
   而那是把「两条语句」换成「每篇两笔事务」。

## 为什么这里不判窗口

两处窗口都是**业务定义**，都不在本模块：

- 稿窗口（``当前稿号 - 保留稿数``）在 :func:`app.services.document_version_service.retention_plan`；
- 代窗口（``RETAIN_GENERATIONS``）在 :mod:`app.services.index_generation`。

本模块只负责「谁来做这件事、什么时候做」，判定仍走服务层，于是自动跑与人工跑**逐字同一条
规则**，不会出现两套口径。
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.transaction import async_transaction
from app.models.chunk import Chunk
from app.services.index_generation import RETAIN_GENERATIONS
from app.models.usage_daily import UsageDaily
from app.models.usage_event import UsageEvent
from app.repositories import document_repository
from app.services.document_version_service import RETAIN_VERSIONS
from app.services.document_version_service import purge_expired_versions
from app.services.usage_rollup import day_bounds

logger = structlog.get_logger(__name__)

#: ``usage_events`` 保留多少天。与 ``scripts/purge_usage_events.py`` 的默认值同一口径：
#: 长窗口看板最长 30 天，90 天留够事后排障的余量。
DEFAULT_KEEP_EVENT_DAYS: int = 90

#: ``usage_daily`` 保留多少天（约 13 个月）。汇总是一天几十行，留一年也不占地方，
#: 而「去年这个时候」这种问题只有它答得出。
DEFAULT_KEEP_DAILY_DAYS: int = 395


@dataclass
class UsageRetentionOutcome:
    """一次用量清理的结果。``skipped_days`` 是**这一天有明细却没有汇总格**、于是被放过的那几天。"""

    events_deleted: int = 0
    daily_deleted: int = 0
    skipped_days: list[str] = field(default_factory=list)


@dataclass
class VersionRetentionOutcome:
    """一次版本清理的结果：清掉的稿数、涉及的文档数、失败的文档数。"""

    documents: int = 0
    purged_versions: int = 0
    failed_documents: int = 0


async def purge_usage(
    db: AsyncSession,
    *,
    now: date | None = None,
    keep_event_days: int = DEFAULT_KEEP_EVENT_DAYS,
    keep_daily_days: int = DEFAULT_KEEP_DAILY_DAYS,
) -> UsageRetentionOutcome:
    """删掉过期的用量明细与汇总。

    ## 先汇总后删明细，而且是**按天**判

    只按「早于截止日」一刀切是危险的：某一天若从来没被汇总过（整栈停过、或上线之前），
    那一刀的删掉就是永久丢失——汇总表里没有它，明细也就没人能再压出来。

    所以逐天判：这一天在 ``usage_daily`` 里有格 → 删它的明细；没有 → **放过**，记进
    ``skipped_days`` 让上层补射（补射是每 5 分钟一次的，最多 5 分钟后这个洞就补上了）。

    ## 为什么从截止日往回扫而不是扫全历史

    清理每天都会跑一次，正常情况下降只差一两天。往回扫有一个上界（:data:`_MAX_DAYS_BACK`），
    于是「第一次上线时库里已经堆了三年明细」也不会变成一次三小时的删除——分几晚扫完即可。
    """

    moment = now or datetime.now(timezone.utc).date()
    cutoff_event_day = moment - timedelta(days=keep_event_days)
    outcome = UsageRetentionOutcome()

    # ── 1) 明细：从截止日往回逐天判（有汇总格才删）──────────────────────
    covered = {
        row
        for row in (
            await db.execute(select(UsageDaily.day).where(UsageDaily.day < cutoff_event_day).distinct())
        )
        .scalars()
        .all()
    }

    for offset in range(1, _MAX_DAYS_BACK + 1):
        day = cutoff_event_day - timedelta(days=offset - 1)
        start, end = day_bounds(day)
        has_events = (
            await db.execute(
                select(func.count()).select_from(UsageEvent).where(
                    UsageEvent.created_at >= start, UsageEvent.created_at < end
                )
            )
        ).scalar_one()
        if not has_events:
            if offset > _QUIET_STOP_DAYS:
                # 连续这么多个空天：再往前多半也是空的（明细本来就只有最近这些），收手。
                break
            continue
        if day not in covered:
            outcome.skipped_days.append(str(day))
            continue
        deleted = (
            await db.execute(
                delete(UsageEvent).where(
                    UsageEvent.created_at >= start, UsageEvent.created_at < end
                )
            )
        ).rowcount or 0
        outcome.events_deleted += int(deleted)

    # ── 2) 汇总：早于它自己的保留期整行删 ──────────────────────────────
    cutoff_daily_day = moment - timedelta(days=keep_daily_days)
    deleted = (
        await db.execute(delete(UsageDaily).where(UsageDaily.day < cutoff_daily_day))
    ).rowcount or 0
    outcome.daily_deleted = int(deleted)
    return outcome


async def purge_versions(
    db: AsyncSession, *, retain: int = RETAIN_VERSIONS, limit: int | None = None
) -> VersionRetentionOutcome:
    """把每篇文档保留窗口之外的归档稿清掉（连切片带版本行）。

    每篇文档**一笔事务**：失败只回滚这一篇，其余照走。窗口判定交给
    :func:`app.services.document_version_service.purge_expired_versions`（与人工脚本同一份规则），
    本函数只负责「遍历、记账、不让一篇坏掉的文档拖垮整晚」。
    """

    outcome = VersionRetentionOutcome()
    documents = await document_repository.list_with_expired_versions(db, retain=retain, limit=limit)
    for document in documents:
        outcome.documents += 1
        try:
            async with async_transaction(db):
                plan = await purge_expired_versions(db, document=document, retain=retain)
            outcome.purged_versions += len(plan.expired_versions)
        except Exception as exc:  # noqa: BLE001 - 一篇失败不许拖累整晚
            outcome.failed_documents += 1
            logger.warning(
                "retention could not purge archived drafts",
                document_id=str(document.id),
                error=type(exc).__name__,
            )
    return outcome


@dataclass
class GenerationRetentionOutcome:
    """一轮索引代回收的读数。

    字段名 = 数值单位，别混：``documents`` 是**被回收的篇数**，``purged_chunks`` 是**删掉
    的切片数**。清扫器的 ``generation_chunks_purged`` 取后者（⚠️ 是片，不是代——一个读数
    里装两个量纲，运维照着名字读就会得出「一晚回收了 12 代」这种错结论）。
    """

    documents: int = 0
    purged_chunks: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"documents": self.documents, "purged_chunks": self.purged_chunks}


async def purge_generations(
    db: AsyncSession,
    *,
    keep: int = RETAIN_GENERATIONS,
    limit: int | None = None,
) -> GenerationRetentionOutcome:
    """把每篇文档保留窗口之外的**旧代**切片清掉（现场九·第六步）。

    ## 判据是集合式的，不是算术减法

    保留的是「该篇**存在的**最高 N 个代」，不是「代 >= 当前代 - N + 1」。两者在代有空洞时
    给出不同答案：某篇的产代依次是 0 / 1 / 5（中间两次失败或跳过），算术减法会算成
    ``>= 4``，把第 1 代一并删掉——而它才是「上一代」，回滚要用；集合式口径保留 {5, 1}，
    删掉 0，正好是想要的那一份。

    ## 两条语句，不是 N 条

    先取「有超过 N 代的篇」（``GROUP BY document_id HAVING count(distinct generation) > N``，
    只读一列），再对这几篇按 ``DENSE_RANK() OVER (PARTITION BY document_id ORDER BY
    generation DESC)`` 一次删掉排名超窗的行。**稳态（没有超窗的篇）只花第一条**，而它不碰
    正文与向量列；超窗的篇数用 ``limit`` 封顶，于是第一次上线遇到「历史库里每篇攒了十几代」
    也不会变成一次无界的删除（同 ``purge_versions`` 的处境与理由）。

    分篇循环也能写对，但那是「有产物的文档数」条语句；这里按篇排名一次删完，代价是这些篇
    的行锁在同一笔事务里持有到提交。

    ## 为什么按文档（而不是按库）

    代是**篇级**的属性：同一个库里一篇刚重建到第 9 代、另一篇还停在第 1 代，窗口必须各算
    各的。按库取最大值会把慢那篇的唯一一份产物删掉——它还在被检索读。

    ## 失败是整批的（代价，明写在这里）

    一次调用只有一笔事务：语句超时、锁等待超时、一行坏数据都会让**这一批**整晚作废。
    接受这个代价的理由是判据本身是纯函数式重算——重跑幂等，丢一晚只是「旧代多留一天」。
    要更细的隔离就得按篇开事务（``purge_versions`` 那样），而那是 N 笔事务换来的。

    ## 事务

    **必须在调用方的事务里**（清扫器用 ``async with async_transaction(db)``）。本函数不
    commit：自己开不了事务的独立调用者拿到的是一笔没人提交的删除，关会话时静默丢弃。

    ## 依赖一条不变量

    「当前代 == 该篇存在的最大代」（写路径把切片与指针放在同一笔事务里推）。判据不读
    ``documents``，所以**单篇回滚（把指针往回写两代以上）上线时必须复核这里**：那时「存在的
    最高 N 代」可能不含指针所指的那一代，删掉它会让整篇检索归零。

    ## 关键词侧不参与

    ES 永远只镜像**当前代**（重建时按文档清空再写），库里没有多代可回收。
    """

    if keep < 1:
        raise ValueError("keep 至少是 1：0 会把当前代也删掉")

    over_window = (
        select(Chunk.document_id)
        .group_by(Chunk.document_id)
        .having(func.count(func.distinct(Chunk.generation)) > keep)
        .order_by(Chunk.document_id)
    )
    if limit is not None:
        over_window = over_window.limit(limit)
    document_ids = [row[0] for row in (await db.execute(over_window)).all()]
    if not document_ids:
        # 稳态：什么也不删，也不发第二条语句。
        return GenerationRetentionOutcome()

    ranked = (
        select(
            Chunk.id.label("id"),
            func.dense_rank()
            .over(partition_by=Chunk.document_id, order_by=Chunk.generation.desc())
            .label("rank"),
        )
        .where(Chunk.document_id.in_(document_ids))
        .subquery()
    )
    result = await db.execute(
        delete(Chunk).where(Chunk.id.in_(select(ranked.c.id).where(ranked.c.rank > keep)))
    )
    return GenerationRetentionOutcome(
        documents=len(document_ids),
        purged_chunks=int(result.rowcount or 0),
    )


#: 明细清理往回扫的上界：第一次上线时库里可能已经堆了几年的明细，分几晚扫完比一次扫死好。
_MAX_DAYS_BACK: int = 400

#: 连续多少个空天就认为「再往前没有明细了」。比 :data:`_MAX_DAYS_BACK` 小得多：
#: 正常的库每天都有调用，连续几十天空着说明明细本来就只有最近这一段。
_QUIET_STOP_DAYS: int = 30
