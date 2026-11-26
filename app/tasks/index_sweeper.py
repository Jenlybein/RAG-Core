"""索引链路的**清扫器**：把「卡住」的东西自动收掉（#225）。

## 它解决的问题

改造前，「卡住」的处置全靠人手跑脚本：一次构建停在 ``building``、一篇文档的租约过期
没人再抢——两种状态都没有任何东西会主动发现，症状是「文档一直显示索引中」「构建一直
显示重建中」，而运维只能靠用户报障来发现，再翻手册找脚本。

这一票把「发现 + 处置」做成一个周期性任务。它做三件事，各自对应一种「卡住」：

| 症状 | 判据 | 处置 |
| --- | --- | --- |
| 构建停在 ``building`` 过了收敛期限 | ``index_build_versions.converge_deadline`` | 按**真值**收尾（:meth:`converge`，``final=True``） |

## 这一票**只做**逾期收尾，另外两样去哪了

原始设计里还有两样：「缺篇未过期就补投」与「单篇租约过期就重投」。它们被**刻意**留在了
外面，理由是各自的处置已经有主，再加一条只多一处真相：

* **缺篇**：期限一到，收尾就把这一版判成 ``failed`` 并在明细里写明缺哪几篇。人工要提前
  补投，``scripts/reconcile_build.py`` 的 ``--redispatch`` 路径比清扫器更准（它按明确的
  ``build_id`` 跑，不需要跨租户扫描）。
* **单篇租约**：租约本来就是**成对**的（抢占写、成功/失败清），而新写的 lease-token 围栏
  （#213）保证迟到的任务写不进去。扫一遍「租约过期但没人接手」需要一条跨租户读，收益是
  「早几分钟让用户再点一次重建」——那一步用户自己就能做，而跨租户读的代价是长期的。

所以清扫器保持**一件事**：谁的死线过了，就给谁一个结论。少而准，比多而含糊有用。

## 为什么它不改状态、只调既有动作

清扫器**不发明任何新语义**：收尾调 ``IndexBuildService.converge``（幂等、真值判据、四项
计数闸门照旧），重投调既有的 ``enqueue_build_document`` / ``enqueue_document``。这样
「清扫出来的结论」与「最后一篇自己收的尾」永远是同一个结论——两个实现会在某一天给出
两个答案，而那一天正好是线上出事的日子。

## 为什么单独一个任务、而不是塞进 worker 的启动钩子

* 它要在**没有请求**的时候也跑（用户不访问，卡住的东西也该被收掉）；
* 它必须在所有 concurrency 槽位耗尽时也能跑（所以它自己的路由是 ``maintenance`` 队列，
  见 ``celery_app.task_routes``）——塞进 worker 的钩子会被自己的队列堵住，那是最讽刺的
  失败模式。

## 它与 ``scripts/reconcile_build.py`` 的分工

脚本是**人**触发的（一次构建出问题，运维坐下来看），清扫器是**时间**触发的（每几分钟
自动跑一遍）。两者的处置逻辑完全共用（都调同一个服务方法），差别只有触发者与范围：
脚本可以指定 ``--build-id`` 只看一次，清扫器总是扫全场。
"""

from __future__ import annotations

import structlog

from app.celery_app import celery_app
from app.db.session_factory import async_session_factory
from app.db.worker_loop import run_in_worker_loop
from app.providers.llm import set_role_resolver
from app.services import model_config_service, runtime_settings
from app.services.index_build_service import IndexBuildService
from app.utils.id_generator import new_id

logger = structlog.get_logger(__name__)

#: 清扫周期（秒）。5 分钟：比任何合理的人工反应都快，又不至于让「刚起 30 秒的构建」被
#: 反复扫（期限的最小值也有 15 分钟，所以这个周期永远扫不到「还在跑的正常构建」）。
SWEEP_INTERVAL_SECONDS: int = 5 * 60

#: 单篇文档的租约过期后最多重投几次（与构建的重投上限分开：两条路的风险不同）。
MAX_LEASE_REDISPATCH: int = 3


async def sweep_once() -> dict[str, int]:
    """跑一遍清扫，返回这一轮各项处置的计数（给日志与测试断言用）。

    三件事顺序固定：**先收尾、再重投**。理由：一个已经过了期限的构建，重投它的缺失篇
    只是把注定要失败的活再跑一遍（而「过期限」本身就是「别再等了」的结论）。先收掉，
    那些缺失篇就跟着那一版的 ``failed`` 一起落定，不再产生新任务。
    """

    async with async_session_factory()() as db:
        service = IndexBuildService()
        settled = 0
    
        # ── 1) 过了收敛期限仍是 building：按真值收尾 ──────────────────────
        for row in await service.list_overdue_builds(db):
            converged = await service.converge(
                db, row.id, tenant_id=str(row.tenant_id), final=True
            )
            settled += 1
            logger.info(
                "sweeper settled an overdue index build",
                build_id=row.id,
                tenant_id=str(row.tenant_id),
                kb_id=str(row.kb_id),
                version=str(row.version),
                verdict=converged.status if converged is not None else None,
            )

        # ── 2) 「缺篇但期限未到」不在这一票处置 ────────────────────────────
        #
        # 它需要**跨租户**地列出所有 ``building`` 行（而现有的两条列举都以租户为参数，
        # 因为它们同时服务「运维脚本指定一个租户」这条路）。给清扫器加一条跨租户列举，
        # 等于在写路径之外再开一个「不带租户的读」——而它带来的收益只是「早几分钟补投」：
        # 期限一到，第 1 步就会按真值把这一版收成 ``failed``，明细里写着缺哪几篇。
        # 所以这里**刻意不做**，把「还差几篇」交给期限这一条兜底（ADR-0034 决策五）。
        #
        # 要人工提前补投时，``scripts/reconcile_build.py`` 的 ``--redispatch`` 路径做得更准
        # （它按一个明确的 build_id 跑，不需要跨租户扫描）。

        return {"settled": settled}


@celery_app.task(bind=True, max_retries=0)
def sweep_index_state_task(self) -> None:
    """清扫入口：周期性任务（``celery beat`` 每 :data:`SWEEP_INTERVAL_SECONDS` 一次）。

    它**不重试自己**（``max_retries=0``）：下一次周期就是它的重试。给它加重试等于让一个
    「兜底的东西」也变成需要兜底的东西。

    Worker 进程要跟上配置与角色解析器（与其它任务同一条纪律，见 ``tasks.indexing``）。
    """

    runtime_settings.ensure_runtime_overrides()
    set_role_resolver(model_config_service.role_endpoint)
    counts = run_in_worker_loop(lambda: sweep_once())
    logger.info("index sweeper finished", **_as_log(counts))


def _as_log(counts: dict[str, int] | None) -> dict[str, int]:
    """``run_in_worker_loop`` 的返回可能是一个计数 dict；异常路径下也可能没有。"""

    return counts or {"settled": 0}


__all__ = ["MAX_LEASE_REDISPATCH", "SWEEP_INTERVAL_SECONDS", "sweep_index_state_task", "sweep_once"]
