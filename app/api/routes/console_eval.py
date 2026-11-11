"""控制面「旁证跑批」接口（``/console/eval/side-evidence``，#109）。

两条端点，一条启动、一条查状态。分开放是因为**触发成功不等于跑批成功**：一分钟级的
跑批不该挂在 HTTP 请求上，所以这里只投递任务并立刻返回 ``run_id``，真正的结论写在
run 目录的 ``status.json`` 里（由 Worker 落地，见 :mod:`app.tasks.side_evidence`）。

三条边界与瀑布那条同源：

- 走 ``require_console_tenant``：管理员（``tenant_id IS NULL``）被拒——旁证跑的是本租户
  可见的题库与检索，没有租户就没有「本租户」这回事。
- ``run_id`` 只接受本服务生成的形状（``YYYYMMDDTHHMMSSZ``），因为它会拼进文件路径：
  放行任意字符串等于把「读哪个文件」交给调用方。
- 状态文件不存在时回 ``queued`` 而不是 404：任务可能还在队列里，这既不是「没有这次跑
  批」也不是「跑完了」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Path as PathParam

from app.api.console_deps import require_console_tenant
from app.core.console_auth import ConsoleIdentity
from app.schemas.common import ApiResponse, ok
from app.services.side_evidence_dispatch import enqueue_side_evidence
from app.services.side_evidence_run import new_run_id

router = APIRouter(prefix="/console", tags=["console"])

#: 服务自己生成的 run_id 形状。校验它不是洁癖：它会拼进 ``eval/runs/`` 的路径。
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")

RUNS_ROOT = Path("eval/runs/side-evidence")


@router.post(
    "/eval/side-evidence",
    response_model=ApiResponse[dict],
    summary="触发一次语义旁证跑批（不进闸门）",
)
def trigger_side_evidence(
    identity: ConsoleIdentity = Depends(require_console_tenant),
    limit: int | None = None,
) -> ApiResponse[dict]:
    """投递一次跑批并立刻返回 ``run_id``；结果要靠查状态那条端点。"""

    run_id = new_run_id()
    out_dir = RUNS_ROOT / run_id
    enqueue_side_evidence(run_id=run_id, out_dir=str(out_dir), limit=limit)
    return ok(
        {
            "run_id": run_id,
            "status": "queued",
            "note": "旁证只提示、不阻断；结果以 status.json 为准（Worker 落后时这里是 queued）",
        }
    )


@router.get(
    "/eval/side-evidence/{run_id}",
    response_model=ApiResponse[dict],
    summary="读一次旁证跑批的状态与汇总",
)
def side_evidence_status(
    run_id: str = PathParam(..., description="触发时返回的 run_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    if not _RUN_ID.match(run_id):
        # 不属于本服务生成的形状：当作不存在，与跨租户同一个答复口径。
        return ok({"run_id": run_id, "status": "queued", "summary": None, "error": None})

    run_dir = RUNS_ROOT / run_id
    status_path = run_dir / "status.json"
    if not status_path.is_file():
        return ok({"run_id": run_id, "status": "queued", "summary": None, "error": None})

    status = json.loads(status_path.read_text(encoding="utf-8"))
    results_path = run_dir / "results.json"
    summary = None
    if results_path.is_file():
        summary = json.loads(results_path.read_text(encoding="utf-8")).get("summary")
    return ok(
        {
            "run_id": run_id,
            "status": status.get("status", "queued"),
            "judge_model": status.get("judge_model"),
            "error": status.get("error"),
            "summary": summary,
            "report_path": str(run_dir / "report.md") if (run_dir / "report.md").is_file() else None,
        }
    )
