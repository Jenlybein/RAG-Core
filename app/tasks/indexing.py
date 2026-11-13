"""Celery 索引任务：Worker 进程里跑完整条索引流水线。

**每个 Worker 进程一个常驻事件循环**（ADR-0023）。任务把**工厂函数**投进
``get_worker_loop().run(...)``：协程与它用到的连接从被调用的那一刻起就属于那个循环，
数据库连接因此在**任务之间复用**（探针读数见 ADR-0023：两个任务一条物理连接）。收尾
——``dispose`` 引擎、关掉这个循环名下的 HTTP 客户端——由 ``WorkerLoop.shutdown`` 在
``worker_process_shutdown`` 里做，**不在任务里做**。

**任务体里不许再关 HTTP 客户端。** 改造前 ``_index_in_its_own_loop`` 在 ``finally`` 里
``await aclose_http_client()``——那是为「每任务一个循环」写的。循环一旦常驻，
那句话等于**每个任务都把共享客户端关掉**，复用立刻归零，而症状是「每任务重新握手」
这种没人会当故障的现象（ADR-0023「决策一 · 代价」）。同理，``finally`` 里关**会话**
不能省：被放弃的 checked-out 连接连 ``dispose`` 都救不回来（会停在
``idle in transaction``），只有关会话能让它回到池里。

失败分两类：
* **可重试**（``RETRYABLE_ERROR_CODES``：模型超时 / 请求失败 / embedding 失败 /
  系统繁忙）且还有次数 → 先 ``release_index_claim`` 释放租约并提交，再
  ``self.retry(exc=exc)``（Celery 默认 10 秒后再投一次）。不释放的话，下一次尝试
  会看到仍然新鲜的租约而抢不到，重试永远不会真正执行；
* 其余（解析失败、内容为空、文档不存在、向量数量不一致等）或次数用尽 →
  ``_mark_failed`` 回写 ``status=2`` + 清空 ``index_claimed_at`` + 脱敏固定文案。
  异常原文只进 ``logger.exception`` 的堆栈，绝不落库、绝不回给调用方。

``request_id`` 通过 structlog contextvars 带进 Worker：上传请求与后台 Worker 是两个
进程，日志靠同一个号牌才串得起来（``RequestContextMiddleware`` 在请求侧写它）。投递到
常驻循环时上下文**跟着走**——``asyncio.run_coroutine_threadsafe`` 会复制调用方的
context，所以流水线里的日志仍带 ``request_id``（用例见 ``tests/test_indexing_task.py``）。

两条日志名 ``document indexed`` / ``document indexing failed`` 是既有排障习惯与
失败留痕契约的一部分，改名前先确认没有脚本或文档依赖它们。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.celery_app import celery_app
from app.core.exceptions import AppError, ErrorCode
from app.db.session_factory import async_session_factory
from app.db.transaction import async_commit_now, async_rollback_now
from app.db.worker_loop import get_worker_loop
from app.providers.llm import set_role_resolver
from app.repositories import document_repository
from app.services.indexing_service import INDEXING_FAILED_MESSAGE, IndexingService
from app.services import model_config_service, runtime_settings
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

# 白名单：只有外部依赖的**瞬时**失败才值得重试。解析失败、内容为空、文档不存在
# 这类错误再跑一次也不会变好，重试只是把失败延迟三倍并多烧三次配额。
RETRYABLE_ERROR_CODES = {
    ErrorCode.API_TIMEOUT,
    ErrorCode.API_REQUEST_ERROR,
    ErrorCode.EMBEDDING_FAILED,
    ErrorCode.SYSTEM_BUSY,
}


def _is_retryable(exc: BaseException) -> bool:
    """这个异常是否属于「再试一次可能就好」的那一类。

    纯函数，不碰数据库、不碰 Celery：任务体的重试决策与测试的真值表都读它。
    """

    return isinstance(exc, AppError) and exc.code in RETRYABLE_ERROR_CODES


async def _mark_failed(
    db: AsyncSession, document_id: str, exc: BaseException | None = None
) -> None:
    """失败留痕：回滚半成品事务，另起一段写 ``status=2`` + 清租约 + 脱敏固定文案。

    ``exc`` 只为与调用处对齐，**绝不**写进 ``error_message``：失败原因只在
    ``logger.exception`` 的堆栈里，数据库只存固定文案（不含异常对象、路径或 SQL）。
    清 ``index_claimed_at`` 是为了让失败终态也不留下新鲜租约。
    """

    await async_rollback_now(db)
    doc = await document_repository.get_by_id_async(db, document_id)
    if doc is None:
        return
    await document_repository.update_status_async(
        db, doc, DocumentStatus.FAILED, INDEXING_FAILED_MESSAGE
    )
    # 失败终态同样不保留租约：下一次重建 / 重试不能看到一份「新鲜的失败租约」。
    doc.index_claimed_at = None
    await async_commit_now(db)


class _RetryRequested(Exception):
    """「该重试」的判定结果：循环里只判定与收尾，**真正的 ``retry()`` 回到任务线程**。

    为什么不能就在循环里调 ``self.retry()``：Celery 的 ``self.request`` 是**线程局部**的
    （``task.request_stack``）。在 Worker 常驻循环那条线程里读它，看到的是一个空的请求
    上下文——``retries`` 恒为 0、没有 task id，于是 ``retry()`` 要么抛
    ``RuntimeError``，要么投出一条没有 id 的消息，重试**静默失效**（而状态也不会变成 2）。
    所以：判定所需的 ``retries`` / ``max_retries`` 由任务线程读好、以普通整数传进来；
    循环里做完「回滚 → 释放租约 → 提交」之后抛这个信号，由任务线程接手 ``retry()``。
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


async def _settle_failure(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    exc: BaseException,
    retries: int,
    max_retries: int,
) -> None:
    """任务失败后的两选一：可重试就释放租约并抛 :class:`_RetryRequested`，否则写失败终态。

    两条分支都跑在**同一个任务会话**上（就是流水线刚用完的那个），所以「回滚半成品 →
    释放租约 → 提交」是一笔连续的收尾，而不是另开一段。
    """

    if _is_retryable(exc) and retries < max_retries:
        # 先释放租约再重试：否则下一次尝试的 CAS 会看到仍然新鲜的租约而
        # 抢不到，重试变成静默 no-op（status 也不会变成 2）。
        await async_rollback_now(db)
        await document_repository.release_index_claim_async(
            db, document_id, tenant_id=tenant_id
        )
        await async_commit_now(db)
        raise _RetryRequested(exc)
    await _mark_failed(db, document_id, exc)
    logger.exception(
        "document indexing failed",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
    )


async def _index_in_the_worker_loop(
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    reparse: bool,
    retries: int,
    max_retries: int,
) -> None:
    """常驻循环里的一次索引：**开一个任务会话 → 跑流水线 → 无论如何关掉它**。

    会话不跨任务复用（一个任务一笔会话、一笔事务边界），复用的是**连接池**——池归循环，
    会话归任务。这个区别是 ADR-0023 决策三的另一半：引擎是进程级的，绑循环的是连接。
    """

    db = async_session_factory()()
    try:
        try:
            await IndexingService().index_existing_document(db, document_id, reparse=reparse)
        except Exception as exc:  # noqa: BLE001 - 分类处置见 _settle_failure
            await _settle_failure(
                db,
                document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                exc=exc,
                retries=retries,
                max_retries=max_retries,
            )
    finally:
        # 不能省：被放弃的 checked-out 连接即使 dispose 了也会停在 idle in transaction。
        await db.close()


def _run_indexing(
    self: Any,
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    request_id: str | None = None,
    reparse: bool = False,
) -> None:
    """任务体：与 Celery 装饰器分离，便于用轻量 ``self`` 替身离线测试。

    ``self`` 只需要 ``request.retries``、``max_retries`` 与 ``retry()``——正是任务
    体用到的全部接口。这三样**只在任务线程里读 / 调**（见 :class:`_RetryRequested`）。
    """

    if request_id:
        bind_contextvars(request_id=request_id)
    try:
        # Worker 进程也要跟上配置：与 app 侧中间件调同一个入口（#93）。放在最前面是因为
        # 索引链路读的每一个配置项（切块窗口、嵌入批次、节说明开关）都可能在上一分钟被
        # 管理员改过；刷新失败不拦任务（ensure 内部保留上一份好值并告警）。
        #
        # 不传会话：配置覆盖层是**同步**的控制面读（走 app/db/session_factory 的同步工厂），
        # 随票 #129 / #130 一起换栈。这里若把异步会话塞进去，它会退化成「异步函数里的
        # 同步查询」，把刚换掉的阻塞又搬回来。
        runtime_settings.ensure_runtime_overrides()
        # 模型角色解析器也要在这个进程里接上（#82）：Worker 不走 ``app/main.py`` 的启动路径，
        # 少了这一行，Worker 里的改写 / 重排会永远只看到 ``.env``，而面板上改的指向看起来
        # 「保存成功、不生效」。注入是幂等的，每个任务调一次即可（解析器本身每次现读库）。
        set_role_resolver(model_config_service.role_endpoint)
        # 重试次数在**任务线程**读好再传进去：Celery 的 request 是线程局部的。
        retries = self.request.retries
        max_retries = self.max_retries
        # 投**工厂**而不是协程对象：协程在常驻循环里才被创建，从那一刻起属于那个循环。
        get_worker_loop().run(
            lambda: _index_in_the_worker_loop(
                document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                reparse=reparse,
                retries=retries,
                max_retries=max_retries,
            )
        )
    except _RetryRequested as signal:
        # 从这里抛：``retry()`` 本身就是异常（``celery.exceptions.Retry``），不能被吞掉。
        raise self.retry(exc=signal.cause) from signal
    finally:
        if request_id:
            clear_contextvars()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def index_document_task(
    self,
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    request_id: str | None = None,
    reparse: bool = False,
) -> None:
    """投递缝（``app/services/index_dispatch.py``）约定的任务入口。

    去重不靠 ``task_id``：并发去重由 ``try_claim_for_indexing`` 的数据库 CAS 保证，
    ``task_id=f"doc:{document_id}"`` 只是给日志 / 追踪看。
    """

    _run_indexing(
        self,
        document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        request_id=request_id,
        reparse=reparse,
    )
