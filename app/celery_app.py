"""Celery 应用：Worker 与 Web 进程只通过 Redis 队列与数据库状态字段通信。

``task_acks_late=True`` 的语义是「任务至少执行一次」：Worker 在任务**执行之后**才
确认，所以进程崩溃 / 机器重启时任务会被重新投递。正因如此，索引任务必须自己幂等
——数据库里的 CAS（``document_repository.try_claim_for_indexing``）就是为此存在的：
重复投递里的后来者抢不到索引权，直接跳过。这两者是**一对**，永远不要只删掉其中
一个。

``worker_prefetch_multiplier=1``：长任务每个 Worker 进程最多预取一条，避免一个
进程囤着一队任务、其他进程空转（embedding 是分钟级的长活）。
``task_track_started=True``：状态可观测（STARTED），排障时能区分「排队」与「在跑」。
``broker_connection_retry_on_startup=True``：容器里 Worker 常比 Redis 先起来，
首次连接失败要重试而不是直接退出。
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "rag_center",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)
