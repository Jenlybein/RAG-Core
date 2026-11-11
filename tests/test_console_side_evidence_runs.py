"""旁证跑批的「历史列表」与「报告内容」两条新端点的验收。

为什么这两条需要单独钉：

1. **列表必须只列本租户的**。`run_id` 是秒级时间戳形状，谁都能编一个去猜别人的目录；
   列表比点查更容易漏掉归属校验（点查至少会想到「这一行是谁的」）。
2. **报告只能按固定文件名读**。`report.md` 在服务端磁盘上；如果把 `eval/runs/` 当静态
   目录挂出去，等于把「按名字读任意文件」交给调用方。这里钉住「形状不对就什么也不读」。
3. **「没有报告」是 `null`，不是空串**：前端据此说「这一轮没有报告文件」，而不是渲染一个
   空白框。

全部用临时目录，不碰真实 `eval/runs/`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.api.routes import console_eval


def _run(root: Path, run_id: str, *, tenant_id: str | None = "tenant_demo", status: str = "ok",
         summary: dict[str, Any] | None = None, report: str | None = None) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"run_id": run_id, "status": status}
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    (run_dir / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    if summary is not None:
        (run_dir / "results.json").write_text(json.dumps({"summary": summary}), encoding="utf-8")
    if report is not None:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
    return run_dir


# ───────────────────────────────────────── 路由绑定：必须发真请求才测得出来

"""
这一组的由来（真实事故）：`@router.get("/eval/side-evidence/{run_id}")` 这个装饰器
一直被挂在内部助手 `_queued` 头上，真正的 `side_evidence_status` 从来没被路由过。
于是状态端点**恒返回 `queued`**：界面上「正在排队」永不结束、计时器无限增长，而下面
读目录的历史列表如实显示 `failed`——同一份文件、两个答案。

它从 #109 实现起就存在，长期没被发现，因为测试是**直接调函数**的：
`console_eval.side_evidence_status(...)` 一直是绿的，错的是「谁被注册到了这个路径上」。
结论与 `tests/README.md` ② 一字不差：这一层必须发真请求。
"""


def _plan_rows() -> list[Any]:
    from app.models.plan import PLAN_CODES, PLAN_PRESETS, plan_row_of

    rows: list[Any] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _world() -> dict[Any, list[Any]]:
    from app.core import passwords
    from app.models.account import Account
    from app.models.plan import Plan
    from app.models.tenant import Tenant

    tenant = Tenant(id="tenant_demo", name="Demo", plan_id=3, status="active")
    account = Account(
        id="00000000-0000-4000-8000-000000000002",
        tenant_id="tenant_demo",
        username="alice",
        password_hash=passwords.hash_password("user-password"),
        role="user",
        status="active",
    )
    return {Plan: _plan_rows(), Tenant: [tenant], Account: [account]}


@pytest.fixture
def asgi(runs_root: Path):
    """真应用 + 真登录拿到的会话 cookie；数据库换成内存假会话。"""

    import importlib

    from fastapi.testclient import TestClient

    from tests.fake_db import RecordingSession

    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    console_deps = importlib.import_module("app.api.console_deps")

    session = RecordingSession(rows=_world())
    main.app.dependency_overrides[session_module.get_db] = lambda: session
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: None
    try:
        with TestClient(main.app) as client:
            assert (
                client.post(
                    "/console/login", json={"username": "alice", "password": "user-password"}
                ).json()["code"]
                == 0
            )
            yield client
    finally:
        main.app.dependency_overrides.clear()


def test_the_status_route_reports_a_finished_run_not_queued(asgi, runs_root: Path) -> None:
    """回归的那一条：状态文件说 `failed`，端点就必须回 `failed`。

    装饰器挂错函数时，这里拿到的是 `_queued` 的答复（永远是 `queued`）——测试因此会红。
    """

    _run(runs_root, "20260101T000000Z", status="failed", report="# r")

    body = asgi.get("/console/eval/side-evidence/20260101T000000Z").json()

    assert body["code"] == 0
    assert body["data"]["status"] == "failed"


def test_the_status_route_passes_the_judge_model_through(asgi, runs_root: Path) -> None:
    """顺带钉住「回的是真数据」：`judge_model` 只有真读文件才会有。"""

    run_dir = _run(runs_root, "20260102T000000Z", status="ok")
    (run_dir / "status.json").write_text(
        json.dumps(
            {"run_id": "20260102T000000Z", "status": "ok", "judge_model": "fake-judge-v1",
             "tenant_id": "tenant_demo"}
        ),
        encoding="utf-8",
    )

    body = asgi.get("/console/eval/side-evidence/20260102T000000Z").json()

    assert body["data"]["status"] == "ok"
    assert body["data"]["judge_model"] == "fake-judge-v1"


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "side-evidence"
    root.mkdir()
    monkeypatch.setattr(console_eval, "RUNS_ROOT", root)
    return root


class _Identity:
    """端点只读它的 `tenant_id`；这里给出最小形状，不构造真会话。"""

    def __init__(self, tenant_id: str | None) -> None:
        self.tenant_id = tenant_id


def test_list_shows_this_tenants_runs_newest_first(runs_root: Path) -> None:
    _run(runs_root, "20260101T000000Z", summary={"context_precision": 0.5})
    _run(runs_root, "20260102T000000Z", summary={"context_precision": 0.7})
    _run(runs_root, "20260103T000000Z", tenant_id="tenant_other")

    body = console_eval.list_side_evidence_runs(limit=10, identity=_Identity("tenant_demo"))
    items = body.data["items"]

    # 倒序 + 不含别人的
    assert [item["run_id"] for item in items] == ["20260102T000000Z", "20260101T000000Z"]
    assert items[0]["summary"]["context_precision"] == 0.7


def test_list_respects_the_limit(runs_root: Path) -> None:
    for day in range(1, 6):
        _run(runs_root, f"2026010{day}T000000Z")

    body = console_eval.list_side_evidence_runs(limit=2, identity=_Identity("tenant_demo"))

    assert len(body.data["items"]) == 2


def test_list_keeps_legacy_runs_without_a_tenant_field(runs_root: Path) -> None:
    """加 `tenant_id` 之前的老产物按「无主」保留：不列出来反而让人以为跑过的都丢了。"""

    _run(runs_root, "20251231T000000Z", tenant_id=None)

    body = console_eval.list_side_evidence_runs(limit=10, identity=_Identity("tenant_demo"))

    assert [item["run_id"] for item in body.data["items"]] == ["20251231T000000Z"]


def test_list_ignores_directories_that_are_not_run_ids(runs_root: Path) -> None:
    """目录名不是 `run_id` 形状的一律跳过——它们不该被当成一次跑批。"""

    (runs_root / "not-a-run").mkdir()
    _run(runs_root, "20260101T000000Z")

    body = console_eval.list_side_evidence_runs(limit=10, identity=_Identity("tenant_demo"))

    assert [item["run_id"] for item in body.data["items"]] == ["20260101T000000Z"]


def test_report_is_returned_as_markdown(runs_root: Path) -> None:
    _run(runs_root, "20260101T000000Z", report="# 旁证报告\n\nCP 0.8\n")

    body = console_eval.read_side_evidence_report(
        run_id="20260101T000000Z", identity=_Identity("tenant_demo")
    )

    assert body.data["report"] == "# 旁证报告\n\nCP 0.8\n"


def test_report_of_another_tenant_is_null_not_an_error(runs_root: Path) -> None:
    """跨租户与不存在同一个答复：不能区分出「这个 run 在别人那儿存在」。"""

    _run(runs_root, "20260101T000000Z", tenant_id="tenant_other", report="# 别人的报告\n")

    body = console_eval.read_side_evidence_report(
        run_id="20260101T000000Z", identity=_Identity("tenant_demo")
    )

    assert body.data["report"] is None


def test_an_illegal_run_id_shape_reads_nothing(runs_root: Path) -> None:
    """形状不对就不拼路径：`..` 之类的名字不该有机会被拼进来。"""

    body = console_eval.read_side_evidence_report(run_id="../etc", identity=_Identity("tenant_demo"))

    assert body.data["report"] is None


def test_a_missing_report_is_null_not_an_empty_string(runs_root: Path) -> None:
    _run(runs_root, "20260101T000000Z")  # 没有 report.md

    body = console_eval.read_side_evidence_report(
        run_id="20260101T000000Z", identity=_Identity("tenant_demo")
    )

    assert body.data["report"] is None
    assert console_eval.list_side_evidence_runs(
        limit=10, identity=_Identity("tenant_demo")
    ).data["items"][0]["has_report"] is False
