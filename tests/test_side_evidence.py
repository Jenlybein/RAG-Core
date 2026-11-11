"""语义旁证（#109）的离线测试：算分口径、报告措辞、失败留痕、控制台两端点。

**只测外部可观察行为**：聚合出来的数字、落盘的三个文件、HTTP 响应体。
裁判与召回都从参数注入，所以这里既不连模型也不连检索——真实裁判由
``scripts/eval_side_evidence.py`` 自己装配（那一层靠一次实跑验证，不靠单测）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

import pytest

from app.services.side_evidence import (
    CaseResult,
    aggregate,
    build_report,
    diagnose_all_unscored,
    run_side_evidence,
)

CASES = [
    {"id": "q1", "type": "colloquial", "question": "退款多久到账", "answer": "7 天"},
    {"id": "q2", "type": "table_lookup", "question": "标准款吞吐多少", "answer": "900 Mbps"},
    {"id": "q3", "type": "absent", "question": "支持刷脸支付吗", "answer": "片段未提及"},
]


class _Judge:
    """可编程的假裁判：按题号决定回什么，``raise_for`` 里的题会抛错。"""

    def __init__(self, *, cp: dict[str, float | None], cr: dict[str, float | None], raise_for: tuple[str, ...] = ()) -> None:
        self._cp = cp
        self._cr = cr
        self._raise_for = raise_for
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "fake-judge-v1"

    def _pick(self, table: dict[str, float | None], question: str, which: str) -> float | None:
        for qid, case in ((case["id"], case) for case in CASES):
            if case["question"] == question:
                if qid in self._raise_for:
                    raise RuntimeError(f"{which} judge unavailable")
                return table.get(qid)
        raise AssertionError(f"未预置的问题：{question}")

    def context_precision(self, *, question: str, contexts: Sequence[str], reference: str) -> float | None:
        return self._pick(self._cp, question, "precision")

    def context_recall(self, *, question: str, contexts: Sequence[str], reference: str) -> float | None:
        return self._pick(self._cr, question, "recall")


def test_unscored_cases_are_excluded_from_the_mean_not_counted_as_zero() -> None:
    """裁判没评上的题**不进分母**——把它当 0 算，一次账单问题会被读成一次检索退化。"""

    results = [
        CaseResult(id="a", type="t", question="q", cp=1.0, cr=1.0),
        CaseResult(id="b", type="t", question="q", cp=None, cr=None, error="timeout"),
    ]

    summary = aggregate(results)

    assert summary["context_precision"] == 1.0  # 不是 0.5
    assert summary["count"] == 2 and summary["scored"] == 1 and summary["unscored"] == 1
    assert summary["errors"] == ["b"]


def test_by_type_is_reported_even_when_one_type_is_empty() -> None:
    results = [
        CaseResult(id="a", type="colloquial", question="q", cp=0.5, cr=0.5),
        CaseResult(id="b", type="table_lookup", question="q", cp=None, cr=None, error="boom"),
    ]

    summary = aggregate(results)

    assert summary["by_type"]["colloquial"]["context_precision"] == 0.5
    assert summary["by_type"]["table_lookup"]["context_precision"] is None
    assert summary["by_type"]["table_lookup"]["unscored"] == 1


def test_report_says_it_is_side_evidence_and_names_the_ruler() -> None:
    """报告第一屏必须能回答：这是旁证（不阻断）、用哪把尺子、换尺子不可比。"""

    text = build_report(
        run_id="20260101T000000Z",
        judge_model="fake-judge-v1",
        summary=aggregate([CaseResult(id="a", type="t", question="q", cp=0.8, cr=0.9)]),
        results=[CaseResult(id="a", type="t", question="q", cp=0.8, cr=0.9)],
    )

    assert "旁证" in text and "不阻断" in text
    assert "fake-judge-v1" in text
    assert "不可直接比较" in text or "不可比" in text
    assert "context_precision" in text and "context_recall" in text


def test_per_case_failure_is_recorded_and_does_not_abort_the_run(tmp_path: Path) -> None:
    judge = _Judge(cp={"q1": 1.0, "q2": 0.0, "q3": 1.0}, cr={"q1": 1.0, "q2": 0.0, "q3": 1.0}, raise_for=("q2",))

    result = run_side_evidence(
        cases=CASES, judge=judge, retrieve=lambda question: ["片段一", "片段二"], out_dir=tmp_path, run_id="r1"
    )

    # 一题失败不该让整轮「全绿」：是 partial，有效分照留、缺的那题单列。
    assert result["status"] == "partial"
    assert result["summary"]["scored"] == 2
    assert result["summary"]["errors"] == ["q2"]
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "已从平均里排除" in report and "q2" in report


def test_a_partially_scored_run_is_neither_ok_nor_failed(tmp_path: Path) -> None:
    """配额打到一半（前几题评上、后面全 429）不能说成「全绿」，也不能抹掉有效分。"""

    judge = _Judge(
        cp={"q1": 1.0, "q2": 1.0, "q3": 1.0},
        cr={"q1": 1.0, "q2": 1.0, "q3": 1.0},
        raise_for=("q2", "q3"),
    )

    result = run_side_evidence(
        cases=CASES, judge=judge, retrieve=lambda question: ["片段"], out_dir=tmp_path, run_id="r5"
    )

    assert result["status"] == "partial"
    assert result["summary"]["scored"] == 1 and result["summary"]["unscored"] == 2
    assert "没评上" in (result["error"] or "")
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["status"] == "partial"


def test_nothing_scored_marks_the_run_failed_with_a_reason(tmp_path: Path) -> None:
    """一道都没评上不是「分数低」，是「裁判这条路没通」。"""

    judge = _Judge(cp={"q1": 1.0, "q2": 1.0, "q3": 1.0}, cr={"q1": 1.0, "q2": 1.0, "q3": 1.0}, raise_for=("q1", "q2", "q3"))

    result = run_side_evidence(
        cases=CASES, judge=judge, retrieve=lambda question: ["片段"], out_dir=tmp_path, run_id="r2"
    )

    assert result["status"] == "failed"
    assert result["error"] and "没评上" in result["error"]
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed" and status["error"]


def test_run_writes_three_files_and_never_stores_source_text_outside_them(tmp_path: Path) -> None:
    judge = _Judge(cp={"q1": 1.0, "q2": 0.5, "q3": 1.0}, cr={"q1": 1.0, "q2": 0.5, "q3": 1.0})

    run_side_evidence(cases=CASES, judge=judge, retrieve=lambda question: ["机密片段"], out_dir=tmp_path, run_id="r3")

    assert {"report.md", "results.json", "status.json"} <= {path.name for path in tmp_path.iterdir()}
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert payload["judge_model"] == "fake-judge-v1"
    assert payload["summary"]["scored"] == 3
    # 产物里只留题面与分数，没有召回片段的正文：正文是运行时的东西，不进档案。
    assert "机密片段" not in (tmp_path / "results.json").read_text(encoding="utf-8")


def test_limit_runs_only_the_first_n_cases(tmp_path: Path) -> None:
    judge = _Judge(cp={case["id"]: 1.0 for case in CASES}, cr={case["id"]: 1.0 for case in CASES})

    result = run_side_evidence(
        cases=CASES, judge=judge, retrieve=lambda question: ["片段"], out_dir=tmp_path, run_id="r4", limit=2
    )

    assert result["summary"]["count"] == 2



# ──────────────────────────────────────────────── 控制台两端点（直接调函数，绕开认证脚手架）

def _identity() -> Any:
    """控制面身份的最小形状：端点只读它的 tenant_id。"""

    from types import SimpleNamespace

    return SimpleNamespace(tenant_id="tenant_demo")


def test_trigger_returns_a_run_id_and_queues_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import console_eval

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(console_eval, "enqueue_side_evidence", lambda **kwargs: calls.append(kwargs))

    body = console_eval.trigger_side_evidence(identity=_identity())

    assert body.data["status"] == "queued"
    assert len(calls) == 1
    assert calls[0]["out_dir"] == str(tmp_path / body.data["run_id"])
    assert re.match(r"^\d{8}T\d{6}Z$", body.data["run_id"])


def test_status_of_unknown_run_is_queued_not_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """任务还在队列里时既不是「没有这次跑」也不是「跑完了」。"""

    from app.api.routes import console_eval

    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)

    body = console_eval.side_evidence_status(run_id="20260101T000000Z", identity=_identity())

    assert body.data["status"] == "queued"
    assert body.data["summary"] is None


def test_status_reads_the_facts_the_worker_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import console_eval

    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "20260101T000000Z"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps({"status": "ok", "error": None, "judge_model": "fake-judge-v1"}), encoding="utf-8"
    )
    (run_dir / "results.json").write_text(
        json.dumps({"summary": {"context_precision": 0.7, "scored": 50}}), encoding="utf-8"
    )
    (run_dir / "report.md").write_text("# 报告", encoding="utf-8")

    body = console_eval.side_evidence_status(run_id="20260101T000000Z", identity=_identity())

    assert body.data["status"] == "ok"
    assert body.data["judge_model"] == "fake-judge-v1"
    assert body.data["summary"]["context_precision"] == 0.7
    assert body.data["report_path"].endswith("report.md")


def test_a_traversal_shaped_run_id_is_treated_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_id 会拼进路径：只放行本服务自己生成的形状。"""

    from app.api.routes import console_eval

    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)

    body = console_eval.side_evidence_status(run_id="../../etc", identity=_identity())

    assert body.data["status"] == "queued"

def test_another_tenants_run_is_not_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_id` 只是时间戳形状，谁都能编一个；归属对不上就当「不存在」。

    这条是本轮 code-review 查出来的：状态查询原先只校验「你是控制面的人」，不校验
    「这次跑批是你的」，于是任一租户凭猜出来的 run_id 就能读到别人的旁证产物——而产物
    里有**题库问句**。答复口径与瀑布 / 反查 / 反馈一致：不区分「不存在」与「不属于你」。
    """

    from app.api.routes import console_eval

    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "20260101T000000Z"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "error": None,
                "judge_model": "fake-judge-v1",
                "tenant_id": "tenant_somebody_else",
            }
        ),
        encoding="utf-8",
    )

    body = console_eval.side_evidence_status(run_id="20260101T000000Z", identity=_identity())

    assert body.data["status"] == "queued"
    assert body.data["summary"] is None


def test_a_run_without_a_tenant_record_is_still_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """早期跑批（或命令行跑的）没有 `tenant_id`：不因此把历史产物判成别人的。"""

    from app.api.routes import console_eval

    monkeypatch.setattr(console_eval, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "20260101T000000Z"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps({"status": "ok", "error": None, "judge_model": "fake-judge-v1"}),
        encoding="utf-8",
    )

    body = console_eval.side_evidence_status(run_id="20260101T000000Z", identity=_identity())

    assert body.data["status"] == "ok"


# ───────────────────────────────────────── 全是「没评上」时，原因要指对地方

_QUOTA_ERROR = (
    "InstructorRetryException: Error code: 429 - {'error': {'code': 'AccountQuotaExceeded', "
    "'message': 'You have exceeded the weekly usage quota. It will reset at "
    "2026-11-16 00:00:00 +0800 CST.'}}"
)


def test_all_unscored_because_of_quota_says_quota_not_credentials() -> None:
    """真实事故：50 题全 429，状态却写「检查裁判凭据 / 模型可用性」。

    人会去翻 Key、翻 base_url、翻模型配置——全都不对，真正要做的是等额度恢复。
    这条断言钉住「原因指向额度」，并且把恢复时间带出来。
    """

    message = diagnose_all_unscored([_QUOTA_ERROR])

    assert "配额" in message
    assert "2026-11-16 00:00:00 +0800" in message
    # 不能再把人送去查凭据。
    assert "凭据" not in message.split("这不是")[0]


def test_all_unscored_because_of_auth_points_at_credentials() -> None:
    message = diagnose_all_unscored(["AuthenticationError: Error code: 401 - invalid api key"])

    assert "鉴权失败" in message
    assert "API Key" in message


def test_quota_wins_when_both_quota_and_auth_appear_in_one_run() -> None:
    """一轮里同时凑齐两件事时先说额度：反过来的顺序会把一个只是没额度的账号拖去翻配置。"""

    message = diagnose_all_unscored([_QUOTA_ERROR, "AuthenticationError: 401 unauthorized"])

    assert "配额" in message


def test_unrecognised_failures_point_at_the_per_case_errors_file() -> None:
    """认不出原因时，至少告诉人去哪儿看逐题原因，而不是编一个。"""

    message = diagnose_all_unscored(["TimeoutError: x"])

    assert "results.json" in message
