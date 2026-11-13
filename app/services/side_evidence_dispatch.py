"""旁证跑批的投递缝（#109）：与索引任务同一种形状。

任务模块在函数内**惰性 import**，所以路由层不依赖 Celery 是否就位；离线套件通过
``set_side_evidence_publisher`` 注入记账版发布器，不需要 Broker。

**为什么不与索引任务共用一个槽**：两者是**不同的 Celery task 对象**（一个
``index_document_task``、一个 ``run_side_evidence_task``），共用会让「这次投递到底该
唤醒哪个任务」失去断言力，任务号牌形状也不一样（``doc:`` / ``side-evidence:``）。
注入点留在最底层（``apply_async`` 处），于是 ``kwargs`` 组装与 ``task_id`` 形状仍由
真实代码执行。
"""

from __future__ import annotations

from app.services.index_dispatch import PublisherSlot, TaskPublisher


def _side_evidence_task() -> TaskPublisher:
    from app.tasks.side_evidence import run_side_evidence_task

    return run_side_evidence_task


#: 旁证任务的发布器槽。
_side_evidence_slot = PublisherSlot(_side_evidence_task, name="side-evidence")


def set_side_evidence_publisher(publisher: TaskPublisher | None) -> None:
    """注入旁证任务的发布器（离线套件设为记账版；``None`` 复原生产实现）。"""

    _side_evidence_slot.set(publisher)


def side_evidence_publisher_is_injected() -> bool:
    """旁证投递缝是否处于注入状态（给离线自检用）。"""

    return _side_evidence_slot.is_injected()


def enqueue_side_evidence(
    *, run_id: str, out_dir: str, limit: int | None = None, tenant_id: str | None = None
) -> None:
    """把一次旁证投递给 Worker；``run_id`` 同时是任务号牌，只为难读日志，不去重。

    ``tenant_id`` 随任务带过去，由 Worker 写进 ``status.json``：查状态的那一侧要能证明
    「这次跑批是发起它那个租户的」（见 ``app.api.routes.console_eval``）。

    调用它的路由是同步 ``def``，FastAPI 会把它丢进线程池——**不在事件循环上**，所以这里
    不需要 ``to_thread`` 那一层。
    """

    _side_evidence_slot.current().apply_async(
        kwargs={"run_id": run_id, "out_dir": out_dir, "limit": limit, "tenant_id": tenant_id},
        task_id=f"side-evidence:{run_id}",
    )
