"""Celery 索引构建任务：Worker 进程里跑完整条「整库重建到新版本」（#142）。

接线与 ``app/tasks/indexing.py`` 逐条相同（ADR-0023）：一个进程一个常驻事件循环，
任务把**工厂函数**投进 ``run_in_worker_loop``，会话在循环里创建、跑完即关，收尾
（dispose 引擎 / 关 HTTP 客户端）由 ``WorkerLoop.shutdown`` 在
``worker_process_shutdown`` 里做，**不在任务里做**。

## 与单篇索引任务的三个差别

* **不重试**（``max_retries=0``）。重试一次整库重建的代价是一整轮 embedding，而失败
  原因（解析失败 / 向量数量不一致 / 计数对不上）多半不会因为再来一次就消失。要重建就
  另起一个新版本——那也正好留下「上一次为什么失败」的痕迹（``failed`` 行）。
* **失败自己收尾**：``IndexBuildService.build`` 把异常收成 ``failed`` + 固定原因再抛，
  任务体只负责记日志。这里**没有**「重置文档状态」这一步：文档状态是单篇的事，构建
  不碰它。
* **取消与进程死亡不对称**（见 ``app/services/index_build_service.py`` 的模块文档）：
  ``SoftTimeLimitExceeded`` 是 ``Exception``，会被收成 ``failed``；SIGKILL 则留下
  ``building`` 行，由 ``list_stalled_builds`` 按年龄指出来。两种情况都**不会**留下
  半成品 active。

``request_id`` 同样通过 structlog contextvars 带进 Worker（投递到常驻循环时上下文跟着
走），日志因此能和发起构建的那次请求串起来。
"""

from __future__ import annotations

from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.celery_app import celery_app
from app.db.session_factory import async_session_factory
from app.db.worker_loop import run_in_worker_loop
from app.providers.llm import set_role_resolver
from app.services import model_config_service, runtime_settings
from app.models.index_version import INDEX_BUILD_READY
from app.services.index_build_service import IndexBuildService

logger = structlog.get_logger(__name__)


async def _build_in_the_worker_loop(
    build_id: str, *, tenant_id: str, activate: bool = False
) -> None:
    """常驻循环里的一次构建：开一个任务会话 → 跑编排 → 无论如何关掉它。

    会话不跨任务复用（一个任务一笔会话），复用的是**连接池**（池归循环、会话归任务，
    ADR-0023 决策三）。

    ``activate``（第 16 步 #207）：构建成功后**立刻激活**。它只给「改了切块设置」
    那一条路径用——那种改动的语义就是「改完就该生效」，让人再去点一次激活等于把
    「设置页已保存」变成半句话。常规重建仍然停在 ``ready``（ADR-0025 决策三：
    先 ready、观察一会儿再切，是运维可见的一步）。
    """

    db = async_session_factory()()
    try:
        service = IndexBuildService()
        row = await service.build(db, build_id, tenant_id=tenant_id)
        if activate and row.status == INDEX_BUILD_READY:
            try:
                await service.activate(db, build_id, tenant_id=tenant_id)
            except Exception as exc:  # noqa: BLE001 - 切换失败不能把「构建成功了」抹掉
                # 构建本身是好的（``ready``），失败的是**切换**那一步（例如关键词别名切不动）。
                # 这里不能让异常冒上去：那会把一个刚建好的版本标成失败，运维再重建一次也是白建。
                # 留在 ``ready`` + 一条 warning 才是事实：版本可用，只是还没上线，可以重试激活。
                logger.warning(
                    "index build activation failed; leaving the build ready",
                    build_id=build_id,
                    tenant_id=tenant_id,
                    reason=type(exc).__name__,
                )
    finally:
        # 不能省：被放弃的 checked-out 连接即使 dispose 了也会停在 idle in transaction。
        await db.close()


def _run_build(
    self: Any,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
    activate: bool = False,
) -> None:
    """任务体：与 Celery 装饰器分离，便于用轻量 ``self`` 替身离线测试。"""

    if request_id:
        bind_contextvars(request_id=request_id)
    try:
        # Worker 进程也要跟上配置（#93）：切块窗口、嵌入批次都可能被管理员刚改过。
        runtime_settings.ensure_runtime_overrides()
        # 模型角色解析器同样要在这个进程里接上（#82），与单篇索引任务一致。
        set_role_resolver(model_config_service.role_endpoint)
        # 投**工厂**：协程在常驻循环里才被创建，从那一刻起属于那个循环。
        run_in_worker_loop(
            lambda: _build_in_the_worker_loop(build_id, tenant_id=tenant_id, activate=activate)
        )
    finally:
        if request_id:
            clear_contextvars()


@celery_app.task(bind=True, max_retries=0)
def build_index_version_task(
    self,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
    #: 构建成功后立刻激活（第 16 步 #207 的「改设置就生效」那条路径专用）。
    activate: bool = False,
) -> None:
    """索引构建任务入口：按 ``build_id`` 跑一次「逐文档重建 → ready」。

    去重不靠 ``task_id``：一个库同时只允许一条 ``building`` 行，那是数据库层面的事实
    （``app/services/index_build_service.py::start_build``）；即使同一个 build_id 被投递
    两次，第二次进来看见的已经是 ``ready`` 或 ``failed``，直接跳过。
    """

    _run_build(self, build_id, tenant_id=tenant_id, request_id=request_id, activate=activate)


# ── 按篇投递的两条任务（#215）──────────────────────────────────────────────


async def _dispatch_in_the_worker_loop(
    build_id: str, *, tenant_id: str, request_id: str | None = None
) -> None:
    """常驻循环里的一次派活：读这一版该建哪些篇，逐条投进 ``build`` 队列。"""

    db = async_session_factory()()
    try:
        await IndexBuildService().dispatch(db, build_id, tenant_id=tenant_id)
    finally:
        await db.close()


def _run_dispatch(
    self: Any,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
    activate: bool = False,
) -> None:
    """派活任务体。``activate`` 不在这里用，但要**带进子任务**：激活发生在收敛成 ready
    之后，而那一步在最后一篇子任务那一侧。所以它由派活任务原样传到每条子任务上。"""

    if request_id:
        bind_contextvars(request_id=request_id)
    try:
        runtime_settings.ensure_runtime_overrides()
        set_role_resolver(model_config_service.role_endpoint)
        run_in_worker_loop(
            lambda: _dispatch_in_the_worker_loop(
                build_id, tenant_id=tenant_id, request_id=request_id
            )
        )
    finally:
        if request_id:
            clear_contextvars()


@celery_app.task(bind=True, max_retries=0)
def dispatch_build_task(
    self,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
    activate: bool = False,
) -> None:
    """派活任务入口：``start_build`` 提交之后投的这一条（#215）。

    它**不跑索引**，只把「这一版该建哪些篇」翻译成一批子任务。把派活与执行分开，是为了让
    两边的失败不互相污染：派活失败（读库出错）不该让某几篇被标成失败，而某一篇失败也不该
    让其余篇不投。
    """

    _run_dispatch(self, build_id, tenant_id=tenant_id, request_id=request_id, activate=activate)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def build_document_task(
    self,
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    build_id: str,
    index_version: str,
    doc_version: int,
    allow_success: bool = True,
) -> None:
    """构建子任务入口：**一篇**文档写进**这一版**（#215）。

    与 ``index_document_task`` 走同一条流水线（``_run_indexing``），差别只有三处、全部由
    载荷带着走：

    * 目标版本是**这一版**，不是库当前的生效版本；
    * 失败**不改文档状态**，只把「文档 + 原因」记进构建行明细；
    * 跑完要回去推进这一版的进度并尝试收敛。

    ## 为什么单独一个任务名，而不是复用 ``index_document_task``

    Celery 的 ``task_routes`` 只能按**任务名**路由。构建子任务要进 ``build`` 队列（否则
    5000 条篇任务会把所有租户的上传淹掉），而用户发起的单篇重建应该留在默认队列——
    同一个任务名做不到这件事，所以老老实实分成两个任务名、共用同一个实现。
    """

    from app.tasks.indexing import _run_indexing

    _run_indexing(
        self,
        document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        build_id=build_id,
        index_version=index_version,
        doc_version=doc_version,
        allow_success=allow_success,
    )
