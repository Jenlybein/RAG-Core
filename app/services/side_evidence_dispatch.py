"""旁证跑批的投递缝（#109）：与索引任务同一种形状。

任务模块在函数内**惰性 import**，所以路由层不依赖 Celery 是否就位；测试 monkeypatch
本函数即可，不需要 Broker（与 :mod:`app.services.index_dispatch` 同一套理由）。
"""

from __future__ import annotations


def enqueue_side_evidence(
    *, run_id: str, out_dir: str, limit: int | None = None, tenant_id: str | None = None
) -> None:
    """把一次旁证投递给 Worker；``run_id`` 同时是任务号牌，只为难读日志，不去重。

    ``tenant_id`` 随任务带过去，由 Worker 写进 ``status.json``：查状态的那一侧要能证明
    「这次跑批是发起它那个租户的」（见 ``app.api.routes.console_eval``）。
    """

    from app.tasks.side_evidence import run_side_evidence_task

    run_side_evidence_task.apply_async(
        kwargs={"run_id": run_id, "out_dir": out_dir, "limit": limit, "tenant_id": tenant_id},
        task_id=f"side-evidence:{run_id}",
    )
