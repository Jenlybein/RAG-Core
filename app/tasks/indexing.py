"""Celery 索引任务：Worker 进程里跑完整条索引流水线。

任务自己开独立的 ``SessionLocal()``（FastAPI 的 ``get_db`` 只属于请求线程），
``asyncio.run`` 把异步流水线跑到底，``finally`` 里一定关掉会话、清掉关联标识。

失败分两类：
* **可重试**（``RETRYABLE_ERROR_CODES``：模型超时 / 请求失败 / embedding 失败 /
  系统繁忙）且还有次数 → 先 ``release_index_claim`` 释放租约并提交，再
  ``self.retry(exc=exc)``（Celery 默认 10 秒后再投一次）。不释放的话，下一次尝试
  会看到仍然新鲜的租约而抢不到，重试永远不会真正执行；
* 其余（解析失败、内容为空、文档不存在、向量数量不一致等）或次数用尽 →
  ``_mark_failed`` 回写 ``status=2`` + 清空 ``index_claimed_at`` + 脱敏固定文案。
  异常原文只进 ``logger.exception`` 的堆栈，绝不落库、绝不回给调用方。

``request_id`` 通过 structlog contextvars 带进 Worker：上传请求与后台 Worker 是两个
进程，日志靠同一个号牌才串得起来（``RequestContextMiddleware`` 在请求侧写它）。

两条日志名 ``document indexed`` / ``document indexing failed`` 是既有排障习惯与
失败留痕契约的一部分，改名前先确认没有脚本或文档依赖它们。
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy.orm import Session
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.celery_app import celery_app
from app.core.exceptions import AppError, ErrorCode
from app.db.session import SessionLocal
from app.repositories import document_repository
from app.services.indexing_service import INDEXING_FAILED_MESSAGE, IndexingService
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


def _mark_failed(db: Session, document_id: str, exc: BaseException | None = None) -> None:
    """失败留痕：回滚半成品事务，另起一段写 ``status=2`` + 清租约 + 脱敏固定文案。

    ``exc`` 只为与调用处对齐，**绝不**写进 ``error_message``：失败原因只在
    ``logger.exception`` 的堆栈里，数据库只存固定文案（不含异常对象、路径或 SQL）。
    清 ``index_claimed_at`` 是为了让失败终态也不留下新鲜租约。
    """

    db.rollback()
    doc = document_repository.get_by_id(db, document_id)
    if doc is None:
        return
    document_repository.update_status(db, doc, DocumentStatus.FAILED, INDEXING_FAILED_MESSAGE)
    # 失败终态同样不保留租约：下一次重建 / 重试不能看到一份「新鲜的失败租约」。
    doc.index_claimed_at = None
    db.commit()


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
    体用到的全部接口。
    """

    if request_id:
        bind_contextvars(request_id=request_id)
    db = SessionLocal()
    try:
        asyncio.run(IndexingService().index_existing_document(db, document_id, reparse=reparse))
    except Exception as exc:
        if _is_retryable(exc) and self.request.retries < self.max_retries:
            # 先释放租约再重试：否则下一次尝试的 CAS 会看到仍然新鲜的租约而
            # 抢不到，重试变成静默 no-op（status 也不会变成 2）。
            db.rollback()
            document_repository.release_index_claim(db, document_id, tenant_id=tenant_id)
            db.commit()
            # 必须从 except 里抛出，不能被 finally 吞掉：finally 只负责关会话、
            # 清关联标识，任何 return / 吞异常都会让重试静默失效。
            raise self.retry(exc=exc)
        _mark_failed(db, document_id, exc)
        logger.exception(
            "document indexing failed",
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
        )
    finally:
        if request_id:
            clear_contextvars()
        db.close()


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
