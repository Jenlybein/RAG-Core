"""索引任务投递缝：T3 定形状，T4 提供被投递的任务。

任务模块在函数内**惰性 import**，所以路由层与上传业务在 T3 阶段不依赖 Celery 是否
已经就位；测试一律 monkeypatch 本函数，不需要 Broker。

并发去重**不是**靠 Celery 的 ``task_id``：真正的去重由数据库 CAS
（``document_repository.try_claim_for_indexing``）保证；``task_id`` 只为日志 / 追踪
可读，别让后人误以为 Celery 会按它去重。
"""

from __future__ import annotations


def enqueue_document(
    *,
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    reparse: bool = False,
) -> None:
    """把索引任务投递到 Broker，带上请求关联标识以便 Worker 续用同一个号牌。"""

    from app.tasks.indexing import index_document_task

    index_document_task.apply_async(
        kwargs={
            "document_id": document_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "request_id": request_id,
            "reparse": reparse,
        },
        task_id=f"doc:{document_id}",
    )
