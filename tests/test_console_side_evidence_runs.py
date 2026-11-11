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
