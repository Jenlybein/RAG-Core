"""归属索引的写入：**独立短事务**，失败只留 warning（#101）。

三条约束都是从红线直接推出来的，改这里之前先看 ADR-0021 决策 2 与 ADR-0010 第五次修订：

1. **不能用请求会话**。``get_db`` 产出的事务语义是只读的，而且从不 ``commit``——在它上面
   顺手 ``INSERT`` 会被 ``db.close()`` 丢掉（看起来「写过了」，其实什么都没留下）。所以这里
   自己开一个会话、自己 ``commit``、自己 ``close``：这是一次**独立短事务**，与请求事务无关。
2. **会话工厂只能有一个入口**。拿不到 ``Depends`` 的路径都走 :func:`app.db.session_factory
   .session_factory`（中间件、Celery、Provider 装配已经在这条路上），测试也就只需要换掉这
   一处，不必让整个离线套件真的去连 PostgreSQL。**不要在业务函数里写 ``if testing:``**：
   那种分支一旦存在，生产路径与测试路径就是两条代码。
3. **这里永远不抛**。写入是旁路：开不出会话、写不进去、``commit`` 失败、连 ``close`` 失败，
   都只记一条 ``warning``。响应里的 ``trace_id`` 在调用本函数**之前**就已经派生好了，所以
   「写库失败」不会让那个字段缺席——反过来「写成功才拿到 id」就等于把契约挂在旁路的健康度上。

日志里只放 id 与错误**类型**：异常文本可能夹带连接串，问句原文更是一个字都不许进日志
（见 ``tests/test_retrieval_log_hygiene.py``）。
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from app.db.session_factory import close_quietly, session_factory
from app.models.retrieval_trace import RetrievalTrace
from app.repositories import retrieval_trace_repository

logger = structlog.get_logger(__name__)

#: 写入失败时的固定消息。测试按它筛记录，运维按它告警——所以它是一句**固定**的话，
#: 不拼异常文本、不随调用点漂移。
TRACE_WRITE_FAILED_MESSAGE = "retrieval trace not recorded"


def record_retrieval_trace(
    *,
    log_id: str,
    trace_id: str,
    tenant_id: str,
    kb_ids: list[str],
    chunk_ids: list[str],
    top_k: int,
    latency_ms: int,
    degraded_reason: str | None,
    empty_reason: str | None,
) -> None:
    """把这一次检索的结构记录写进归属索引；任何失败都降级成一条 warning。

    参数全是**已经算好的事实**（身份、范围、结局），这里不做判定也不改口径：
    ``degraded_reason`` / ``empty_reason`` 直接来自本次响应的 ``metadata.retrieval``，
    于是「库里那一行」与「回给调用方的那个响应」不会各说一套。
    """

    session: Session | None = None
    try:
        session = session_factory()()
        retrieval_trace_repository.create(
            session,
            RetrievalTrace(
                id=log_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                kb_ids=list(kb_ids),
                chunk_ids=list(chunk_ids),
                top_k=top_k,
                latency_ms=latency_ms,
                degraded_reason=degraded_reason,
                empty_reason=empty_reason,
            ),
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 旁路：任何失败都不许冒到检索返回值上
        logger.warning(
            TRACE_WRITE_FAILED_MESSAGE,
            tenant_id=tenant_id,
            trace_id=trace_id,
            error=type(exc).__name__,
        )
    finally:
        close_quietly(session, on_failure=_report_close_failure)


def _report_close_failure(error_type: str) -> None:
    """把「关会话失败」翻译成一条 warning（消息与写库失败共用同一条固定文本）。"""

    logger.warning(TRACE_WRITE_FAILED_MESSAGE, error=error_type)
