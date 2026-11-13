"""旁证跑批的投递缝（#109）：与索引任务同一种形状。

任务模块在函数内**惰性 import**，所以路由层不依赖 Celery 是否就位；离线套件通过
``set_side_evidence_publisher`` 注入记账版发布器，不需要 Broker。

**为什么不与索引任务共用一个 publisher**：两者是**不同的 Celery task 对象**（一个
``index_document_task``、一个 ``run_side_evidence_task``），共用一个记账器会让「这次
投递到底该唤醒哪个任务」失去断言力。注入缝留在最底层（``apply_async`` 处），于是
``kwargs`` 组装与 ``task_id`` 形状仍然由真实代码执行。
"""

from __future__ import annotations


from app.services.index_dispatch import TaskPublisher

_side_evidence_publisher: TaskPublisher | None = None


def set_side_evidence_publisher(publisher: TaskPublisher | None) -> None:
    """注入发布器（离线套件设为记账版；``None`` 复原生产实现）。"""

    global _side_evidence_publisher
    _side_evidence_publisher = publisher


def enqueue_side_evidence(
    *, run_id: str, out_dir: str, limit: int | None = None, tenant_id: str | None = None
) -> None:
    """把一次旁证投递给 Worker；``run_id`` 同时是任务号牌，只为难读日志，不去重。

    ``tenant_id`` 随任务带过去，由 Worker 写进 ``status.json``：查状态的那一侧要能证明
    「这次跑批是发起它那个租户的」（见 ``app.api.routes.console_eval``）。
    """

    if _side_evidence_publisher is None:
        from app.tasks.side_evidence import run_side_evidence_task

        publisher: TaskPublisher = run_side_evidence_task
    else:
        publisher = _side_evidence_publisher

    publisher.apply_async(
        kwargs={"run_id": run_id, "out_dir": out_dir, "limit": limit, "tenant_id": tenant_id},
        task_id=f"side-evidence:{run_id}",
    )
