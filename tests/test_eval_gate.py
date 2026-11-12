"""门禁的纯函数接缝：算分口径、未评分排除、相对判据、报告渲染、数据集与配置校验。

**为什么这些必须是纯函数的离线真值表**：门禁是「阻断发布」的那一侧。它算错一次，要么
把退化放进主干（假绿），要么把没退化的一次拦住（假红）。两种错都不会让检索链路报错，
只会在人读报告时消失。所以这里不连数据库、不连 ES、不发网络：期望值全部由手算得出
（不是把实现再跑一遍），实现改了而语义没变时这些测试不会碎。

接缝（公开入口）：
``mean_scored`` / ``is_scored`` / ``score_run`` / ``compare_to_baseline`` /
``context_scores`` / ``corpus_version`` / ``validate_dataset`` / ``validate_experiment`` /
``render_report`` / ``select_value``。断言只看这些入口的返回值。
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_gate import (
    DEFAULT_OVERALL_PP,
    DEFAULT_PER_TYPE_PP,
    EXCLUDED_QUESTION_TYPES,
    GATE_METRICS,
    GateThresholds,
    QuestionScore,
    compare_to_baseline,
    context_scores,
    corpus_version,
    is_scored,
    latency_stats,
    mean_scored,
    render_report,
    score_run,
    select_value,
    validate_dataset,
    validate_experiment,
)

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / "eval"


# ─────────────────────────────────────────────────────────────── 构造假数据


def _score(
    qid: str,
    *,
    type_: str = "colloquial",
    cp: float | None = 1.0,
    cr: float | None = 1.0,
    reason: str | None = None,
) -> QuestionScore:
    return QuestionScore(
        id=qid,
        type=type_,
        context_precision=cp,
        context_recall=cr,
        unscored_reason=reason,
    )


def _payload(
    scores: list[QuestionScore],
    *,
    run_id: str = "20261111T000000Z",
    corpus: str = "c0ffee00",
    status: str = "ok",
    full: bool = True,
) -> dict[str, Any]:
    """一份最小的 results 载荷：形状与运行器写进 results.json 的一致。"""

    metrics = score_run(scores)
    return {
        "schema": "eval-run/1",
        "run_id": run_id,
        "run_dir": f"eval/runs/{run_id}",
        "generated_at": "2026-11-11T00:00:00+00:00",
        "status": status,
        "failure": None,
        "experiment": {"name": "gate", "mode": "hybrid", "top_k": 5},
        "corpus": {"version": corpus, "documents": 21, "chunks": 469, "questions": 137},
        "scope": {
            "full": full,
            "question_ids": [
                score.id for score in scores if score.type not in EXCLUDED_QUESTION_TYPES
            ],
            "excluded_types": {"absent": 0},
        },
        "metrics": metrics,
        "environment": {
            "retrieval": {"mode": "hybrid", "top_k": 5, "rerank_enabled": False},
            "embedding": {"model": "text-embedding-v4", "dimensions": 1024},
        },
        "per_question": [
            {
                "id": score.id,
                "type": score.type,
                "context_precision": score.context_precision,
                "context_recall": score.context_recall,
                "unscored_reason": score.unscored_reason,
            }
            for score in scores
        ],
        "gate": {
            "verdict": "no_baseline",
            "passed": None,
            "reason": None,
            "overall": [],
            "by_type": [],
            "regressions": [],
            "summary": "",
        },
        "http_smoke": None,
    }


def _compare(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    overall_pp: float = DEFAULT_OVERALL_PP,
    per_type_pp: float = DEFAULT_PER_TYPE_PP,
) -> dict[str, Any]:
    return compare_to_baseline(
        current, baseline, thresholds=GateThresholds(overall_pp, per_type_pp)
    )


# ───────────────────────────────────────────────────── 延迟（逐题耗时 → P50 / P95）


def test_latency_stats_uses_nearest_rank_on_the_sorted_durations() -> None:
    """100 个 10..1000 的耗时：P50 取第 50 个 = 500，P95 取第 95 个 = 950。"""

    durations = [{"duration_ms": value} for value in range(10, 1001, 10)]

    stats = latency_stats(durations)

    assert stats["count"] == 100
    assert stats["mean_ms"] == pytest.approx(505.0)
    assert stats["p50_ms"] == pytest.approx(500.0)
    assert stats["p95_ms"] == pytest.approx(950.0)
    assert stats["max_ms"] == pytest.approx(1000.0)


def test_latency_stats_skips_questions_that_were_never_timed() -> None:
    """没有 ``duration_ms`` 的记录不算「0ms」——它没被计时，不能把中位数拉低。"""

    stats = latency_stats(
        [{"duration_ms": 100.0}, {"id": "q2"}, {"duration_ms": None}, {"duration_ms": 300.0}]
    )

    assert stats["count"] == 2
    assert stats["p50_ms"] == pytest.approx(100.0)  # ceil(0.5 × 2) = 第 1 个
    assert stats["p95_ms"] == pytest.approx(300.0)  # ceil(0.95 × 2) = 第 2 个


def test_latency_stats_without_any_timing_is_none_not_zero() -> None:
    stats = latency_stats([{"id": "q1"}, {"id": "q2"}])

    assert stats == {
        "count": 0,
        "mean_ms": None,
        "p50_ms": None,
        "p95_ms": None,
        "max_ms": None,
    }


def test_report_renders_p50_and_p95_from_the_per_question_durations() -> None:
    """报告的延迟表只从 ``per_question[*].duration_ms`` 来，不另采一次。"""

    payload = _payload([_score("q1", cp=0.9, cr=0.9), _score("q2", cp=0.8, cr=0.8)])
    payload["per_question"][0]["duration_ms"] = 120.0
    payload["per_question"][1]["duration_ms"] = 480.0
    payload["duration_seconds"] = 7.5

    report = render_report(payload)

    assert "## 延迟（逐题耗时，P50 / P95）" in report
    assert "| P50 |" in report and "| P95 |" in report
    # P50 = 第 1 个 = 120，P95 = 第 2 个 = 480；两个都从逐题耗时算出来
    assert "120.0" in report and "480.0" in report
    assert "7.5" in report  # 整轮总时长作为对照，而不是唯一的时间数字


def test_the_reproduction_command_carries_the_rerank_switch_only_when_it_was_on() -> None:
    """重排关的一轮：复现命令与历史一模一样；开的一轮：必须带 ``--rerank``。"""

    payload = _payload([_score("q1")])

    off_report = render_report(payload)
    assert "run --env-file <主 checkout>\\.env\n" in off_report
    assert "--rerank" not in off_report

    payload["environment"]["retrieval"]["rerank_enabled"] = True
    on_report = render_report(payload)
    assert "run --env-file <主 checkout>\\.env --rerank" in on_report


def test_report_says_so_when_no_question_was_timed() -> None:
    payload = _payload([_score("q1")])
    report = render_report(payload)
    assert "## 延迟（逐题耗时，P50 / P95）" in report
    assert "没有留下逐题耗时" in report


# ─────────────────────────────────────────────────── 未评分记录不计入平均


def test_is_scored_rejects_none_and_nan() -> None:
    """NaN 与 None 都是「没评分」，不是 0 分——0 分会把平均拉低，那是另一种谎。"""

    assert is_scored(0.0) is True
    assert is_scored(1.0) is True
    assert is_scored(None) is False
    assert is_scored(float("nan")) is False


def test_mean_scored_skips_unscored_records() -> None:
    # 0.5 与 1.0 两个已评分 → 0.75；None / NaN 不参与分母
    assert mean_scored([0.5, None, float("nan"), 1.0]) == pytest.approx(0.75)
    assert mean_scored([None, float("nan")]) is None
    assert mean_scored([]) is None
    # 全 0 与「没有记录」必须区分开：前者是考了全错，后者是没考
    assert mean_scored([0.0, 0.0]) == 0.0


def test_score_run_excludes_absent_and_false_premise_from_the_main_average() -> None:
    """``absent``（6 道）与 ``false_premise``（4 道）不进主平均。

    absent 测的是误召，false_premise 的正确行为是「识别前提有误」——两者都不是
    CP/CR 能回答的问题，混进分母只会把真实差距压小（现有两个评测脚本同一条规则）。
    """

    metrics = score_run(
        [
            _score("q1", type_="exact_code", cp=1.0, cr=1.0),
            _score("q2", type_="absent", cp=0.0, cr=0.0),
            _score("q3", type_="false_premise", cp=0.0, cr=0.0),
        ]
    )
    assert metrics["overall"]["context_precision"] == pytest.approx(1.0)
    assert metrics["overall"]["context_recall"] == pytest.approx(1.0)
    assert metrics["overall"]["total"] == 1
    assert metrics["excluded"] == {"absent": 1, "false_premise": 1}
    assert set(metrics["by_type"]) == {"exact_code"}


def test_score_run_lists_unscored_records_and_skips_them_per_metric() -> None:
    """一条记录 CP 没评上分时，它仍然可以贡献 CR（返回空 → CR=0 是真的 0）。"""

    metrics = score_run(
        [
            _score("q1", cp=1.0, cr=1.0),
            _score("q2", cp=None, cr=0.0, reason="no_retrieved_context"),
            _score("q3", cp=0.0, cr=0.0),
        ]
    )
    overall = metrics["overall"]
    # CP 只平均已评分的两条：(1.0 + 0.0) / 2
    assert overall["context_precision"] == pytest.approx(0.5)
    assert overall["scored"]["context_precision"] == 2
    # CR 三条都有值：返回空是真的 0 分，不该被当成「没测」
    assert overall["context_recall"] == pytest.approx(1 / 3)
    assert overall["scored"]["context_recall"] == 3
    assert overall["total"] == 3

    assert [item["id"] for item in metrics["unscored"]] == ["q2"]
    assert metrics["unscored"][0]["reason"] == "no_retrieved_context"


def test_score_run_splits_by_type_without_borrowing_counts() -> None:
    metrics = score_run(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("c2", type_="exact_code", cp=0.0, cr=1.0),
            _score("m1", type_="model_number", cp=1.0, cr=0.5),
        ]
    )
    exact = metrics["by_type"]["exact_code"]
    assert exact["context_precision"] == pytest.approx(0.5)
    assert exact["context_recall"] == pytest.approx(1.0)
    assert exact["total"] == 2
    assert metrics["by_type"]["model_number"]["context_precision"] == pytest.approx(1.0)


def test_score_run_with_nothing_scored_is_none_not_zero() -> None:
    metrics = score_run([_score("q1", cp=None, cr=None, reason="retrieval_failed")])
    assert metrics["overall"]["context_precision"] is None
    assert metrics["overall"]["context_recall"] is None
    assert metrics["overall"]["scored"] == {"context_precision": 0, "context_recall": 0}
    assert metrics["unscored"][0]["id"] == "q1"


# ─────────────────────────────────────────────── 相对基线：整体 −1pp / 单题型 3pp


def test_gate_metrics_are_the_two_documented_names() -> None:
    """门禁指标名固定为 context_precision / context_recall——报告、教程、口头沟通同名。"""

    assert GATE_METRICS == ("context_precision", "context_recall")


def test_overall_drop_within_one_point_passes() -> None:
    """δ = −1.00pp 恰好落在判据边界上：判**通过**（不低于 −1pp）。"""

    baseline = _payload([_score("q1", cp=0.8000, cr=0.9000)], run_id="base")
    current = _payload([_score("q1", cp=0.7900, cr=0.9000)], run_id="now")

    gate = _compare(current, baseline)

    assert gate["verdict"] == "pass"
    assert gate["passed"] is True
    row = next(r for r in gate["overall"] if r["metric"] == "context_precision")
    assert row["delta_pp"] == pytest.approx(-1.0, abs=1e-6)
    assert row["status"] == "pass"


def test_overall_drop_over_one_point_is_red() -> None:
    baseline = _payload([_score("q1", cp=0.8000, cr=0.9000)], run_id="base")
    current = _payload([_score("q1", cp=0.7890, cr=0.9000)], run_id="now")

    gate = _compare(current, baseline)

    assert gate["verdict"] == "fail"
    assert gate["passed"] is False
    row = next(r for r in gate["overall"] if r["metric"] == "context_precision")
    assert row["status"] == "fail"
    assert row["delta_pp"] == pytest.approx(-1.1, abs=1e-6)
    assert "context_precision" in gate["summary"]
    assert "-1.10" in gate["summary"] or "-1.1" in gate["summary"]


def test_overall_improvement_passes() -> None:
    baseline = _payload([_score("q1", cp=0.70, cr=0.70)], run_id="base")
    current = _payload([_score("q1", cp=0.75, cr=0.60)], run_id="now")

    gate = _compare(current, baseline)

    # CP +5pp 通过；CR −10pp 不通过 → 整体红。单条通过不等于整体通过。
    assert gate["verdict"] == "fail"
    cr_row = next(r for r in gate["overall"] if r["metric"] == "context_recall")
    assert cr_row["status"] == "fail"
    assert cr_row["delta_pp"] == pytest.approx(-10.0)


def test_single_type_drop_under_three_points_passes() -> None:
    """单类退化 −2.9pp 在 3pp 阈值内；整体被其余题型摊薄，也在 −1pp 内。"""

    baseline = _payload(
        [
            _score("a1", type_="exact_code", cp=0.80, cr=0.80),
            _score("b1", type_="colloquial", cp=0.80, cr=0.80),
            _score("c1", type_="synonym", cp=0.80, cr=0.80),
            _score("d1", type_="table_lookup", cp=0.80, cr=0.80),
        ],
        run_id="base",
    )
    current = _payload(
        [
            _score("a1", type_="exact_code", cp=0.771, cr=0.80),
            _score("b1", type_="colloquial", cp=0.80, cr=0.80),
            _score("c1", type_="synonym", cp=0.80, cr=0.80),
            _score("d1", type_="table_lookup", cp=0.80, cr=0.80),
        ],
        run_id="now",
    )

    gate = _compare(current, baseline)

    assert gate["verdict"] == "pass"
    row = next(
        r
        for r in gate["by_type"]
        if r["type"] == "exact_code" and r["metric"] == "context_precision"
    )
    assert row["delta_pp"] == pytest.approx(-2.9, abs=1e-6)
    assert row["status"] == "pass"


def test_single_type_drop_over_three_points_is_red_and_names_the_type() -> None:
    """整体没退、单类塌了：判红，并说清「哪个题型掉了多少」。"""

    baseline = _payload(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("c2", type_="exact_code", cp=1.0, cr=1.0),
            _score("m1", type_="model_number", cp=0.80, cr=0.80),
            _score("m2", type_="model_number", cp=0.80, cr=0.80),
        ],
        run_id="base",
    )
    current = _payload(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("c2", type_="exact_code", cp=1.0, cr=1.0),
            _score("m1", type_="model_number", cp=0.749, cr=0.80),
            _score("m2", type_="model_number", cp=0.749, cr=0.80),
        ],
        run_id="now",
    )

    gate = _compare(current, baseline)

    # 整体 CP = (1+1+0.749+0.749)/4 = 0.8745 vs 0.90 → −2.55pp 也会红。
    assert gate["verdict"] == "fail"
    assert gate["regressions"], "判红必须给出退化的明细，而不是只说一句「没过」"
    model_row = next(
        r
        for r in gate["by_type"]
        if r["type"] == "model_number" and r["metric"] == "context_precision"
    )
    assert model_row["status"] == "fail"
    assert model_row["delta_pp"] == pytest.approx(-5.1, abs=1e-6)
    assert "model_number" in gate["summary"]
    assert "-5.10" in gate["summary"] or "-5.1" in gate["summary"]


def test_a_type_present_on_only_one_side_is_skipped_not_failed() -> None:
    """防御分支：题型只在一边出现时记「跳过」，绝不判退化成红。

    正常路径下两侧题集相同 → 题型集合也相同（题集不同会在可比性那一步就被拦下）。
    但基线可能来自更早的题库版本：把一个「无法比较」记成「退化」，就是假红。
    """

    def block(cp: float, cr: float, total: int) -> dict[str, Any]:
        return {
            "context_precision": cp,
            "context_recall": cr,
            "scored": {"context_precision": total, "context_recall": total},
            "total": total,
        }

    current = {
        "run_id": "now",
        "scope": {"question_ids": ["c1"], "full": True},
        "corpus": {"version": "v1"},
        "metrics": {
            "overall": block(0.8, 0.8, 1),
            "by_type": {
                "exact_code": block(0.8, 0.8, 1),
                "table_lookup": block(0.1, 0.1, 1),
            },
        },
    }
    baseline = {
        "run_id": "base",
        "scope": {"question_ids": ["c1"], "full": True},
        "corpus": {"version": "v1"},
        "metrics": {"overall": block(0.8, 0.8, 1), "by_type": {"exact_code": block(0.8, 0.8, 1)}},
    }

    gate = _compare(current, baseline)

    new_rows = [r for r in gate["by_type"] if r["type"] == "table_lookup"]
    assert {r["status"] for r in new_rows} == {"skipped"}
    assert all("基线" in r["detail"] for r in new_rows)
    assert gate["verdict"] == "pass"


def test_no_baseline_is_not_red_but_says_so() -> None:
    """首次建立基线时没有可比对象：不判红，但必须写明「这不是通过」。"""

    current = _payload([_score("q1", cp=0.8, cr=0.8)], run_id="now")

    gate = _compare(current, None)

    assert gate["verdict"] == "no_baseline"
    assert gate["passed"] is None
    assert "基线" in gate["summary"]


def test_a_different_question_set_is_incomparable() -> None:
    """两侧题集不同（或一侧被 --limit 裁剪）时，任何差值都不成立。"""

    baseline = _payload([_score("q1", cp=0.9, cr=0.9), _score("q2", cp=0.9, cr=0.9)], run_id="base")
    current = _payload([_score("q1", cp=0.1, cr=0.1)], run_id="now", full=False)

    gate = _compare(current, baseline)

    assert gate["verdict"] == "incomparable"
    assert gate["passed"] is None
    assert "题" in gate["summary"]


def test_a_changed_corpus_version_is_incomparable() -> None:
    """语料换了，基线就不再是同一把尺子——宁可说不可比，也不悄悄给出差值。"""

    baseline = _payload([_score("q1", cp=0.9, cr=0.9)], run_id="base", corpus="aaaa1111")
    current = _payload([_score("q1", cp=0.9, cr=0.9)], run_id="now", corpus="bbbb2222")

    gate = _compare(current, baseline)

    assert gate["verdict"] == "incomparable"
    assert "语料" in gate["summary"]


def test_a_changed_ruler_is_incomparable() -> None:
    """同一份语料、同一批题，换了切块窗口或嵌入模型也是「换了把尺子」。"""

    baseline = _payload([_score("q1", cp=0.9, cr=0.9)], run_id="base")
    baseline["environment"]["chunking"] = {
        "split_strategy": "structure",
        "chunk_size": 512,
        "chunk_overlap": 50,
    }
    baseline["environment"]["embedding"] = {"model": "text-embedding-v4", "dimensions": 1024}
    current = _payload([_score("q1", cp=0.9, cr=0.9)], run_id="now")
    current["environment"]["chunking"] = {
        "split_strategy": "structure",
        "chunk_size": 800,
        "chunk_overlap": 100,
    }
    current["environment"]["embedding"] = {"model": "text-embedding-v4", "dimensions": 1024}

    gate = _compare(current, baseline)

    assert gate["verdict"] == "incomparable"
    assert "切块窗口" in gate["summary"]

    current["environment"]["chunking"] = dict(baseline["environment"]["chunking"])
    current["environment"]["embedding"] = {"model": "another-model", "dimensions": 1024}
    gate = _compare(current, baseline)
    assert gate["verdict"] == "incomparable"
    assert "嵌入模型" in gate["summary"]


def test_a_rerank_switch_change_is_incomparable_and_names_the_axis() -> None:
    """「重排开」与「重排关」不是同一个测量：差值会被读成检索质量变化（#114）。

    尺子原来只有五项（模式 / 条数 / 切块策略 / 切块窗口 / 嵌入模型），重排不在里面，
    于是开着重排的一次跑会和关着重排的冻结基线判成「可比」。
    """

    baseline = _payload([_score("q1", cp=0.9, cr=0.9)], run_id="base")  # 重排关
    current = _payload([_score("q1", cp=0.2, cr=0.2)], run_id="now")
    current["environment"]["retrieval"]["rerank_enabled"] = True

    gate = _compare(current, baseline)

    assert gate["verdict"] == "incomparable"
    assert gate["passed"] is None
    # 不能只说「不可比」，要指出是哪一根刻度不同、两侧各是什么值
    assert "重排开关" in gate["summary"]
    assert "重排开关：本次 开、基线 关" in gate["summary"]


def test_two_runs_that_both_kept_rerank_off_are_still_comparable() -> None:
    """历史两次运行都关重排：补上刻度之后仍然可比，不能把老档案判成不可比。"""

    baseline = _payload([_score("q1", cp=0.80, cr=0.80)], run_id="base")
    current = _payload([_score("q1", cp=0.79, cr=0.80)], run_id="now")

    gate = _compare(current, baseline)

    assert gate["verdict"] == "pass"
    assert gate["summary"].startswith("绿")


def test_the_frozen_baseline_still_reads_the_same_against_a_rerank_off_run() -> None:
    """随仓库的冻结基线（重排关）自己跟自己比：可比、差值为零、读数一字不变。"""

    frozen = json.loads((EVAL_DIR / "baseline" / "results.json").read_text("utf-8"))
    assert frozen["environment"]["retrieval"]["rerank_enabled"] is False

    gate = compare_to_baseline(frozen, frozen)

    assert gate["verdict"] == "pass"
    assert {row["delta_pp"] for row in gate["overall"]} == {0.0}
    # 冻结基线的读数（CP 0.2220 / CR 0.8717，127 题进平均）不许被这一票改动
    assert frozen["metrics"]["overall"]["context_precision"] == pytest.approx(0.2220, abs=1e-4)
    assert frozen["metrics"]["overall"]["context_recall"] == pytest.approx(0.8717, abs=1e-4)
    assert frozen["metrics"]["overall"]["total"] == 127


def test_an_unmeasured_overall_metric_is_red() -> None:
    """一条都没评上分却判绿，就是最坏的假绿：闸门必须红，并说清是「没测到」。"""

    baseline = _payload([_score("q1", cp=0.8, cr=0.8)], run_id="base")
    current = _payload([_score("q1", cp=None, cr=None, reason="retrieval_failed")], run_id="now")

    gate = _compare(current, baseline)

    assert gate["verdict"] == "fail"
    row = next(r for r in gate["overall"] if r["metric"] == "context_precision")
    assert row["status"] == "fail"
    assert "未评分" in row["detail"] or "没评" in row["detail"]


def test_thresholds_come_from_the_experiment_config() -> None:
    baseline = _payload([_score("q1", cp=0.80, cr=0.80)], run_id="base")
    current = _payload([_score("q1", cp=0.76, cr=0.80)], run_id="now")

    assert _compare(current, baseline)["verdict"] == "fail"  # 整体 −4pp > 1pp、单类 −4pp > 3pp
    # 两条判据都放宽到 5pp 才该通过：改阈值就是改实验条件，得写在配置里
    assert _compare(current, baseline, overall_pp=5.0, per_type_pp=5.0)["verdict"] == "pass"


# ─────────────────────────────────────────────────────── ragas ID 版双指标


def test_context_scores_use_only_chunk_ids() -> None:
    """CP = 检索到的 ID 里有多少在标注里；CR = 标注的 ID 有多少被检索到。"""

    cp, cr = asyncio.run(context_scores(["a", "b", "c"], ["b", "c", "d"]))

    assert cp == pytest.approx(2 / 3)
    assert cr == pytest.approx(2 / 3)


def test_context_scores_treat_a_repeated_id_as_one() -> None:
    """同一片被两路召回成两行时，它是同一片——集合口径。"""

    cp, cr = asyncio.run(context_scores(["a", "a"], ["a", "b"]))

    assert cp == pytest.approx(1.0)
    assert cr == pytest.approx(0.5)


def test_context_scores_report_cp_as_unscored_when_nothing_was_retrieved() -> None:
    """空返回时 CP 没有分母（ragas 给 NaN）→ 记「未评分」，而 CR 是真的 0 分。

    这两件事必须分开：把 CR=0 也当成「未评分」会让「检索全空」的一次跑出满绿。
    """

    cp, cr = asyncio.run(context_scores([], ["a"]))

    assert cp is None
    assert cr == 0.0


# ───────────────────────────────────────────────────────────── 语料版本


def test_corpus_version_is_stable_and_content_addressed() -> None:
    files = {"b.md": "二", "a.md": "一"}
    assert corpus_version(files) == corpus_version({"a.md": "一", "b.md": "二"})
    assert corpus_version(files) != corpus_version({"a.md": "一（改）", "b.md": "二"})
    # 文件名进哈希：只换篇名不换内容也算换了语料
    assert corpus_version(files) != corpus_version({"c.md": "一", "b.md": "二"})
    assert len(corpus_version(files)) == 12


# ───────────────────────────────────────────────────── 数据集校验（离线）


@pytest.fixture(scope="module")
def repo_locators() -> dict[tuple[str, str], int]:
    """用真实切块器算出「(文档, heading_path) → 片数」，不连库、不发网络。"""

    from app.utils.markdown_splitter import MarkdownStructuredSplitter

    manifest = json.loads((EVAL_DIR / "corpus" / "manifest.json").read_text("utf-8"))
    splitter = MarkdownStructuredSplitter(chunk_size=512, chunk_overlap=50, table_max_rows=10)
    counts: dict[tuple[str, str], int] = {}
    for kb in manifest["kbs"]:
        for filename in kb["documents"]:
            path = EVAL_DIR / "corpus" / filename
            for piece in splitter.split(path.read_text("utf-8")):
                key = (path.stem, str(piece.metadata["heading_path"]))
                counts[key] = counts.get(key, 0) + 1
    return counts


@pytest.fixture(scope="module")
def repo_questions() -> dict[str, Any]:
    return json.loads((EVAL_DIR / "questions.json").read_text("utf-8"))


def test_validate_dataset_accepts_the_repo_dataset(repo_questions, repo_locators) -> None:
    report = validate_dataset(repo_questions, locators=repo_locators)

    assert report["problems"] == []
    assert report["stats"]["questions"] == 137
    assert report["stats"]["answerable"] == 131
    assert report["stats"]["gate"] == 127
    assert report["stats"]["excluded"] == {"absent": 6, "false_premise": 4}
    # 137 道题全部能定位到真实切片（146 个标注，见 #98 的探针结论）
    assert report["stats"]["labels"] == 146


def test_validate_dataset_flags_an_absent_question_with_labels(repo_locators) -> None:
    payload = {
        "k": 5,
        "questions": [
            {
                "id": "q1",
                "type": "absent",
                "query": "库里没有的东西",
                "relevant": [{"document": "a", "heading_path": "b"}],
            }
        ],
    }
    report = validate_dataset(payload, locators={("a", "b"): 1})
    assert any("absent" in problem for problem in report["problems"])


def test_validate_dataset_flags_a_label_that_does_not_resolve(repo_locators) -> None:
    payload = {
        "k": 5,
        "questions": [
            {
                "id": "q9",
                "type": "exact_code",
                "query": "写了不存在的节",
                "relevant": [{"document": "no_such_doc", "heading_path": "不存在的节"}],
            }
        ],
    }
    report = validate_dataset(payload, locators=repo_locators)
    assert any("q9" in problem and "找不到" in problem for problem in report["problems"])


def test_validate_dataset_flags_a_trap_that_points_at_the_gold_section(repo_locators) -> None:
    locator = {
        "document": "support_errors",
        "heading_path": "售后故障与错误码速查 > 连接与证书类错误（一）",
    }
    payload = {
        "k": 5,
        "questions": [
            {
                "id": "q9",
                "type": "exact_code",
                "query": "x",
                "relevant": [locator],
                "trap": [locator],
            }
        ],
    }
    report = validate_dataset(
        payload, locators={("support_errors", "售后故障与错误码速查 > 连接与证书类错误（一）"): 1}
    )
    assert any("陷阱" in problem for problem in report["problems"])


def test_validate_dataset_warns_about_a_duplicated_question(repo_locators) -> None:
    """同一句问句 + 同一组标注出现两次 = 这道题在平均里被记了两遍。

    这是**警告**不是错误：题库里真有一对（q24 / q66）共享同一句问句与同一节金标，
    它让 rare_token 这一类暗中加权。改它属于题库的票，不在这里拦。
    """

    locator = {"document": "a", "heading_path": "b"}
    payload = {
        "k": 5,
        "questions": [
            {"id": "q1", "type": "rare_token", "query": "同一个问题", "relevant": [locator]},
            {"id": "q2", "type": "rare_token", "query": "同一个问题", "relevant": [locator]},
        ],
    }
    report = validate_dataset(payload, locators={("a", "b"): 1})
    assert report["problems"] == []
    assert any("q1" in warning and "q2" in warning for warning in report["warnings"])


def test_validate_dataset_flags_missing_type_and_bad_k(repo_locators) -> None:
    payload = {
        "k": 0,
        "questions": [{"id": "q1", "query": "x", "type": "", "relevant": []}],
    }
    report = validate_dataset(payload, locators={})
    assert any("k" in problem for problem in report["problems"])
    assert any("type" in problem for problem in report["problems"])


# ─────────────────────────────────────────────────── 实验配置校验


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "experiment": {"name": "gate", "mode": "hybrid", "top_k": 5, "split_strategy": "structure"},
        "gate": {"overall_pp": 1.0, "per_type_pp": 3.0, "baseline": ""},
        "http": {"samples": 3},
    }
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(config.get(section), dict):
            config[section].update(values)
        else:
            config[section] = values
    return config


_ALLOWED = {"modes": ("vector", "bm25", "hybrid"), "strategies": ("structure", "size")}


def test_validate_experiment_accepts_the_repo_config() -> None:
    raw = tomllib.loads((EVAL_DIR / "experiment.toml").read_text("utf-8"))
    report = validate_experiment(raw, **_ALLOWED)
    assert report["problems"] == []


def test_validate_experiment_flags_an_unknown_key() -> None:
    report = validate_experiment(_config(extra={"oops": 1}), **_ALLOWED)
    assert any("extra" in problem for problem in report["problems"])


def test_validate_experiment_flags_an_unsupported_mode_and_top_k() -> None:
    report = validate_experiment(_config(experiment={"mode": "fancy", "top_k": 999}), **_ALLOWED)
    problems = " ".join(report["problems"])
    assert "mode" in problems
    assert "top_k" in problems


def test_validate_experiment_requires_per_type_to_be_looser_than_overall() -> None:
    """单题型阈值必须不小于整体阈值，否则两条判据互相矛盾（同一处差 2pp 既过又不过）。"""

    report = validate_experiment(_config(gate={"overall_pp": 3.0, "per_type_pp": 1.0}), **_ALLOWED)
    assert any("per_type_pp" in problem for problem in report["problems"])


# ─────────────────────────────────────────────────────────── 报告渲染


def _gate_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    """一次判红的 run：整体 CR 塌了，`model_number` 的 CP 也塌了。

    两侧题集必须一致（同一组题才可比），所以基线里也有 x1，只是它有评分、本次没有。
    """

    baseline = _payload(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("m1", type_="model_number", cp=0.80, cr=0.80),
            _score("x1", type_="colloquial", cp=0.50, cr=0.50),
        ],
        run_id="20261110T000000Z",
    )
    current = _payload(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("m1", type_="model_number", cp=0.749, cr=0.80),
            _score("x1", type_="colloquial", cp=None, cr=0.0, reason="no_retrieved_context"),
        ],
        run_id="20261111T000000Z",
    )
    current["gate"] = _compare(current, baseline)
    return current, baseline


def test_report_attaches_a_reproduction_command_to_every_number() -> None:
    """报告里每个数字旁边都要有「怎么把它重新打出来」的命令。

    这不是排版要求：数字没有出处，就等于没有数字（教程里那些传说百分比的教训）。
    """

    payload, _ = _gate_payload()
    report = render_report(payload)

    assert "uv run python scripts/eval_runner.py report --run eval/runs/20261111T000000Z" in report
    # 每个门禁数字都有自己的 select 键（分题型按整块取，一块就是那一行的全部数字）
    for key in (
        "metrics.overall.context_precision",
        "metrics.overall.context_recall",
        "metrics.by_type.model_number",
        "metrics.by_type.exact_code",
        "environment.embedding.model",
        "corpus.version",
        "per_question",
    ):
        assert f"--select {key}" in report, f"{key} 没有附复现命令"


def test_report_always_lists_the_per_type_breakdown_even_without_a_baseline() -> None:
    """分题型拆解必须**常驻**：第一次跑（还没有基线）也要看得到每一类的 CP / CR。

    否则「整体没退但某一类塌了」这件事在建档那一跑里就已经看不见了。
    """

    payload = _payload(
        [
            _score("c1", type_="exact_code", cp=1.0, cr=1.0),
            _score("m1", type_="model_number", cp=0.4, cr=0.8),
        ],
        run_id="now",
    )
    payload["gate"] = _compare(payload, None)
    report = render_report(payload)

    assert "| exact_code | 1 | 1.0000 |" in report
    assert "| model_number | 1 | 0.4000 |" in report
    # 没有基线时数字照列，只是 Δ 与结论为空
    assert "--select metrics.by_type.model_number" in report
    assert "本次还没有可比基线" in report


def test_report_says_which_type_dropped_and_by_how_much() -> None:
    payload, _ = _gate_payload()
    report = render_report(payload)

    assert "红" in report
    assert "model_number" in report
    assert "-5.10" in report or "-5.1" in report
    assert "3.0pp" in report or "3pp" in report


def test_report_states_the_gate_needs_no_llm() -> None:
    payload, _ = _gate_payload()
    report = render_report(payload)
    assert "零 LLM" in report
    assert "ID" in report


def test_report_lists_unscored_records_as_excluded_from_the_average() -> None:
    payload, _ = _gate_payload()
    report = render_report(payload)
    assert "未评分" in report
    assert "x1" in report
    assert "不计入平均" in report


def test_report_marks_a_smoke_run_as_not_eligible_for_the_gate() -> None:
    """``--limit`` 跑出来的数字是真的，但它不能当闸门用——必须写在报告里。"""

    payload = _payload([_score("q1", cp=0.5, cr=0.5)], run_id="now", full=False)
    payload["gate"] = _compare(payload, None)
    report = render_report(payload)
    assert "不参与门禁" in report or "不进闸门" in report


def test_report_for_a_failed_run_holds_no_numbers() -> None:
    """真实跑失败时只写「没跑出来 + 原因」，绝不摆一张看起来像结果的空表。"""

    payload = _payload([], run_id="now", status="failed")
    payload["failure"] = {
        "stage": "preflight",
        "message": "KEYWORD_SEARCH_PROVIDER must be 'elasticsearch'",
    }
    payload["gate"] = {
        "verdict": "incomparable",
        "passed": None,
        "reason": "run_failed",
        "overall": [],
        "by_type": [],
        "regressions": [],
        "summary": "",
    }
    report = render_report(payload)

    assert "未跑出数字" in report
    assert "KEYWORD_SEARCH_PROVIDER" in report
    assert "| context_precision |" not in report


def test_select_value_reads_the_dotted_path_used_in_commands() -> None:
    payload, _ = _gate_payload()
    assert select_value(payload, "metrics.overall.context_precision") == pytest.approx(0.8745)
    assert select_value(payload, "corpus.version") == "c0ffee00"
    assert select_value(payload, "environment.embedding.dimensions") == 1024
    with pytest.raises(KeyError):
        select_value(payload, "metrics.nope.context_precision")


def test_score_run_normalises_nan_to_none() -> None:
    """``NaN`` 进了清单也必须变成 ``None``：``NaN`` 不是合法 JSON，下次对比读不回来。"""

    metrics = score_run([_score("q1", cp=float("nan"), cr=None, reason="x")])
    assert metrics["unscored"][0]["context_precision"] is None
    assert metrics["unscored"][0]["context_recall"] is None
    assert metrics["overall"]["context_precision"] is None
