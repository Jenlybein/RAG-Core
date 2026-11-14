"""C6 守分抽样：比较逻辑与「说清楚这是抽样」的用例（#153 §③-4）。

抽样脚本本身要连库（20 次查询嵌入，真钱），所以这里只测**纯函数**那一半：

* 逐题对账的三种形态——不变、变差、变好——判据要分得清；
* 「缺一侧读数」不许当成 0（否则基线缺失会被读成「掉分」）；
* 结论词与「抽样」这句话必须出现在产物与 markdown 里：这份报告最容易被误读成
  「137 题整体不退步」，所以「抽样」二字是被断言的，不是随手写在文档里的。
"""

from __future__ import annotations

import json

from scripts import eval_c6_guard


def _baseline() -> list[dict]:
    return [
        {"id": "q01", "type": "exact_code", "context_precision": 0.2, "context_recall": 1.0},
        {"id": "q02", "type": "exact_code", "context_precision": 0.2, "context_recall": 1.0},
        {"id": "q03", "type": "cross_section", "context_precision": 0.4, "context_recall": 0.5},
    ]


def _row(question_id: str, kind: str, precision: float | None, recall: float | None) -> dict:
    return {
        "id": question_id,
        "type": kind,
        "context_precision": precision,
        "context_recall": recall,
        "returned_count": 5,
        "reference_count": 1,
    }


def test_a_sample_matching_the_baseline_is_no_regression() -> None:
    """逐题相同 → 结论「抽样无退化」，整体 Δ 是 0.00pp（不是「没有数字」）。"""

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline(),
        sample_rows=[
            _row("q01", "exact_code", 0.2, 1.0),
            _row("q02", "exact_code", 0.2, 1.0),
            _row("q03", "cross_section", 0.4, 0.5),
        ],
    )

    payload = verdict.as_dict()
    assert payload["verdict"] == "抽样无退化"
    assert payload["regressions"] == []
    assert payload["overall"]["context_recall"]["delta_pp"] == 0.0
    assert payload["scored_size"] == 3


def test_one_worse_and_one_better_question_are_both_reported() -> None:
    """一题掉到 0、一题变好：退化清单只写掉的那题，但两题的差值都要在表里。"""

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline(),
        sample_rows=[
            _row("q01", "exact_code", 0.0, 0.0),  # 掉光
            _row("q02", "exact_code", 0.2, 1.0),  # 没变
            _row("q03", "cross_section", 0.4, 1.0),  # 变好
        ],
    )

    payload = verdict.as_dict()
    assert payload["verdict"] == "抽样有退化"
    assert payload["regressions"] == ["q01"]
    rows = {row["id"]: row for row in payload["rows"]}
    assert rows["q01"]["delta"] == {"context_precision": -0.2, "context_recall": -1.0}
    assert rows["q03"]["delta"] == {"context_precision": 0.0, "context_recall": 0.5}


def test_a_missing_reading_is_not_a_zero() -> None:
    """检索报错 / 未评分的题：两侧任缺一个就记 ``None``，既不进平均也不判退化。

    缺失当 0 是这类报告最常见的谎：一次超时会被读成「掉分」。
    """

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline(),
        sample_rows=[
            {**_row("q01", "exact_code", None, None), "error": "AppError: boom"},
            _row("q02", "exact_code", 0.2, 1.0),
        ],
    )

    payload = verdict.as_dict()
    assert payload["failures"] == [{"id": "q01", "error": "AppError: boom"}]
    assert payload["verdict"] == "抽样有报错（不是掉分，是没有读数）"
    assert payload["scored_size"] == 1
    assert payload["rows"][0]["delta"] == {
        "context_precision": None,
        "context_recall": None,
    }


def test_a_baseline_missing_that_question_is_not_scored() -> None:
    """基线里没有这题（题集换过版本）：这一题只能记「无基线」，不能算退化。"""

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline()[:1],
        sample_rows=[_row("q99", "rare_token", 0.0, 0.0)],
    )

    payload = verdict.as_dict()
    assert payload["verdict"] == "抽样无退化"
    assert payload["scored_size"] == 0
    assert payload["overall"]["context_precision"]["baseline"] is None


def test_the_rendered_report_says_it_is_a_sample() -> None:
    """产物里必须写明「抽样 / n=20 / 不外推」——这份报告最容易被当成整体门禁读。"""

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline(),
        sample_rows=[_row("q01", "exact_code", 0.2, 1.0)],
    )
    payload = {
        "run_id": "20260101T000000Z",
        "baseline_run_id": "20261110T224302Z",
        "sample_size": 1,
        **verdict.as_dict(),
        "degraded": [],
    }

    rendered = eval_c6_guard.render_markdown(payload)
    assert "抽样" in rendered
    assert "n=1" in rendered or "**1 题**" in rendered
    assert "不外推" in rendered
    assert "q01" in rendered


def test_outputs_are_written_where_the_acceptance_report_expects(tmp_path) -> None:
    """产物落在 ``eval/reports/c6/c6-guard-<run_id>.{md,json}``：报告首页直接链它。"""

    verdict = eval_c6_guard.compare(
        baseline_questions=_baseline(),
        sample_rows=[_row("q01", "exact_code", 0.2, 1.0)],
    )
    payload = {"run_id": "20260101T000000Z", "sample_size": 1, **verdict.as_dict()}
    stem = eval_c6_guard.write_outputs(payload, out_dir=tmp_path)

    assert stem.name == "c6-guard-20260101T000000Z"
    assert (tmp_path / "c6-guard-20260101T000000Z.md").is_file()
    written = json.loads((tmp_path / "c6-guard-20260101T000000Z.json").read_text("utf-8"))
    assert written["verdict"] == "抽样无退化"
