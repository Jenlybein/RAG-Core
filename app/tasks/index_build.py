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


# ── 按篇投递的两条任务（#215）──────────────────────────────────────────────


async def _dispatch_in_the_worker_loop(
    build_id: str, *, tenant_id: str, request_id: str | None = None
) -> None:
    """常驻循环里的一次派活：读这一版该建哪些篇，逐条投进 ``build`` 队列。"""

    db = async_session_factory()()
    try:
        # `settle_when_nothing_to_send=True`：派活是这一轮构建的**唯一入口**，它投完就该
        # 有结论（空库 → ready 空版本；有文档但协调不下来 → 交给守卫判 failed）。清扫器的
        # 补投路径**不传**这个开关——它每 60 秒跑一次，撞上「最后一篇正在处理」时参与集
        # 必然为空，那时收尾会打死一个健康的构建。
        await IndexBuildService().dispatch(
            db, build_id, tenant_id=tenant_id, settle_when_nothing_to_send=True
        )
    finally:
        await db.close()


def _run_dispatch(
    self: Any,
    build_id: str,
    *,
    tenant_id: str,
    request_id: str | None = None,
) -> None:
    """派活任务体。

    **它没有 ``activate`` 参数**：要不要自动激活记在**构建行**上（``auto_activate``，迁移
    ``f1a4b8d6c392``），由 ``converge`` 读到——因为收敛可能由最后一篇子任务、清扫器或运维
    脚本任意一方触发，把布尔量塞进这一条消息的载荷里，换任何一条触发路径都会丢。

    （第一版实现确实把它当载荷一路带着，然后在改按篇投递时半路丢了：症状是「改了切块设置、
    构建跑完停在 ready 不激活」。评审把这一跳补回构建行，死参数一并删掉。）
    """

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
) -> None:
    """派活任务入口：``start_build`` 提交之后投的这一条（#215）。

    它**不跑索引**，只把「这一版该建哪些篇」翻译成一批子任务。把派活与执行分开，是为了让
    两边的失败不互相污染：派活失败（读库出错）不该让某几篇被标成失败，而某一篇失败也不该
    让其余篇不投。
    """

    _run_dispatch(self, build_id, tenant_id=tenant_id, request_id=request_id)


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
