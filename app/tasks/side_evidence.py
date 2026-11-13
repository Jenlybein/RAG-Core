"""Celery 旁证任务（#109）：Worker 里跑一遍 50 题语义旁证。

为什么放在 Worker 而不是请求线程里：一次 50 题，每题要跑检索 + 两个 ragas 指标，而 ragas
的 ContextPrecision / ContextRecall **不是每题一次调用**——它把每个片段拆成陈述句逐句
判相关性，实测粗算每题 20~30 次请求、单次 1.5 秒。50 题串行就是半小时量级（CPU 全程
不到 1%），所以跑批是异步的并且并发几题（``SIDE_EVIDENCE_CONCURRENCY``）。

任务**先写 ``running`` 状态再开跑**：控制台轮询靠这个文件区分「排队中」与「在跑」，
而不是靠「文件还没出现」去猜。跑的过程中每完成一题就原子更新一次 ``progress``，
界面因此能画进度条；跑完（无论成败）都会再写一次终态，所以状态文件永远是最后落地的
那个事实——请求成功不等于跑批成功。

**事件循环**（第 11 步票 #137 收口）：与索引任务共用 Worker 子进程的**常驻循环**
（ADR-0023）——把工厂函数投进 ``get_worker_loop().run(...)``，协程在循环里才被创建。
旧写法是每任务 ``asyncio.run(_run_in_its_own_loop(...))`` + ``finally`` 里关 HTTP 客户端，
那是「每任务一个循环」时代的形状：循环一关，客户端与连接池跟着失效，下一个任务拿到的是
一个已经死掉的池子；而常驻循环的客户端由 ``worker_process_shutdown`` 统一关，
**任务体绝不能自己关**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from app.celery_app import celery_app
from app.db.worker_loop import run_in_worker_loop
from app.services.eval_manifest import REPO_ROOT

logger = structlog.get_logger(__name__)

#: 旁证题库，**锚在仓库根**：Worker 的 cwd 不保证是仓库根（`celery -A app.celery_app` 从
#: 哪启动都合法），用相对路径会让「题库找不到」变成一台机器一个样。
ANSWERS_PATH = REPO_ROOT / "eval" / "answers_v1.json"


async def _run_side_evidence(
    *,
    cases: list[dict[str, Any]],
    out_dir: Path,
    run_id: str,
    limit: int | None,
    tenant_id: str | None,
) -> dict[str, Any]:
    """在常驻循环里跑一轮旁证。**不关客户端**——它归循环、不归这一次任务。"""

    from app.services.side_evidence_run import run_once

    return await run_once(
        cases=cases, out_dir=out_dir, run_id=run_id, limit=limit, tenant_id=tenant_id
    )


@celery_app.task(bind=True, max_retries=0)
def run_side_evidence_task(
    self,
    *,
    run_id: str,
    out_dir: str,
    limit: int | None = None,
    answers_path: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    from app.services.side_evidence import write_run, write_status

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    write_status(
        out_dir=target,
        run_id=run_id,
        status="running",
        error=None,
        judge_model="unknown",
        tenant_id=tenant_id,
    )

    try:
        payload = json.loads(Path(answers_path or ANSWERS_PATH).read_text(encoding="utf-8"))
        cases = [case for case in payload.get("cases", []) if case.get("status") == "ok"]
        if not cases:
            write_run(
                out_dir=target, run_id=run_id, judge_model="unknown", summary={}, results=[],
                status="failed", error="没有可用的答案集：先跑 scripts/build_answer_set.py",
                tenant_id=tenant_id,
            )
            return {"status": "failed", "run_id": run_id}
        # 投**工厂**而不是协程对象：协程在常驻循环里才被创建，从那一刻起属于那个循环。
        result = run_in_worker_loop(
            lambda: _run_side_evidence(
                cases=cases,
                out_dir=target,
                run_id=run_id,
                limit=limit,
                tenant_id=tenant_id,
            )
        )
        logger.info(
            "side evidence finished",
            run_id=run_id,
            status=result["status"],
            scored=result["summary"].get("scored"),
        )
        return {"status": result["status"], "run_id": run_id}
    except Exception as exc:  # noqa: BLE001 - 任务失败要落成状态，而不是只在 Worker 日志里
        logger.exception("side evidence crashed", run_id=run_id)
        write_run(
            out_dir=target, run_id=run_id, judge_model="unknown", summary={}, results=[],
            status="failed", error=f"{type(exc).__name__}: {exc}", tenant_id=tenant_id,
        )
        return {"status": "failed", "run_id": run_id}
