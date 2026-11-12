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
            # 形状与有结论时**完全一致**，只是值一律为空：多一个字段不会让前端分支变形。
            "progress": None,
        }
    )


def _read_status(run_dir: Path) -> dict | None:
    """读一个 run 目录的 ``status.json``；没有 / 读坏都返回 ``None``（按「还没落地」处理）。"""

    path = run_dir / "status.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _summary_of(run_dir: Path) -> dict | None:
    path = run_dir / "results.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("summary")
    except (ValueError, OSError):
        return None


@router.get(
    "/eval/side-evidence",
    response_model=ApiResponse[dict],
    summary="最近若干次旁证跑批（控制台历史列表）",
)
def list_side_evidence_runs(
    limit: int = 10,
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    """按 run_id 倒序列出最近的跑批，**只列本租户的**。

    归属口径与状态端点一致：状态文件里记着发起它那个租户，对不上就不列出来。老文件可能
    没有 ``tenant_id``（加这个字段之前的产物），这类按「无主」保留——它们是本机历史，
    不列出来反而让人以为「跑过的都丢了」。

    ``run_id`` 是秒级时间戳形状，目录名排序即时间排序，不需要读文件里的时间戳。
    """

    if not RUNS_ROOT.is_dir():
        return ok({"items": [], "limit": limit})

    wanted = tenant_id_of(identity)
    items: list[dict] = []
    for run_dir in sorted(
        (p for p in RUNS_ROOT.iterdir() if p.is_dir() and _RUN_ID.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    ):
        if len(items) >= limit:
            break
        status = _read_status(run_dir)
        if status is not None and status.get("tenant_id") not in (None, wanted):
            continue
        items.append(
            {
                "run_id": run_dir.name,
                "status": (status or {}).get("status", "queued"),
                "judge_model": (status or {}).get("judge_model"),
                "error": (status or {}).get("error"),
                "summary": _summary_of(run_dir),
                "has_report": (run_dir / "report.md").is_file(),
            }
        )
    return ok({"items": items, "limit": limit})


@router.get(
    "/eval/side-evidence/{run_id}/report",
    response_model=ApiResponse[dict],
    summary="读一次旁证跑批的报告原文（Markdown 文本）",
)
def read_side_evidence_report(
    run_id: str = PathParam(..., description="触发时返回的 run_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    """把 ``report.md`` 的正文交给前端，由它在弹窗里渲染。

    **为什么不让前端直接拼文件路径去取**：``report.md`` 在服务端磁盘上，浏览器没有那条
    路径；而把 ``eval/runs/`` 整个目录当静态资源挂出去，等于把「按名字读任意文件」的能力
    交给调用方。这里只认 ``_RUN_ID`` 形状、只读那一个固定文件名、并校验归属。

    报告里**没有原文**（旁证只算分、不落问句正文），所以整段下发是安全的。
    """

    if not _RUN_ID.match(run_id):
        return ok({"run_id": run_id, "report": None})

    run_dir = RUNS_ROOT / run_id
    status = _read_status(run_dir)
    if status is not None and status.get("tenant_id") not in (None, tenant_id_of(identity)):
        return ok({"run_id": run_id, "report": None})

    path = run_dir / "report.md"
    if not path.is_file():
        return ok({"run_id": run_id, "report": None})
    return ok({"run_id": run_id, "report": path.read_text(encoding="utf-8")})


@router.get(
    "/eval/side-evidence/{run_id}",
    response_model=ApiResponse[dict],
    summary="读一次旁证跑批的状态与汇总",
)
def side_evidence_status(
    run_id: str = PathParam(..., description="触发时返回的 run_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    """读一轮跑批的状态、裁判模型与汇总；``queued`` 表示「还没有结论」，不是错误。

    **这个装饰器曾经挂错了函数**（挂在内部助手 ``_queued`` 上），于是这条端点从 #109
    实现那天起就一直回 ``queued``：界面上「正在排队」永远不结束，计时器无限增长，而
    下面那个读目录的历史列表却如实显示 ``failed``——同一份文件、两个答案。
    它之所以长期没被发现，是因为测试直接调 ``side_evidence_status(...)`` 而**没发请求**：
    函数本身是对的，错的是「谁被注册到了这个路径上」。回归用例因此必须是 ASGI 级的
    （见 `tests/test_console_side_evidence_runs.py` 里那条真请求）。
    """

    if not _RUN_ID.match(run_id):
        # 不属于本服务生成的形状：当作不存在，与跨租户同一个答复口径。
        return _queued(run_id)

    run_dir = RUNS_ROOT / run_id
    status = _read_status(run_dir)
    if status is None:
        return _queued(run_id)

    # 归属校验：``run_id`` 只是一个时间戳形状的字符串，谁都能编一个去猜别人的目录。
    # 状态文件里记着发起它那个租户，对不上就按「不存在」处理——与瀑布、反查、反馈同一口径：
    # 不区分「不存在」与「不属于你」。
    if status.get("tenant_id") not in (None, tenant_id_of(identity)):
        return _queued(run_id)
    return ok(
        {
            "run_id": run_id,
            "status": status.get("status", "queued"),
            "judge_model": status.get("judge_model"),
            "error": status.get("error"),
            "summary": _summary_of(run_dir),
            "report_path": str(run_dir / "report.md") if (run_dir / "report.md").is_file() else None,
            # 跑批过程中由跑批一侧每题原子更新一次（`{"done": n, "total": 50}`）：
            # 界面据此画进度，而不是让「running」悬在那里让人猜还要多久。
            "progress": status.get("progress"),
        }
    )
