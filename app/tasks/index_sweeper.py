"""索引链路的**清扫器**：把「卡住」的东西自动收掉（#225）。

## 它解决的问题

改造前，「卡住」的处置全靠人手跑脚本：一次构建停在 ``building``、一篇文档的租约过期
没人再抢——两种状态都没有任何东西会主动发现，症状是「文档一直显示索引中」「构建一直
显示重建中」，而运维只能靠用户报障来发现，再翻手册找脚本。

## 它做三件事（与 ADR-0034 决策七逐一对应）

| 症状 | 判据 | 处置 |
| --- | --- | --- |
| 构建过了收敛期限仍是 ``building`` | ``index_build_versions.converge_deadline`` | 按**真值**收尾（``converge``，``final=True``） |
| 构建还在期限内但缺篇 | 真值 vs 参与篇数快照 | 补投缺的那几篇（``dispatch(skip_existing=True)``），重投次数封顶 |
| 文档停在 ``3`` 且租约已过期 | ``documents.index_claimed_at`` | 清空租约并重投一次；超过重投上限就把这篇标成失败 |

## 为什么它住在 Worker 进程里，而不是一个 `beat` 进程（#225 的翻案与再翻案）

**第一版实现用了 ``celery beat`` + 独立队列**，理由看起来成立（`worker_process_init` 是
每个 prefork 子进程各调一次，把定时器放那里会被跑成「N 核 × 周期」）。但那与 spec 的
Out of Scope 正面冲突（「不引入 Celery beat / 新的调度基础设施」），而且代价实实在在：
多一个必须部署的进程、多一条要同步的队列、多一份运维清单。

**第二版（现在这个）按 spec 走**：回收器是 Worker 进程内的一个**守护线程**，每
``SWEEP_INTERVAL_SECONDS`` 秒醒一次，用 Redis 的 ``SET NX EX`` 抢一把**单实例锁**——抢到
才跑，抢不到就跳过。这正面回答了「N 核 × 周期」那个问题：多出来的是**抢锁失败的次数**，
不是**真的跑了几遍**。锁是短 TTL 且不主动释放（谁也别删别人的锁），代价上限是「锁过期与
本轮重叠时两个线程同时跑一次」——而 :func:`sweep_once` 的每一步都幂等（收尾有真值表守门、
重投有次数上限），重叠只可能让同一批任务被多投一次，不会被多收拾一次。

**为什么线程而不是任务**：清扫器必须能在「所有并发槽位都被长任务占满」时也跑。作为任务
它会被自己的队列堵住（那是最讽刺的失败模式）；作为线程它跟槽位无关。

## 为什么它不改状态、只调既有动作

清扫器**不发明任何新语义**：收尾调 ``IndexBuildService.converge``（幂等、真值判据、四项
计数闸门照旧），重投调既有的 ``enqueue_build_document`` / ``enqueue_document``。这样
「清扫出来的结论」与「最后一篇自己收的尾」永远是同一个结论——两个实现会在某一天给出
两个答案，而那一天正好是线上出事的日子。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final

import structlog

from app.core import redis_client
from app.core.config import settings
from app.db.base import now
from app.db.session_factory import async_session_factory
from app.db.worker_loop import run_in_worker_loop
from app.repositories import document_repository
from app.services.index_build_service import MAX_REDISPATCH, IndexBuildService
from app.services.index_dispatch import enqueue_document

logger = structlog.get_logger(__name__)

#: 清扫周期（秒）。一分钟：比任何合理的人工反应都快，而期限的最小值也有 15 分钟，所以
#: 正常跑着的构建永远扫不到（不会误伤）。
SWEEP_INTERVAL_SECONDS: Final[int] = 60

#: 单实例锁的键与 TTL。TTL 比周期长一半：持锁那一位跑完之前不会被别人抢走，而进程被
#: ``kill -9`` 之后最多等一个周期就能有人接手（不会留下一个永远不释放的锁）。
SWEEP_LOCK_KEY: Final[str] = "lock:index:sweeper"
SWEEP_LOCK_TTL_SECONDS: Final[int] = SWEEP_INTERVAL_SECONDS * 2

#: 单篇文档的租约过期后最多重投几次。超过就把它标成失败——「一直重投也救不回来」必须有个
#: 尽头，否则一篇坏掉的文档会让清扫器变成永动机，而用户看到的是永远转圈。
MAX_LEASE_REDISPATCH: Final[int] = 3

#: 租约「过期」的判据：抢占时间早于 `now - LEASE_GRACE`。
#:
#: 与 ``DOCUMENT_LEASE_SECONDS`` 同一个口径，但多留一格余量——清扫器早一秒判定「没人跑」，
#: 就会和一个只是慢的正常任务撞上；而撞上的代价是「同一篇被索引两遍」（浪费一轮 embedding）。
LEASE_GRACE_SECONDS: Final[int] = 600

_sweeper_stop = threading.Event()
_sweeper_thread: threading.Thread | None = None


async def _try_lock(ttl: int = SWEEP_LOCK_TTL_SECONDS) -> bool:
    """抢单实例锁；抢到返回真。

    ``SET NX EX`` 是 Redis 里唯一的原子「抢锁」写法（两步的 ``GET`` + ``SET`` 之间会留下
    窗口，而那个窗口正好是「两个进程都以为自己抢到了」的地方）。

    **失败一律按「没抢到」处理**（包括 Redis 不可达）：「清扫器多跑一遍」比「清扫器因为
    Redis 抽风而停摆」安全得多——它做的每一步都幂等。
    """

    try:
        client = redis_client.get_redis_client()
        return bool(await client.set(SWEEP_LOCK_KEY, uuid.uuid4().hex, nx=True, ex=ttl))
    except Exception:  # noqa: BLE001 - 抢锁失败不该让清扫器停摆
        logger.warning("index sweeper could not acquire the lock; skipping this round")
        return False


async def sweep_once(*, session_factory: Callable[[], Any] | None = None) -> dict[str, int]:
    """跑一遍清扫，返回这一轮各项处置的计数（给日志与测试断言用）。

    顺序固定：**先收尾、再补投、最后救租约**。

    * 先收尾：一个过了期限的构建，重投它的缺失篇只是把注定要失败的活再跑一遍（「过期限」
      本身就是「别再等了」的结论）。
    * 再补投：期限没到但缺篇——补上就还有救。
    * 最后救租约：单篇的租约过期与构建无关，放最后。

    ``session_factory`` 只为**测试**留：它让用例能把这一趟绑到真库上，而不是绑到进程级的
    那个工厂（离线套件把它换成了不写库的替身，见 ``tests/conftest.py``）。生产路径不传，
    用进程级工厂——一个兜底任务的会话来源不该是可配置的。
    """

    factory = session_factory or async_session_factory
    async with factory()() as db:
        service = IndexBuildService()
        settled = 0
        redispatched = 0
        revived = 0

        # ── 1) 过了收敛期限仍是 building：按真值收尾 ──────────────────────
        for row in await service.list_overdue_builds(db):
            converged = await service.converge(db, row.id, tenant_id=str(row.tenant_id), final=True)
            settled += 1
            logger.info(
                "sweeper settled an overdue index build",
                build_id=row.id,
                tenant_id=str(row.tenant_id),
                kb_id=str(row.kb_id),
                version=str(row.version),
                verdict=None if converged is None else converged.status,
            )

        # ── 2) 期限未到但缺篇：补投（这是 spec 要的「排了队没人接」那条）──────
        for row in await service.list_undone_builds(db):
            if int(row.redispatch_count or 0) >= MAX_REDISPATCH:
                # 到顶了：交给下一轮的「过期收尾」或人工，不再投必然失败的消息。
                continue
            truth = await service.collect_truth(db, row=row, tenant_id=str(row.tenant_id))
            if truth.shortfall <= 0:
                continue
            dispatched = await service.dispatch(
                db, str(row.id), tenant_id=str(row.tenant_id), skip_existing=True
            )
            if dispatched:
                await service.mark_redispatched(db, str(row.id), tenant_id=str(row.tenant_id))
                redispatched += dispatched
                logger.info(
                    "sweeper redispatched missing build documents",
                    build_id=row.id,
                    tenant_id=str(row.tenant_id),
                    dispatched=dispatched,
                    shortfall=truth.shortfall,
                )

        # ── 3) 单篇租约过期：清掉租约再重投（spec 的「接了之后没声了」）────────
        stale_before = now() - timedelta(seconds=LEASE_GRACE_SECONDS)
        for document in await document_repository.list_stale_claims(
            db, claimed_before=stale_before
        ):
            attempts = int(document.redispatch_count or 0)
            if attempts >= MAX_LEASE_REDISPATCH:
                # 救不回来了：把它标成失败，让用户看到一个明确的结论，而不是永远转圈。
                await document_repository.mark_stale_as_failed(
                    db, document.id, tenant_id=str(document.tenant_id)
                )
                logger.warning(
                    "sweeper gave up on a document whose lease kept expiring",
                    document_id=document.id,
                    tenant_id=str(document.tenant_id),
                    kb_id=str(document.kb_id),
                    attempts=attempts,
                )
                continue
            # 先清租约再投：不清的话新任务 CAS 会看到「仍然新鲜的租约」而抢不到，
            # 重投变成一次静默 no-op（症状与「清扫器没跑」一模一样）。
            await document_repository.clear_stale_claim(
                db, document.id, tenant_id=str(document.tenant_id)
            )
            enqueue_document(
                document_id=str(document.id),
                tenant_id=str(document.tenant_id),
                kb_id=str(document.kb_id),
                request_id=None,
            )
            revived += 1
            logger.info(
                "sweeper requeued a document whose worker never reported",
                document_id=document.id,
                tenant_id=str(document.tenant_id),
                kb_id=str(document.kb_id),
                attempts=attempts,
            )

        # 三个阶段的写都在这一笔事务里（收尾那一步自己提交过，重复提交是 no-op）。
        # **必须提交**：不提交的话，「清租约 + 回排队中」与「重投次数加一」会随会话关闭被
        # 回滚——症状是清扫器日志说有动作、库里一行没变，与「清扫器没跑」一模一样。
        await db.commit()

        if settled or redispatched or revived:
            logger.info(
                "index sweeper finished",
                settled=settled,
                redispatched=redispatched,
                revived=revived,
            )
        return {"settled": settled, "redispatched": redispatched, "revived": revived}


def _sweeper_loop() -> None:
    """守护线程的主体：每 ``SWEEP_INTERVAL_SECONDS`` 秒抢一次锁、抢到就跑一轮。

    ``stop_event.wait(period)`` 而不是 ``time.sleep``：进程要退的时候能立刻醒来，不必等
    满一个周期（关机阶段卡住 60 秒会被误读成「收尾慢」）。
    """

    # 子进程刚起时不要立刻扫：让「启动」这件事先把连接池与配置装好，避免第一轮清扫撞上
    # 一个还没热起来的异步引擎。
    if _sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
        return
    while not _sweeper_stop.is_set():
        try:
            if run_in_worker_loop(_try_lock_in_the_worker_loop):
                run_in_worker_loop(sweep_once)
        except Exception:  # noqa: BLE001 - 清扫器自己崩了不许影响任务执行
            logger.exception("index sweeper round failed")
        if _sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
            return


async def _try_lock_in_the_worker_loop() -> bool:
    """``_try_lock`` 的协程版本（线程里不能直接 await，要提交给常驻循环）。"""

    return await _try_lock()


def start_sweeper_thread() -> None:
    """在工作进程里起清扫线程（``worker_process_init`` 调；重复调用是 no-op）。

    **每个 prefork 子进程都会调到这里**，所以会起 N 个线程——而它们抢的是同一把 Redis 锁，
    真正跑清扫的只有一个。这就是「单实例锁」的全部作用，不是「起了 N 个清扫器」。
    """

    global _sweeper_thread
    if _sweeper_thread is not None and _sweeper_thread.is_alive():
        return
    if not settings.RATE_LIMIT_ENABLED:
        # 限流开关关着时 Redis 不一定在（离线套件与评测环境就是这样）：这时清扫器不启动。
        # 清扫器是**兜底**，不是正确性依赖——真正的一致性由收敛判据与租约围栏守。
        logger.info("index sweeper not started: RATE_LIMIT_ENABLED is off")
        return
    _sweeper_stop.clear()
    _sweeper_thread = threading.Thread(target=_sweeper_loop, name="index-sweeper", daemon=True)
    _sweeper_thread.start()
    logger.info("index sweeper started", interval_seconds=SWEEP_INTERVAL_SECONDS)


def stop_sweeper_thread(*, grace: float = 5.0) -> None:
    """让清扫线程在下一个等待点醒来并退出（``worker_process_shutdown`` 调）。

    必须在常驻循环**停掉之前**调：线程会往那个循环提交协程，循环没了它就抛
    ``RuntimeError: Event loop is closed``——那不是业务错误，但会盖掉真正的退出原因。
    """

    global _sweeper_thread
    _sweeper_stop.set()
    if _sweeper_thread is not None:
        _sweeper_thread.join(timeout=grace)
        _sweeper_thread = None
    logger.info("index sweeper stopped")


__all__ = [
    "LEASE_GRACE_SECONDS",
    "MAX_LEASE_REDISPATCH",
    "SWEEP_INTERVAL_SECONDS",
    "sweep_once",
    "start_sweeper_thread",
    "stop_sweeper_thread",
]
