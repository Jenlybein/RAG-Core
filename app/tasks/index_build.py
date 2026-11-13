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
from app.services.index_build_service import IndexBuildService

logger = structlog.get_logger(__name__)


async def _build_in_the_worker_loop(build_id: str, *, tenant_id: str) -> None:
    """常驻循环里的一次构建：开一个任务会话 → 跑编排 → 无论如何关掉它。

    会话不跨任务复用（一个任务一笔会话），复用的是**连接池**（池归循环、会话归任务，
    ADR-0023 决策三）。
    """

    db = async_session_factory()()
    try:
        await IndexBuildService().build(db, build_id, tenant_id=tenant_id)
    finally:
        # 不能省：被放弃的 checked-out 连接即使 dispose 了也会停在 idle in transaction。
        await db.close()


def _run_build(
    self: Any,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
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
            lambda: _build_in_the_worker_loop(build_id, tenant_id=tenant_id)
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
) -> None:
    """索引构建任务入口：按 ``build_id`` 跑一次「逐文档重建 → ready」。

    去重不靠 ``task_id``：一个库同时只允许一条 ``building`` 行，那是数据库层面的事实
    （``app/services/index_build_service.py::start_build``）；即使同一个 build_id 被投递
    两次，第二次进来看见的已经是 ``ready`` 或 ``failed``，直接跳过。
    """

    _run_build(self, build_id, tenant_id=tenant_id, request_id=request_id)
