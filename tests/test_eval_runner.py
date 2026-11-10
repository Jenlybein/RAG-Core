"""统一评测运行器的子命令与归档接缝：临时目录 + 假数据，不连库、不发网络。

运行器的 ``run`` 子命令要真 PostgreSQL + 真 Elasticsearch + 真嵌入，跑一次几分钟，
不适合放进默认套件；能在离线钉住的是**不那么起眼但更容易错**的那部分：

* 四个子命令的退出码与它们说出来的话（校验失败必须是红的，不能是「提示一下」）；
* 归档目录的三份产物（人读 / 机读 / 环境快照）能被下一个人读回来再渲染一次；
* 基线的挑选顺序（随仓库的固定基线优先，其次最近一次**全量**归档——冒烟不能当基线）；
* HTTP 冒烟的字段清单与挑题顺序（它不进闸门，但缺口字段必须当场报出来）。

``scripts/eval_retrieval.py`` 里那些「必须连真库」的部分在 ``run`` 内被调用，
由真跑覆盖；这里不替它编造数字。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import eval_runner
from scripts.eval_gate import QuestionScore, build_results_payload

REPO = Path(__file__).resolve().parents[1]


# ───────────────────────────────────────────────────────────────── 小工具


def _score(
    qid: str, *, type_: str = "colloquial", cp: Any = 1.0, cr: Any = 1.0, reason: str | None = None
) -> QuestionScore:
    return QuestionScore(
        id=qid, type=type_, context_precision=cp, context_recall=cr, unscored_reason=reason
    )


def _payload(
    *,
    run_id: str,
    scores: list[QuestionScore],
    full: bool = True,
    status: str = "ok",
    corpus_version: str = "v1",
) -> dict[str, Any]:
    return build_results_payload(
        run_id=run_id,
        run_dir=f"eval/runs/{run_id}",
        generated_at="2026-11-11T00:00:00+00:00",
        status=status,
        experiment={"name": "t", "mode": "hybrid", "top_k": 5},
        corpus={"version": corpus_version, "documents": 1, "chunks": 2, "questions": len(scores)},
        environment={
            "retrieval": {
                "mode": "hybrid",
                "top_k": 5,
                "rerank_enabled": False,
                "query_rewrite_enabled": False,
                "synonym_expansion_enabled": False,
            },
            "embedding": {"model": "text-embedding-v4", "dimensions": 1024},
            "keyword": {"provider": "elasticsearch", "index": "rag_chunks_eval"},
        },
        scores=scores,
        gate={
            "verdict": "no_baseline",
            "passed": None,
            "reason": None,
            "overall": [],
            "by_type": [],
            "regressions": [],
            "summary": "",
        },
        per_question=[
            {
                "id": score.id,
                "type": score.type,
                "query": f"问句 {score.id}",
                "returned_count": 3,
                "reference_count": 2,
                "context_precision": score.context_precision,
                "context_recall": score.context_recall,
                "unscored_reason": score.unscored_reason,
            }
            for score in scores
        ],
        scope_full=full,
        excluded_types={"absent": 1},
    )


# ───────────────────────────────────────────────────────── 离线可导入（硬约束）


def test_the_runner_imports_without_an_env_file_or_the_app() -> None:
    """「无 .env、无网络」下必须能 import 本模块，否则 pytest 一收集就崩。

    做法是另起一个进程、只看它 import 之后 ``sys.modules`` 里有没有 ``app.*``：
    模块顶层一旦顺手 import 了 ``app.core.config``（那会读环境、定型 settings）
    或 ``scripts.eval_retrieval``（那会在 import 时解析 argv），这条就会红。
    """

    code = (
        "import sys;"
        "import scripts.eval_runner as runner;"
        "print(runner.RUN_SCHEMA);"
        "print(any(name == 'app' or name.startswith('app.') for name in sys.modules));"
        "print('scripts.eval_retrieval' in sys.modules)"
    )
    completed = subprocess.run(  # noqa: S603 - 固定命令，无 shell
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["eval-run/1", "False", "False"]


# ───────────────────────────────────────────────────────── 子命令：validation


def test_validate_dataset_subcommand_passes_on_the_repo_dataset(capsys) -> None:
    code = eval_runner.main(["validate-dataset"])
    output = capsys.readouterr().out

    assert code == 0
    assert "137 道" in output
    assert "进平均 127 道" in output
    assert "absent 6" in output and "false_premise 4" in output


def test_validate_dataset_subcommand_is_red_on_a_broken_label(tmp_path, capsys) -> None:
    """标注写了语料里不存在的节：必须退出码 1，并指出是哪一道题。"""

    broken = {
        "k": 5,
        "questions": [
            {
                "id": "q404",
                "type": "exact_code",
                "query": "不存在的节",
                "relevant": [{"document": "support_errors", "heading_path": "这一节不存在"}],
            }
        ],
    }
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    code = eval_runner.main(["validate-dataset", "--questions", str(questions_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "q404" in captured.out
    assert "不通过" in captured.err


def test_validate_dataset_subcommand_missing_file_is_a_hard_failure(tmp_path, capsys) -> None:
    code = eval_runner.main(["validate-dataset", "--questions", str(tmp_path / "nope.json")])
    assert code == 2
    assert "题库不存在" in capsys.readouterr().err


def test_validate_config_subcommand_accepts_the_repo_config(capsys) -> None:
    code = eval_runner.main(["validate-config"])
    output = capsys.readouterr().out

    assert code == 0
    assert "mode='hybrid'" in output
    assert "整体 ≥ 基线 -1.0pp" in output


def test_validate_config_subcommand_is_red_on_an_unsupported_mode(tmp_path, capsys) -> None:
    config_path = tmp_path / "experiment.toml"
    config_path.write_text(
        "[experiment]\n"
        'name = "t"\n'
        'mode = "fancy"\n'
        "top_k = 5\n"
        'split_strategy = "structure"\n'
        "[gate]\n"
        "overall_pp = 1.0\n"
        "per_type_pp = 3.0\n"
        'baseline = ""\n'
        "[http]\n"
        "samples = 3\n",
        encoding="utf-8",
    )

    code = eval_runner.main(["validate-config", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "mode" in captured.out


def test_validate_config_is_red_when_the_configured_baseline_cannot_be_read(
    tmp_path, capsys
) -> None:
    """配了基线却读不到，会让闸门悄悄退化成「无基线」——必须在配置校验就红。"""

    config_path = tmp_path / "experiment.toml"
    config_path.write_text(
        "[experiment]\n"
        'name = "t"\n'
        'mode = "hybrid"\n'
        "top_k = 5\n"
        'split_strategy = "structure"\n'
        "[gate]\n"
        "overall_pp = 1.0\n"
        "per_type_pp = 3.0\n"
        'baseline = "eval/runs/does-not-exist"\n'
        "[http]\n"
        "samples = 3\n",
        encoding="utf-8",
    )

    code = eval_runner.main(["validate-config", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "基线" in captured.out


# ───────────────────────────────────────────────────── 归档：写 / 读 / 重出报告


def test_write_run_dir_writes_the_three_artifacts_and_reads_back(tmp_path) -> None:
    run_dir = tmp_path / "20261111T000000Z"
    payload = _payload(run_id="20261111T000000Z", scores=[_score("q1")])

    written = eval_runner.write_run_dir(run_dir, payload)

    assert {path.name for path in written} == {"results.json", "environment.json", "report.md"}
    assert (run_dir / "report.md").read_text(encoding="utf-8").startswith("# 评测门禁报告")

    # 归档是自足的：读回来能直接再渲染一次，不需要重跑检索
    reloaded = eval_runner.read_run(run_dir)
    assert reloaded["run_id"] == "20261111T000000Z"
    assert reloaded["metrics"]["overall"]["context_precision"] == pytest.approx(1.0)
    assert reloaded["environment"]["embedding"]["model"] == "text-embedding-v4"
    assert eval_runner.render_report(reloaded).startswith("# 评测门禁报告")


def test_write_run_dir_never_writes_nan(tmp_path) -> None:
    """``NaN`` 不是合法 JSON：写出去之后，下一次「与基线比」会读不回来。"""

    run_dir = tmp_path / "run"
    payload = _payload(run_id="run", scores=[_score("q1", cp=float("nan"), cr=None, reason="x")])

    eval_runner.write_run_dir(run_dir, payload)

    text = (run_dir / "results.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    assert json.loads(text)["metrics"]["overall"]["context_precision"] is None


def test_report_subcommand_rerenders_and_can_print_one_number(tmp_path, capsys) -> None:
    run_dir = tmp_path / "20261111T000000Z"
    payload = _payload(run_id="20261111T000000Z", scores=[_score("q1", cp=0.6123, cr=0.75)])
    eval_runner.write_run_dir(run_dir, payload)

    assert eval_runner.main(["report", "--run", str(run_dir)]) == 0
    output = capsys.readouterr().out
    assert "报告" in output
    assert "复现命令" in (run_dir / "report.md").read_text(encoding="utf-8")

    code = eval_runner.main(
        ["report", "--run", str(run_dir), "--select", "metrics.overall.context_precision"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "0.6123"


def test_report_subcommand_fails_loudly_on_an_unknown_select_key(tmp_path, capsys) -> None:
    run_dir = tmp_path / "run"
    eval_runner.write_run_dir(run_dir, _payload(run_id="run", scores=[_score("q1")]))

    code = eval_runner.main(["report", "--run", str(run_dir), "--select", "metrics.nope"])

    assert code == 2
    assert "取不到" in capsys.readouterr().err


def test_report_subcommand_can_repin_the_baseline_without_rerunning(tmp_path, monkeypatch) -> None:
    """重钉基线的动作不该要求重跑一次检索：归档里已经有全部数字。

    副本里的复现命令必须指向副本自己——否则换一台机器 clone 下来，
    报告里每条 ``--select`` 都指向一个不存在的 ``eval/runs/<id>``。
    """

    baseline_dir = tmp_path / "baseline"
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", baseline_dir)
    run_dir = tmp_path / "20261111T000000Z"
    eval_runner.write_run_dir(run_dir, _payload(run_id="20261111T000000Z", scores=[_score("q1")]))

    code = eval_runner.main(["report", "--run", str(run_dir), "--update-baseline"])

    assert code == 0
    promoted = json.loads((baseline_dir / "results.json").read_text(encoding="utf-8"))
    archived = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert promoted["promoted_from"] == archived["run_dir"]
    assert promoted["run_dir"] == str(baseline_dir)
    report = (baseline_dir / "report.md").read_text(encoding="utf-8")
    assert f"--run {baseline_dir} --select" in report
    assert "来源 run" in report


def test_repinning_a_partial_run_is_refused(tmp_path, monkeypatch) -> None:
    baseline_dir = tmp_path / "baseline"
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", baseline_dir)
    run_dir = tmp_path / "run"
    eval_runner.write_run_dir(
        run_dir, _payload(run_id="run", scores=[_score("q1")], full=False, status="smoke")
    )

    code = eval_runner.main(["report", "--run", str(run_dir), "--update-baseline"])

    assert code == 2
    assert not (baseline_dir / "results.json").exists()


def test_report_subcommand_refuses_a_directory_without_results(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        eval_runner.main(["report", "--run", str(empty)])

    assert "results.json" in str(excinfo.value)


# ───────────────────────────────────────────────────────────── 基线的挑选


def _install_runs(tmp_path, monkeypatch, payloads: list[dict[str, Any]]) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        eval_runner.write_run_dir(runs / payload["run_id"], payload)
    monkeypatch.setattr(eval_runner, "RUNS_DIR", runs)
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", tmp_path / "baseline")
    return runs


def test_the_pinned_baseline_wins_over_the_archives(tmp_path, monkeypatch) -> None:
    _install_runs(
        tmp_path,
        monkeypatch,
        [_payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.5, cr=0.5)])],
    )
    pinned = _payload(run_id="20260202T000000Z", scores=[_score("q1", cp=0.9, cr=0.9)])
    eval_runner.write_run_dir(tmp_path / "baseline", pinned)

    baseline, source = eval_runner.resolve_baseline(reference=None)

    assert baseline is not None
    assert baseline["run_id"] == "20260202T000000Z"
    assert source is not None
    assert Path(source).parts[-2:] == ("baseline", "results.json")


def test_baseline_falls_back_to_the_newest_full_run_and_skips_smoke_runs(
    tmp_path, monkeypatch
) -> None:
    """``--limit`` 跑出来的归档不能当基线：5 道题的数不能代表 127 道题。"""

    _install_runs(
        tmp_path,
        monkeypatch,
        [
            _payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.9, cr=0.9)]),
            _payload(
                run_id="20260103T000000Z",
                scores=[_score("q1", cp=0.1, cr=0.1)],
                full=False,
                status="smoke",
            ),
        ],
    )

    baseline, source = eval_runner.resolve_baseline(reference=None)

    assert baseline is not None
    assert baseline["run_id"] == "20260101T000000Z"
    assert source and "20260101T000000Z" in source


def test_baseline_none_means_no_comparison(tmp_path, monkeypatch) -> None:
    _install_runs(
        tmp_path, monkeypatch, [_payload(run_id="20260101T000000Z", scores=[_score("q1")])]
    )
    assert eval_runner.resolve_baseline(reference="none") == (None, None)


def test_an_explicit_baseline_reference_is_honoured(tmp_path, monkeypatch) -> None:
    runs = _install_runs(
        tmp_path,
        monkeypatch,
        [
            _payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.4, cr=0.4)]),
            _payload(run_id="20260102T000000Z", scores=[_score("q1", cp=0.8, cr=0.8)]),
        ],
    )

    baseline, _source = eval_runner.resolve_baseline(reference="20260101T000000Z")

    assert baseline is not None
    assert baseline["run_id"] == "20260101T000000Z"
    assert (runs / "20260101T000000Z" / "results.json").is_file()


def test_an_unreadable_baseline_reference_is_a_loud_failure(tmp_path, monkeypatch) -> None:
    _install_runs(tmp_path, monkeypatch, [])

    with pytest.raises(SystemExit) as excinfo:
        eval_runner.resolve_baseline(reference="20260101T000000Z")

    assert "找不到 run" in str(excinfo.value)


# ───────────────────────────────────────────────────── HTTP 冒烟（不进闸门）


def test_missing_response_fields_is_empty_for_a_complete_body() -> None:
    body = {
        "code": 0,
        "msg": "success",
        "data": {
            "query": "q",
            "kb_ids": ["kb-eval"],
            "retrieved_chunks": [{"chunk_id": "c1", "kb_id": "kb-eval", "score": 0.9}],
            "metadata": {
                "mode": "hybrid",
                "top_k": 5,
                "vector_store": "pgvector",
                "trace_id": "0" * 32,
                "retrieval": {
                    "degraded": False,
                    "degraded_reason": None,
                    "empty_reason": None,
                    "rerank": None,
                },
            },
        },
    }
    assert eval_runner.missing_response_fields(body) == []


def test_missing_response_fields_names_every_missing_layer() -> None:
    body = {
        "code": 0,
        "data": {
            "query": "q",
            "kb_ids": [],
            "retrieved_chunks": [{"chunk_id": "c1"}],
            "metadata": {
                "mode": "hybrid",
                "top_k": 5,
                "vector_store": "pgvector",
                "retrieval": {"degraded": False},
            },
        },
    }
    missing = eval_runner.missing_response_fields(body)

    assert "metadata.trace_id" in missing
    assert "metadata.retrieval.degraded_reason" in missing
    assert "data.retrieved_chunks[0].kb_id" in missing


def test_missing_response_fields_reports_a_business_failure_and_a_non_json_body() -> None:
    assert any(
        "code=" in item
        for item in eval_runner.missing_response_fields({"code": 20008, "msg": "no", "data": None})
    )
    assert eval_runner.missing_response_fields("not json") == ["响应体不是 JSON 对象"]


def test_smoke_questions_cover_one_per_type_then_fill_in_file_order() -> None:
    questions = [
        SimpleNamespace(id="q1", type="exact_code"),
        SimpleNamespace(id="q2", type="exact_code"),
        SimpleNamespace(id="q3", type="colloquial"),
        SimpleNamespace(id="q4", type="synonym"),
    ]

    assert [q.id for q in eval_runner.select_smoke_questions(questions, 2)] == ["q1", "q3"]
    # 题型不够 3 种时按原顺序补齐，绝不因为「凑不够」少跑一道
    assert [q.id for q in eval_runner.select_smoke_questions(questions[:2], 3)] == ["q1", "q2"]


# ───────────────────────────────────────────────────────────── 纯小工具


def test_utc_stamp_is_sortable_and_always_utc() -> None:
    moment = datetime(2026, 11, 11, 14, 20, 0, tzinfo=UTC)
    assert eval_runner.utc_stamp(moment) == "20261111T142000Z"
    # 带偏移的输入也回到 UTC：报告会被跨时区的人读
    local = datetime(2026, 11, 11, 6, 20, 0, tzinfo=UTC).astimezone(
        datetime.now().astimezone().tzinfo
    )
    assert eval_runner.utc_stamp(local) == "20261111T062000Z"


def test_database_url_is_redacted_before_it_reaches_the_archive() -> None:
    redacted = eval_runner._redact_database_url(
        "postgresql+psycopg://rag:s3cr3t@localhost:5432/rag"
    )
    assert "s3cr3t" not in redacted
    assert redacted.startswith("postgresql+psycopg://rag:***@localhost:5432/rag")
    # 没有口令的连接串原样返回
    assert eval_runner._redact_database_url("sqlite:///./dev.db") == "sqlite:///./dev.db"


def test_json_safe_turns_non_finite_floats_into_none() -> None:
    cleaned = eval_runner.json_safe(
        {"a": float("nan"), "b": float("inf"), "c": [1.0, float("-inf")], "d": "NaN"}
    )
    assert cleaned == {"a": None, "b": None, "c": [1.0, None], "d": "NaN"}
