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

from fastapi import APIRouter, Depends, Path as PathParam

from app.api.console_deps import require_console_tenant, tenant_id_of
from app.core.console_auth import ConsoleIdentity
from app.schemas.common import ApiResponse, ok
from app.services.eval_manifest import REPO_ROOT
from app.services.side_evidence_dispatch import enqueue_side_evidence
from app.services.side_evidence_run import new_run_id

router = APIRouter(prefix="/console", tags=["console"])

#: 服务自己生成的 run_id 形状。校验它不是洁癖：它会拼进 ``eval/runs/`` 的路径。
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")

#: 跑批产物的根目录，**锚在仓库根**而不是进程 cwd。
#:
#: 这个路径不是只在本地用：触发端算出 ``out_dir`` 后随任务投递给 Worker，由**另一个进程**
#: 去写 ``status.json``。如果两边都用相对路径，Worker 从别的目录启动时就会写到别处——而
#: 触发端随后去读自己的相对路径，看到的是「任务一直 running」。锚死之后传给 Worker 的
#: 是绝对路径，两边指的是同一个地方。
RUNS_ROOT = REPO_ROOT / "eval" / "runs" / "side-evidence"


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
    enqueue_side_evidence(
        run_id=run_id,
        out_dir=str(out_dir),
        limit=limit,
        tenant_id=tenant_id_of(identity),
    )
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
def _queued(run_id: str) -> ApiResponse[dict]:
    """「还没有结论」的统一答复：形状与有结论时**完全一致**，只是值都是空的。

    三种情况都走这里——``run_id`` 形状不对、目录里还没有 ``status.json``、状态文件属于
    别的租户。它们的对外答复必须一模一样（不区分「不存在」与「不属于你」），所以用同一个
    构造函数而不是三份字面量：三份字面量迟早在字段上漂移，而漂移的表现是「前端某次拿到
    的键少一个」，最难查的那种。
    """

    return ok(
        {
            "run_id": run_id,
            "status": "queued",
            "judge_model": None,
            "error": None,
            "summary": None,
            "report_path": None,
        }
    )


def side_evidence_status(
    run_id: str = PathParam(..., description="触发时返回的 run_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    if not _RUN_ID.match(run_id):
        # 不属于本服务生成的形状：当作不存在，与跨租户同一个答复口径。
        return _queued(run_id)

    run_dir = RUNS_ROOT / run_id
    status_path = run_dir / "status.json"
    if not status_path.is_file():
        return _queued(run_id)

    status = json.loads(status_path.read_text(encoding="utf-8"))
    # 归属校验（本轮 code-review 补的）：``run_id`` 只是一个时间戳形状的字符串，谁都能编一个
    # 去猜别人的目录。状态文件里记着发起它那个租户，对不上就按「不存在」处理——与瀑布、
    # 反查、反馈同一口径：不区分「不存在」与「不属于你」。
    if status.get("tenant_id") not in (None, tenant_id_of(identity)):
        return _queued(run_id)
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
