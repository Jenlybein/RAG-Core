"""Celery 旁证任务（#109）：Worker 里跑一遍 50 题语义旁证。

为什么放在 Worker 而不是请求线程里：一次 50 题 × 每题两指标，按实测约每题 5~14 秒，
整轮是分钟级。挂在 HTTP 请求上要么超时，要么让浏览器等一个不知道什么时候回来的请求。

任务**先写 ``running`` 状态再开跑**：控制台轮询靠这个文件区分「排队中」与「在跑」，
而不是靠「文件还没出现」去猜。跑完（无论成败）都会再写一次终态，所以状态文件永远是
最后落地的那个事实——请求成功不等于跑批成功。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from app.celery_app import celery_app
from app.services.eval_manifest import REPO_ROOT

logger = structlog.get_logger(__name__)

#: 旁证题库，**锚在仓库根**：Worker 的 cwd 不保证是仓库根（`celery -A app.celery_app` 从
#: 哪启动都合法），用相对路径会让「题库找不到」变成一台机器一个样。
ANSWERS_PATH = REPO_ROOT / "eval" / "answers_v1.json"


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
    from app.services.side_evidence import write_run
    from app.services.side_evidence_run import run_once

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "error": None,
                "judge_model": "unknown",
                "tenant_id": tenant_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
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
        result = run_once(cases=cases, out_dir=target, run_id=run_id, limit=limit, tenant_id=tenant_id)
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
