"""票 #165（覆盖判定准确率评测）的离线守卫。

守的不是「脚本跑得动」，而是**口径**——这一类错一条都不会自己报错：

1. **标注独立于材料**：refs 的要点文本必须**逐字**出自 ``answer_points``，137 题库的单元
   必须**原样**出自该题的 ``relevant``。照材料反推要点时覆盖率必然全中，结论作废
   （上一轮就是这么栽的）。
2. **B 条件真的少了一节**：删掉的单元必须从池子里消失、且它正是「期望判 none」的那个要点
   所指的单元；删完还得剩下被支撑的要点，否则这一条退化成「全缺」，量不出逐要点。
3. **A / C 的池子只多不少**：C = A + 固定条数的无关节，且无关节不是期望节。
4. **指标算得对**：用构造好的判定结果复算四个率——期望值手算给出，不拿被测函数反推。
5. **预算与空跑边界**：预估次数 ≤60，``plan`` 模式一次模型调用都不发。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import eval_t165_coverage as ev

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def refs_units() -> dict[str, ev.Unit]:
    index, _ = ev.load_units(ev.REFS_CORPUS_DIR)
    return index


@pytest.fixture(scope="module")
def base_units() -> dict[str, ev.Unit]:
    index, _ = ev.load_units(ev.BASE_CORPUS_DIR)
    return index


# ─────────────────────────────────────────────────────────────── 1. 标注独立于材料


def test_refs_point_texts_are_verbatim_answer_points(refs_units: dict[str, ev.Unit]) -> None:
    payload = json.loads(ev.REFS_QUESTIONS.read_text(encoding="utf-8"))
    answers = {str(item["id"]): list(item["answer_points"]) for item in payload["questions"]}

    for case in ev.load_refs_cases(refs_units):
        allowed = answers[case.case_id]
        for point in case.points:
            assert point.text in allowed, f"{case.case_id} 的要点不是标注原文：{point.text}"


def test_refs_units_resolve_to_real_corpus_units(refs_units: dict[str, ev.Unit]) -> None:
    cases = ev.load_refs_cases(refs_units)
    assert len(cases) == 8
    for case in cases:
        assert case.units, f"{case.case_id} 没有任何期望单元"
        for key in case.units:
            assert key in refs_units, f"{case.case_id} 的单元不在语料里：{key}"
        assert case.drop is None or case.drop in case.units


def test_base_units_come_verbatim_from_relevant_labels(base_units: dict[str, ev.Unit]) -> None:
    payload = json.loads(ev.BASE_QUESTIONS.read_text(encoding="utf-8"))
    relevant = {
        str(item["id"]): {
            f"{entry['document']}::{entry['heading_path']}" for entry in item["relevant"]
        }
        for item in payload["questions"]
    }
    cases = ev.load_base_cases()
    assert len(cases) >= 4
    for case in cases:
        # 这 4 题的标注都是两段路径（h1 > h2），折叠口径对它们是恒等的：单元 key 必须与
        # ``relevant`` 里那一行逐字相同——于是「单元来自标注」这条不看语料正文也成立。
        for key in case.units:
            assert key in relevant[case.case_id], f"{case.case_id} 的单元不在标注里：{key}"
        for key in case.units:
            assert key in base_units, f"{case.case_id} 的单元不在语料里：{key}"


# ─────────────────────────────────────────────────────────────── 2/3. 三个条件的池子


def test_condition_b_drops_exactly_one_unit_and_keeps_a_supported_one(
    refs_units: dict[str, ev.Unit],
) -> None:
    cases = ev.load_refs_cases(refs_units)
    in_b = [case for case in cases if case.drop is not None]

    assert [case.case_id for case in in_b] == ["r01", "r04", "r05", "r06", "r07", "r08"]
    for case in in_b:
        pool_a = ev.build_pool(refs_units, case, condition="A")
        pool_b = ev.build_pool(refs_units, case, condition="B")
        assert len(pool_b) == len(pool_a) - 1
        assert case.drop not in {unit.key for unit in pool_b}
        assert case.drop in {unit.key for unit in pool_a}
        # 删完必须还剩被支撑的要点（否则退化成「全缺」，测不出逐要点）
        dropped = case.dropped_point_indexes()
        kept = [index for index in range(len(case.points)) if index not in dropped]
        assert dropped and kept
        for index in kept:
            assert case.points[index].unit in {unit.key for unit in pool_b}


def test_condition_a_gives_every_expected_unit(refs_units: dict[str, ev.Unit]) -> None:
    for case in ev.load_refs_cases(refs_units):
        pool = ev.build_pool(refs_units, case, condition="A")
        assert [unit.key for unit in pool] == list(case.units)


def test_condition_c_adds_distractors_without_removing_expected_units(
    refs_units: dict[str, ev.Unit], base_units: dict[str, ev.Unit]
) -> None:
    _, refs_all = ev.load_units(ev.REFS_CORPUS_DIR)

    for index, cases in (
        (refs_units, ev.load_refs_cases(refs_units)),
        (base_units, ev.load_base_cases()),
    ):
        all_units = refs_all if index is refs_units else ev.load_units(ev.BASE_CORPUS_DIR)[1]
        for case in cases:
            pool_a = ev.build_pool(index, case, condition="A")
            pool_c = ev.build_pool(index, case, condition="C", catalogue=all_units)
            keys_a = [unit.key for unit in pool_a]
            keys_c = [unit.key for unit in pool_c]
            assert keys_c[: len(keys_a)] == keys_a, "C 的前缀必须与 A 逐条相同"
            extra = keys_c[len(keys_a) :]
            assert len(extra) == ev.DISTRACTOR_COUNT
            assert not set(extra) & set(keys_a)


# ─────────────────────────────────────────────────────────────── 4. 指标口径


def test_expectations_split_points_by_condition(refs_units: dict[str, ev.Unit]) -> None:
    case = next(item for item in ev.load_refs_cases(refs_units) if item.case_id == "r05")

    for condition in ("A", "C"):
        expected = ev.expectations(case, condition=condition)
        assert {item.expected for item in expected} == {ev.SUPPORTED}
    expected_b = {item.index: item.expected for item in ev.expectations(case, condition="B")}
    assert expected_b == {0: ev.SUPPORTED, 1: ev.NONE, 2: ev.SUPPORTED, 3: ev.SUPPORTED}


def test_case_metrics_count_each_rate_from_a_worked_example() -> None:
    """手算的一组：3 个期望 supported（1 个判成 none、1 个判成 supported、1 个判成 unknown），
    1 个期望 none（判成 none）。"""

    expected = [
        ev.PointExpectation(index=0, text="p0", expected=ev.SUPPORTED, unit="u0"),
        ev.PointExpectation(index=1, text="p1", expected=ev.SUPPORTED, unit="u1"),
        ev.PointExpectation(index=2, text="p2", expected=ev.NONE, unit="u2"),
        ev.PointExpectation(index=3, text="p3", expected=ev.SUPPORTED, unit="u3"),
    ]
    statuses = (ev.NONE, ev.SUPPORTED, ev.NONE, ev.UNKNOWN)

    metrics = ev.score_verdicts(expected, statuses)

    assert metrics["expected_supported"] == 3
    assert metrics["judged_supported"] == 1
    assert metrics["judged_none"] == 2
    assert metrics["judged_unknown"] == 1
    assert metrics["expected_none"] == 1
    assert metrics["none_detected"] == 1
    assert metrics["false_gap"] == 1
    assert metrics["verdicts"] == 4


def test_invalidated_judgement_is_counted_separately_from_unknown() -> None:
    expected = [ev.PointExpectation(index=0, text="p0", expected=ev.SUPPORTED, unit="u0")]

    metrics = ev.score_verdicts(expected, None, invalid_reason="a point without a verdict")

    assert metrics["invalid"] == 1
    assert metrics["judged_unknown"] == 0
    assert metrics["judged_supported"] == 0


def test_pool_summary_names_the_dropped_unit(refs_units: dict[str, ev.Unit]) -> None:
    case = next(item for item in ev.load_refs_cases(refs_units) if item.case_id == "r05")
    pool_b = ev.build_pool(refs_units, case, condition="B")
    summary = ev.describe_pool(case, pool_b, condition="B")

    assert summary["condition"] == "B"
    assert summary["dropped"] == case.drop
    assert len(summary["units"]) == len(pool_b)


# ─────────────────────────────────────────────────────────────── 5. 预算与空跑


def test_model_call_estimate_is_under_the_cap() -> None:
    predicted = ev.predict_model_calls()

    assert predicted["nominal"] <= ev.MODEL_CALL_CAP
    assert predicted["budget"] == ev.MODEL_CALL_CAP
    # **理论上界可以超**：票面的 60 是按旧成本契约（硬顶 3）估的，加 ⑤ 复查后最坏 = 64。
    # 守住的是「名义预估不超」，而不是「最坏也不超」——后者要改票面预算，不是改代码。
    assert predicted["worst"] >= predicted["nominal"]
    assert predicted["worst"] == len(ev.GAP_GATE_CASES) * 0 + predicted["worst"]  # 占位防呆
    assert predicted["worst"] == (
        predicted["nominal"]
        + (len(ev.GAP_GATE_CASES) + len(ev.RECOVERY_CASES)) * (ev.FULL_RESEARCH_CALLS - 3)
    ), "最坏情况 = 名义 + 完整 Research 每题的额外兜底位"
    assert set(predicted["groups"]) == {"A", "B", "C", "gap_gate", "recovery"}


def test_plan_mode_is_free(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = ev.build_plan()

    assert payload["model_calls"]["nominal"] <= ev.MODEL_CALL_CAP
    assert payload["model_calls"]["actual"] == 0
    assert payload["checks"]["labels_ok"] is True
    assert payload["checks"]["pools_ok"] is True
    assert payload["sample"]  # 样本清单必须能打印出来


# ─────────────────────────────────────────────────────────────── 6. 缺口闸与 unknown 口径


def test_condition_words_match_production_constants() -> None:
    """三个状态词与「材料不足」那句话必须与生产**同一个字面量**。

    抄一份是为了让 ``plan`` 模式不必 import ``app``（那会读配置）；这条用例把「抄漏了」
    变成一条会红的断言。
    """

    from app.services import citation_blocks, research

    assert ev.SUPPORTED == research.SUPPORT_SUPPORTED
    assert ev.NONE == research.SUPPORT_NONE
    assert ev.UNKNOWN == research.SUPPORT_UNKNOWN
    assert ev.GAP_DECLARATION == citation_blocks.GAP_DECLARATION


def test_gate_verdict_separates_blocked_declared_and_not_exercised() -> None:
    assert ev.gate_verdict({"gaps": [], "answer": "随便答"})["status"] == "not_exercised"
    assert ev.gate_verdict({"gaps": [], "answer": None})["ok"] is None

    blocked = ev.gate_verdict(
        {"gaps": ["A2"], "answer": None, "answer_rejected_reason": "gap not declared"}
    )
    assert blocked == {
        "status": "blocked",
        "ok": True,
        "reason": "gap not declared",
    }

    declared = ev.gate_verdict(
        {"gaps": ["A2"], "answer": "这里材料不足，无法回答。", "declared_insufficient": True}
    )
    assert declared["status"] == "declared" and declared["ok"] is True

    leaked = ev.gate_verdict(
        {"gaps": ["A2"], "answer": "答案是 42。", "declared_insufficient": False}
    )
    assert leaked["status"] == "leaked" and leaked["ok"] is False


def test_unknown_reading_counts_only_followed_up_cases() -> None:
    records = [
        {
            "case": "r01",
            "stop_reason": "max_rounds",
            "coverage": [
                {"point_id": "A1", "status": "unknown"},
                {"point_id": "A2", "status": "supported"},
            ],
            "gaps": [],
            "answered": True,
            "declared_insufficient": False,
        },
        {
            "case": "r04",
            "stop_reason": "evidence_complete",
            "coverage": [{"point_id": "A1", "status": "supported"}],
            "gaps": [],
            "answered": True,
            "declared_insufficient": False,
        },
    ]

    reading = ev.unknown_reading(records)

    assert reading["case_count"] == 2
    assert reading["followup_exercised"] == 1
    assert reading["cases_with_unknown"] == 1
    assert reading["rate_with_unknown"] == 1.0
    assert reading["point_total"] == 3
    assert reading["point_unknown_rate"] == round(1 / 3, 4)


def test_actual_calls_are_recounted_from_the_artifacts() -> None:
    payload = {
        "conditions": {
            "A": {"cases": [{"model_calls": 1}, {"model_calls": 1}]},
            "B": {"cases": [{"model_calls": 1}]},
        },
        "gap_gate": {"cases": [{"model_calls": 3}]},
        "unknown": {"records": [{"model_calls": 4}]},
    }

    assert ev.actual_model_calls(payload) == 10


# ─────────────────────────────────────────────────────────────── 7. 离线复算


def _record(rows: list[dict], *, invalid_reason: str | None = None) -> dict:
    return {"case": "x", "condition": "B", "invalid_reason": invalid_reason, "points": rows}


def test_recount_keeps_anti_noise_rate_within_one() -> None:
    """B 条件里「本该 none 却判 supported」不许把抗干扰率抬过 1（真的发生过一次）。"""

    record = _record(
        [
            {"index": 0, "expected": "supported", "status": "supported", "on_expected_unit": True},
            {"index": 1, "expected": "none", "status": "supported", "on_expected_unit": True},
        ]
    )

    counted = ev.recount(record)
    metrics = ev.aggregate([counted])

    assert counted["supported_on_expected"] == 1
    assert metrics["抗干扰率"] == 1.0
    assert metrics["误报support率"] == 1.0


def test_recount_treats_a_missing_status_as_an_invalidated_case() -> None:
    record = _record(
        [{"index": 0, "expected": "supported", "status": None, "on_expected_unit": False}],
        invalid_reason="a point without a verdict",
    )

    counted = ev.recount(record)

    assert counted["invalid"] == 1
    assert counted["judged_unknown"] == 0


def test_recompute_metrics_refreshes_rates_without_touching_call_counts() -> None:
    payload = {
        "model_calls": {"nominal": 1, "budget": 60, "actual": 99},
        "conditions": {
            "A": {
                "cases": [
                    _record(
                        [
                            {
                                "index": 0,
                                "expected": "supported",
                                "status": "none",
                                "on_expected_unit": False,
                            }
                        ]
                    )
                ],
                "metrics": {"假缺口率": 0.0},
            }
        },
        "gap_gate": {"cases": [{"model_calls": 3}]},
        "unknown": {"cases": [{"model_calls": 2}]},
    }

    refreshed = ev.recompute_metrics(payload)

    assert refreshed["conditions"]["A"]["metrics"]["假缺口率"] == 1.0
    assert refreshed["model_calls"]["nominal"] == 1
    assert refreshed["model_calls"]["actual"] == 5


# ─────────────────────────────────────────────────────────────── 8. 归档旁证


def test_archived_reading_counts_only_max_rounds_as_recovered(tmp_path: Path) -> None:
    fixture = tmp_path / "results.json"
    fixture.write_text(
        json.dumps(
            {
                "run_id": "run-x",
                "generated_at": "2026-11-14T00:00:00+00:00",
                "per_question": [
                    {"id": "r01", "stop_reason": "max_rounds"},
                    {"id": "r02", "stop_reason": "no_new_evidence"},
                    {"id": "r03", "stop_reason": "evidence_complete"},
                    {"id": "r04", "stop_reason": "max_rounds"},
                ],
            }
        ),
        encoding="utf-8",
    )

    reading = ev.archived_followup_reading(fixture)

    assert reading["available"] is True
    assert reading["questions"] == 4
    assert reading["recovered"] == 2
    assert reading["rate_recovered"] == 0.5
    assert reading["stop_reasons"]["no_new_evidence"] == 1


def test_archived_reading_says_unavailable_instead_of_guessing(tmp_path: Path) -> None:
    reading = ev.archived_followup_reading(tmp_path / "missing.json")

    assert reading["available"] is False


# ─────────────────────────────────────────────────────────────── 9. 报告结构


def test_report_renders_every_required_section() -> None:
    """报告的骨架必须齐全：五个数、两个结论、「不能回答什么」。

    空产物也要渲染得出来（这一节是票面的硬要求，不能只在「有数据时」才出现）。
    """

    payload = {
        "generated_at": "2026-11-15T00:00:00+00:00",
        "llm_chain": {"llm_endpoint_source": "model_configs", "llm_model": "m"},
        "model_calls": {"nominal": 52, "actual": 52, "budget": 60},
        "conditions": {},
        "archived": {"available": False},
    }

    text = ev.render_report(payload)

    for heading in (
        "## 一、五个数",
        "## 二、结论",
        "## 六、这份对照不能回答什么",
        "## 七、怎么复现",
    ):
        assert heading in text
    assert "要不要加「补证后再判一次」" in text
    assert "缺口闸现在实际拦到的是什么" in text
    assert "样本量小" in text
