"""反馈提交：**本地说了算**，观测只是旁路（#105）。

三条边界，改这里之前先看 ADR-0021 决策 3：

1. **判定权与写入权都在本地库**。校验「这条记录存不存在」「是不是当前租户的」「``trace_id``
   对不对得上」全在本地完成；写入 ``retrieval_feedback`` 也是本地一次短事务。不查观测服务，
   也不靠它鉴权——观测组件挂掉不该让用户点不了「没用」。
2. **推送分数是旁路**。本地写完才尽力推（``TRACE_SINK=langfuse`` 时才真的推），推失败只留
   一条 warning（见 ``app.observability.emit_feedback_score``），**绝不影响响应**。
3. **错误码各管一段**：分数越界 ``20022``；记录不存在 / ``trace_id`` 对不上 / 不属于当前
   租户，三者对外**同一个码、同一句话**（``20021``，不泄漏「存在但不属于你」）；本地写库
   失败 ``20020``——它现在只指本地，不再指观测服务不可用。

写入走**独立短事务**（与归属索引同一个理由）：请求会话 ``get_async_db`` 是只读语义、从不
``commit``，在它上面顺手 ``INSERT`` 会被 ``close()`` 丢掉。所以校验读用请求会话，写入自己
开一个会话、自己提交、自己关。

**异步**（第 11 步票 #128）：读写都在 ``AsyncSession`` 上，提交点仍是
``app.db.transaction`` 的异步孪生 ``async_transaction``——这里不出现裸 ``commit``，
结构守卫（``tests/test_transaction.py``）扫的就是这一条。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.session_factory import async_session_factory, close_quietly
from app.db.transaction import async_transaction
from app.observability import emit_feedback_score_off_loop
from app.repositories import retrieval_feedback_repository, retrieval_trace_repository

logger = structlog.get_logger(__name__)

#: 分数的合法区间（闭区间）。契约常量，不做成配置项。
MIN_SCORE = 1
MAX_SCORE = 5

#: 本地写库失败时的固定消息。它现在是**唯一**能导致提交失败的那一类原因
#: （观测服务不可用不再是失败），所以这句话必须说清是哪一段出了问题。
FEEDBACK_WRITE_FAILED_MESSAGE = "feedback not recorded"


async def submit_feedback(
    *,
    db: AsyncSession,
    tenant_id: str,
    log_id: str,
    trace_id: str,
    score: int,
    comment: str | None = None,
) -> dict[str, object]:
    """提交一次打分；返回要回显给调用方的最终结果。"""

    _ensure_score_in_range(score)
    await _ensure_owns_trace(db, tenant_id=tenant_id, log_id=log_id, trace_id=trace_id)
    await _write_locally(log_id=log_id, tenant_id=tenant_id, score=score, comment=comment)
    # 本地已经是权威了，这一步才是旁路（顺序不能反：先推后写，就变成「观测挂了这次反馈
    # 就不算数」）。推失败只留 warning，响应照常返回（#105 决策 2）。
    # 走 `_off_loop` 入口：同步 SDK / JSONL 的写不能占事件循环（票 #137 的评审抓到的）。
    await emit_feedback_score_off_loop(trace_id=trace_id, score=score, comment=comment)
    return {"log_id": log_id, "score": score, "comment": comment}


def _ensure_score_in_range(score: int) -> None:
    """分数只收 1~5，越界回 ``20022``。

    这一条**必须排在所有读库动作之前**：先查库再判断分数，会让一次明显的手误（6 分）
    变成一次数据库往返，而且驳回的时候如果已经写了别的什么，就变成脏数据。
    """

    if not MIN_SCORE <= score <= MAX_SCORE:
        raise AppError(
            ErrorCode.FEEDBACK_INVALID_SCORE,
            f"score must be between {MIN_SCORE} and {MAX_SCORE}",
        )


async def _ensure_owns_trace(
    db: AsyncSession,
    *,
    tenant_id: str,
    log_id: str,
    trace_id: str,
) -> None:
    """这一步提交得成立吗：那一行存在、``trace_id`` 对得上、且是当前租户的。

    **三种失败对外完全一样**（同一个码、同一句话）。这条不是洁癖：分开答复等于提供一个
    探测口——「不存在」与「不属于你」的差别能让别人试出「某个 ``log_id`` 在别家租户那里
    存在」。调用方能做的动作本来就一样（检查自己的 ``log_id`` / ``trace_id``），
    文案分不分都不会让他多知道任何一件该知道的事。

    ``log_id`` 与 ``trace_id`` **不可混用**：``trace_id`` 是由 ``log_id`` 确定性派生的，
    只校验「有个 trace 叫这个名字」而不校验两者的对应关系，等于把两个 id 都降级成
    「随便填一个别人的」——这也是三种失败合并成一个判定的原因（它在本地一次读完）。
    """

    # 租户作为**强制参数**传进去：跨租户在仓储层就已经是 None，这里不再有「查到了但
    # 不是你的」这种中间态可判，也就没有把它答成别的码的机会。
    trace = await retrieval_trace_repository.get_by_id_for_tenant(db, log_id, tenant_id=tenant_id)
    if trace is None or trace.trace_id != trace_id:
        raise AppError(ErrorCode.FEEDBACK_LOG_MISMATCH)


async def _write_locally(
    *,
    log_id: str,
    tenant_id: str,
    score: int,
    comment: str | None,
) -> None:
    """本地写入（同一 ``log_id`` 覆盖）；失败抛 ``20020``。

    这是**业务写入不是旁路**：读的人要能指望「提交成功 = 库里有一行」，所以这里的异常
    不能像观测那样被吞成 warning。日志只放 id 与错误类型：``comment`` 是调用方写的一句话，
    不往日志里搬。
    """

    session: AsyncSession | None = None
    try:
        session = async_session_factory()()
        # 一笔短事务：出块提交、异常回滚。这里不出现 ``commit``，提交点只在
        # ``app.db.transaction``（见模块文档最后一段）。
        async with async_transaction(session):
            await retrieval_feedback_repository.upsert(
                session,
                log_id=log_id,
                tenant_id=tenant_id,
                score=score,
                comment=comment,
            )
    except Exception as exc:  # noqa: BLE001 - 任何写库失败都翻译成同一个业务码
        logger.warning(
            FEEDBACK_WRITE_FAILED_MESSAGE,
            tenant_id=tenant_id,
            log_id=log_id,
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.FEEDBACK_FAILED) from exc
    finally:
        await close_quietly(session, on_failure=_report_close_failure)


def _report_close_failure(error_type: str) -> None:
    """关会话失败也报在这条消息上：它和「写库失败」对读日志的人是同一件事。"""

    logger.warning(FEEDBACK_WRITE_FAILED_MESSAGE, error=error_type)
