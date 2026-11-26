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
from app.db.worker_loop import run_in_worker_loop
from app.providers.llm import set_role_resolver
from app.repositories import document_repository
from app.services import index_build_service
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


def _failure_message(exc: BaseException | None) -> str:
    """落库的失败文案：默认通用脱敏句，**容量型拒绝**用后端给的中文（带数字）。

    ## 为什么只放一条例外

    其余异常的原文里可能有路径、SQL、内部主机名——那是排障信息，属于日志，不属于用户界面。
    而容量型拒绝的 ``message`` 是 ``static_quota`` 里**我们自己写的**固定中文句
    （「这一篇占 12.0 万 token，库上限 3 万 token（free 档），删掉旧文档或提额」），它天生
    就是给用户看的那句话。

    判据用**错误码**而不是异常类型：`AppError` 是个大筐，将来别的业务码走到这里仍然只落
    通用文案——例外必须窄到只有这一条。
    """

    if isinstance(exc, AppError) and int(exc.code) == int(ErrorCode.KB_TOKEN_QUOTA_EXCEEDED):
        text = str(exc.message or "").strip()
        if text:
            return text
    return INDEXING_FAILED_MESSAGE


async def _mark_failed(
    db: AsyncSession, document_id: str, exc: BaseException | None = None
) -> None:
    """失败留痕：回滚半成品事务，另起一段写 ``status=2`` + 清租约 + 脱敏固定文案。

    **唯一例外是容量型拒绝（20036）**：那时 ``exc.message`` 是**我们自己写的**中文句
    （带「本篇 X / 已用 Y / 上限 Z / 套餐」，见 ``static_quota``），不含异常原文、路径或
    SQL，落库是安全的；而通用英文文案会让用户看到一句与真实原因无关的话。用错误码判断，
    不靠「异常类型像不像」——将来别的 ``AppError`` 走到这里，仍然只落通用文案。

    ``exc`` 只为与调用处对齐，**绝不**写进 ``error_message``：失败原因只在
    ``logger.exception`` 的堆栈里，数据库只存固定文案（不含异常对象、路径或 SQL）。
    清 ``index_claimed_at`` 是为了让失败终态也不留下新鲜租约。

    **这条路径不受 #213 的令牌核对约束**：它是**另一笔事务**、在失败之后重新读行，
    写的是失败终态（不是切片），把它也挡掉只会让卡住的文档永远留在「索引中」。
    清除租约同样走语句（``release_index_claim_async``）而不是给对象赋属性：与状态回写
    （#212 改成 UPDATE 语句）保持同一种写法，免得「改了内存、库里没变」再来一次。
    """

    await async_rollback_now(db)
    doc = await document_repository.get_by_id(db, document_id)
    if doc is None:
        return
    written = await document_repository.update_status(
        db, doc, DocumentStatus.FAILED, _failure_message(exc)
    )
    if written != 1:
        logger.warning(
            "failed status write touched no row",
            document_id=document_id,
            tenant_id=doc.tenant_id,
            written=written,
        )
    # 失败终态同样不保留租约：下一次重建 / 重试不能看到一份「新鲜的失败租约」。
    await document_repository.release_index_claim_async(db, document_id, tenant_id=doc.tenant_id)
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


async def _record_build_outcome(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    build_id: str,
    ok: bool,
    title: str = "",
    reason: str | None = None,
) -> None:
    """把一篇构建子任务的结果记回构建行，并尝试收敛（#215）。

    记完就 ``converge(final=False)``：「谁看见谁收尾」——最后一篇跑完的那一方负责收尾，而
    它并不需要知道「我是不是最后一篇」（判据读真值，不读计数器）。``final=False`` 是因为
    按篇投递时**任何一方都不该断言「不会有人再补」**：清扫器还可能重投，所以只有期限到了
    或清扫器判定没人再补时，才允许「不够 → failed」。
    """

    # ``record_outcome`` 是**构建服务的方法**（它要读构建行、推进进度、失败写明细、再判收敛），
    # 不是模块级函数。以前这里写成 ``index_build_service.record_outcome``（模块级），
    # 而这条路径从来没有被任何用例走到——所以直到补上「任务体」用例才炸出来。
    written = await index_build_service.IndexBuildService().record_outcome(
        db,
        build_id,
        tenant_id=tenant_id,
        document_id=document_id,
        title=title,
        ok=ok,
        reason=reason,
    )
    logger.info(
        "index build document settled",
        build_id=build_id,
        document_id=document_id,
        tenant_id=tenant_id,
        ok=ok,
        build_status=None if written is None else written.status,
    )


async def _settle_failure(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    kb_id: str,
    exc: BaseException,
    retries: int,
    max_retries: int,
    build_id: str | None = None,
    title: str = "",
) -> None:
    """任务失败后的两选一：可重试就释放租约并抛 :class:`_RetryRequested`，否则写失败终态。

    两条分支都跑在**同一个任务会话**上（就是流水线刚用完的那个），所以「回滚半成品 →
    释放租约 → 提交」是一笔连续的收尾，而不是另开一段。

    ## 构建子任务失败：**不改文档状态**（#215）

    那篇文档本身没坏：线上旧版本照常可查、检索照常命中。把它标成 ``failed`` 会让用户看到
    失败却发现内容好好的。所以构建形状下：释放租约（让清扫器之后还能再抢这一篇）、把
    「文档 + 原因」记进构建行的明细，然后收敛。文档状态原样留在成功。
    """

    if build_id is not None:
        await async_rollback_now(db)
        await document_repository.release_index_claim_async(db, document_id, tenant_id=tenant_id)
        await async_commit_now(db)
        await _record_build_outcome(
            db,
            document_id,
            tenant_id=tenant_id,
            build_id=build_id,
            ok=False,
            title=title,
            # 常量在**构建服务**里（它是「未入新版本」明细的文案），不在单篇索引服务里。
            # 这条分支以前从未被任何用例走到，所以这个错前缀一直没被发现。
            reason=index_build_service.BUILD_DOCUMENT_FAILED_REASON,
        )
        logger.exception(
            "index build document failed",
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            build_id=build_id,
        )
        return

    if _is_retryable(exc) and retries < max_retries:
        # 先释放租约再重试：否则下一次尝试的 CAS 会看到仍然新鲜的租约而
        # 抢不到，重试变成静默 no-op（status 也不会变成 2）。
        await async_rollback_now(db)
        await document_repository.release_index_claim_async(db, document_id, tenant_id=tenant_id)
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
    doc_version: int | None,
    retries: int,
    max_retries: int,
    build_id: str | None = None,
    index_version: str | None = None,
    allow_success: bool = False,
) -> None:
    """常驻循环里的一次索引：**开一个任务会话 → 跑流水线 → 无论如何关掉它**。

    会话不跨任务复用（一个任务一笔会话、一笔事务边界），复用的是**连接池**——池归循环，
    会话归任务。这个区别是 ADR-0023 决策三的另一半：引擎是进程级的，绑循环的是连接。

    ``doc_version`` 是载荷带来的「这次算哪一稿」（#173），原样交给流水线：Worker 不读
    指针，迟到的任务因此不会把别的稿写进来。
    """

    db = async_session_factory()()
    try:
        try:
            written = await IndexingService().index_existing_document(
                db,
                document_id,
                reparse=reparse,
                doc_version=doc_version,
                index_version=index_version,
                allow_success=allow_success,
            )
            if build_id is not None:
                await _record_build_outcome(
                    db,
                    document_id,
                    tenant_id=tenant_id,
                    build_id=build_id,
                    ok=written > 0,
                )
        except Exception as exc:  # noqa: BLE001 - 分类处置见 _settle_failure
            await _settle_failure(
                db,
                document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                exc=exc,
                retries=retries,
                max_retries=max_retries,
                build_id=build_id,
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
    doc_version: int | None = None,
    build_id: str | None = None,
    index_version: str | None = None,
    allow_success: bool = False,
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
        run_in_worker_loop(
            lambda: _index_in_the_worker_loop(
                document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                reparse=reparse,
                doc_version=doc_version,
                retries=retries,
                max_retries=max_retries,
                build_id=build_id,
                index_version=index_version,
                allow_success=allow_success,
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
    doc_version: int | None = None,
    build_id: str | None = None,
    index_version: str | None = None,
    allow_success: bool = False,
) -> None:
    """投递缝（``app/services/index_dispatch.py``）约定的任务入口。

    去重不靠 ``task_id``：并发去重由 ``try_claim_for_indexing`` 的数据库 CAS 保证，
    ``task_id=f"doc:{document_id}"`` 只是给日志 / 追踪看。

    ``doc_version`` 是投递方在**投递那一刻**定下的稿号（#173）：Worker 不向指针二次
    确认，所以迟到的任务不会把别的稿写进来。``None`` 表示投递方不知道（老调用点），
    此时流水线回落文档行的当前稿指针。
    """

    _run_indexing(
        self,
        document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        request_id=request_id,
        reparse=reparse,
        doc_version=doc_version,
        build_id=build_id,
        index_version=index_version,
        allow_success=allow_success,
    )
