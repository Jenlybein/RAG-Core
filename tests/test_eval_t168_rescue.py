"""#168 评测脚本的用例：**标准答案机械可查**与**选题条件的机械性**（不连库、不连模型）。

这组用例守的是本票两条最容易被「顺手放宽」的规矩：

1. 「证据章节 ∈ expected」必须是**字符串比对**得出的（节号 / heading 分支），不许人工判断；
2. 「B 组每题在 `top_k=2` 下 expected 缺席」必须有**机械证据**，而不是题面上的一句自述。

所以用例全部打在纯函数上：喂构造的行与标注，断言比对结果与选题结果。真跑（连库 + 真模型）
的部分不在这里测——那部分由 `plan` / `run` 的产物（`results.json`）留痕。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval_t168_rescue import (
    B_TOP_K,
    GROUP_A,
    GROUP_B,
    NOMINAL_CALLS_PER_CASE,
    WORST_CALLS_PER_CASE,
    EvalCase,
    Expected,
    MaterialRef,
    Probe,
    archive_gaps,
    build_cases,
    case_order_key,
    check_archived_premise,
    check_case_set,
    document_stem_of,
    heading_in_branch,
    material_matches,
    material_ref,
    missing_expected,
    normalize_heading,
    predict_model_calls,
    section_of_heading,
    select_b2_cases,
)

# ───────────────────────────────────────────────── 标准答案：节号口径（refs 语料）


def test_section_number_is_read_from_the_last_heading_segment() -> None:
    assert section_of_heading("会员订单退款政策 > 2. 退款时限与审核") == "2"
    assert section_of_heading("积分规则 > 2. 积分抵扣与使用上限 > 2.2 抵扣上限") == "2.2"
    # 没有节号的分支（eval 语料的 heading 就不带号）：空串，不会误匹配任何节
    assert section_of_heading("终端型号与配件指南 > 整机规格（标准款）") == ""


def test_section_annotation_matches_only_the_same_document_and_section() -> None:
    expected = Expected(document="refund_policy", section="2", kind="section")
    assert material_matches(
        expected,
        MaterialRef(
            document="refund_policy",
            section="2",
            heading="任意 > 2. x",
            section_id="s",
            chunk_id="c",
        ),
    )
    # 同一节号、不同文档 → 不算
    assert not material_matches(
        expected,
        MaterialRef(
            document="promo_rules", section="2", heading="任意 > 2. x", section_id="s", chunk_id="c"
        ),
    )
    # 同一文档、别的节 → 不算
    assert not material_matches(
        expected,
        MaterialRef(
            document="refund_policy",
            section="3",
            heading="任意 > 3. x",
            section_id="s",
            chunk_id="c",
        ),
    )


# ─────────────────────────────────────────────── 标准答案：heading 分支口径（eval 语料）


def test_heading_branch_accepts_parent_and_child_but_never_a_sibling() -> None:
    expected = "终端型号与配件指南 > 整机规格（标准款）"
    assert heading_in_branch(expected, expected)  # 同等
    # 材料是它下面的 h3（「一节大于窗口」时切块器会这样交付）
    assert heading_in_branch(expected, expected + " > 电源与散热")
    # 材料是它的父 h2（标准答案标在 h3 上时反过来也会出现）
    assert heading_in_branch(expected + " > 电源与散热", expected)
    # 兄弟节：前缀相同但不是同一条分支 → 不算
    assert not heading_in_branch(expected, "终端型号与配件指南 > 整机规格（增强款）")
    assert not heading_in_branch("终端型号与配件指南", "价格与折扣对照表 > 配件价格")


def test_normalize_heading_只有排版差异时视为同一节() -> None:
    assert normalize_heading("  A  >  B ") == normalize_heading("A > B")
    assert normalize_heading("A > > B") == "A > B"


def test_heading_annotation_requires_the_same_document() -> None:
    expected = Expected(
        document="support_errors", heading="售后 > 配额与限流类错误（一）", kind="heading"
    )
    assert material_matches(
        expected,
        MaterialRef(
            document="support_errors",
            section="",
            heading="售后 > 配额与限流类错误（一）",
            section_id="s",
            chunk_id="c",
        ),
    )
    assert not material_matches(
        expected,
        MaterialRef(
            document="ops_runbook",
            section="",
            heading="售后 > 配额与限流类错误（一）",
            section_id="s",
            chunk_id="c",
        ),
    )


# ─────────────────────────────────────────────── 文档词干（两套语料同一函数）


@pytest.mark.parametrize(
    ("document_id", "source_filename", "stem"),
    [
        ("eval-doc-support_errors", None, "support_errors"),
        ("1f8b8b74-0eac-4220-a5df-5f13a5a39e91", "refund_policy.md", "refund_policy"),
        (
            "1f8b8b74-0eac-4220-a5df-5f13a5a39e91",
            "eval/corpus-refs/points_policy.md",
            "points_policy",
        ),
    ],
)
def test_document_stem_covers_both_corpora(
    document_id: str, source_filename: str | None, stem: str
) -> None:
    assert document_stem_of(document_id, source_filename) == stem


def test_material_ref_reads_the_run_row_shape() -> None:
    row: dict[str, Any] = {
        "document_id": "eval-doc-support_errors",
        "chunk_id": "chunk-1",
        "section_id": "sec-1",
        "heading_path": "售后故障与错误码速查 > 配额与限流类错误（一）",
    }
    ref = material_ref(row, stems={"eval-doc-support_errors": "support_errors"})
    assert ref.document == "support_errors"
    assert ref.section == ""
    assert ref.heading == "售后故障与错误码速查 > 配额与限流类错误（一）"


def test_missing_expected_keeps_the_annotation_order() -> None:
    expected = (
        Expected(document="promo_rules", section="3"),
        Expected(document="points_policy", section="3"),
        Expected(document="refund_policy", section="1"),
    )
    materials = [
        MaterialRef(document="promo_rules", section="3", heading="", section_id="a", chunk_id="a"),
        MaterialRef(
            document="refund_policy", section="4", heading="", section_id="b", chunk_id="b"
        ),
    ]
    assert [item.label for item in missing_expected(expected, materials)] == [
        "points_policy:3",
        "refund_policy:1",
    ]


# ─────────────────────────────────────────────── 选题：机械条件与排序


def _probe(
    *,
    source: str,
    origin: str,
    missing: int = 1,
    total: int = 1,
    top_k: int = B_TOP_K,
) -> Probe:
    expected = tuple(Expected(document=f"d{index}", section=str(index)) for index in range(total))
    hits = tuple(
        MaterialRef(
            document=f"d{index}", section=str(index), heading="", section_id="s", chunk_id="c"
        )
        for index in range(total - missing)
    )
    return Probe(
        source=source,
        origin_id=origin,
        query=f"query-{origin}",
        top_k=top_k,
        expected=expected,
        hits=hits,
        missing=expected[total - missing :] if missing else (),
    )


def test_selection_takes_refs_first_then_eval_with_all_missing_then_partial() -> None:
    probes = [
        _probe(source="eval", origin="q9", missing=1, total=1),  # 全部缺席
        _probe(source="eval", origin="q100", missing=1, total=1),  # 全部缺席（自然序在 q9 之后）
        _probe(source="eval", origin="q13", missing=1, total=2),  # 部分缺席
        _probe(source="refs", origin="r02", missing=1, total=2),
        _probe(source="refs", origin="r10", missing=2, total=2),
        _probe(source="eval", origin="q1", missing=0, total=1),  # 落选：top-2 已覆盖
    ]
    chosen = select_b2_cases(probes, limit=4)
    assert [(probe.source, probe.origin_id) for probe in chosen] == [
        ("refs", "r02"),
        ("refs", "r10"),
        ("eval", "q9"),
        ("eval", "q100"),
    ]


def test_selection_never_picks_a_probe_whose_expected_is_fully_hit() -> None:
    probes = [_probe(source="refs", origin="r01", missing=0, total=1)]
    assert select_b2_cases(probes) == []


def test_case_order_key_is_natural() -> None:
    ids = ["q100", "q13", "q9", "r08", "r10"]
    assert sorted(ids, key=case_order_key) == ["q9", "q13", "q100", "r08", "r10"]


def test_build_cases_numbers_them_in_selection_order() -> None:
    probes = [
        _probe(source="refs", origin="r02", missing=1, total=2),
        _probe(source="refs", origin="r03", missing=1, total=2),
    ]
    cases = build_cases(probes)
    assert [case.case_id for case in cases] == ["B01", "B02"]
    assert [case.group for case in cases] == [GROUP_B, GROUP_B]
    assert [case.origin_id for case in cases] == ["r02", "r03"]


# ─────────────────────────────────────────────── 题集不变量（选题条件真的成立）


def _cases(count: int, *, source: str = "refs", top_k: int = B_TOP_K) -> list[EvalCase]:
    return [
        EvalCase(
            case_id=f"B{index:02d}",
            group=GROUP_B,
            source=source,
            origin_id=f"r{index:02d}",
            query=f"q{index}",
            expected=(Expected(document="d", section="1"),),
            top_k=top_k,
        )
        for index in range(1, count + 1)
    ]


def test_check_case_set_passes_when_ten_cases_and_every_probe_shows_a_miss() -> None:
    cases = _cases(10)
    probes = [
        _probe(source=case.source, origin=case.origin_id, missing=1, total=2) for case in cases
    ]
    assert check_case_set(cases, probes=probes)["ok"] is True


def test_check_case_set_fails_when_a_question_has_no_mechanical_miss() -> None:
    cases = _cases(10)
    probes = [
        _probe(source=case.source, origin=case.origin_id, missing=1, total=2) for case in cases
    ]
    probes[3] = _probe(source=cases[3].source, origin=cases[3].origin_id, missing=0, total=1)
    result = check_case_set(cases, probes=probes)
    assert result["ok"] is False
    assert any("选题条件不成立" in problem for problem in result["problems"])


def test_check_case_set_fails_when_case_count_is_wrong_or_answer_key_is_missing() -> None:
    cases = _cases(9)
    result = check_case_set(cases)
    assert result["ok"] is False
    assert any("题数" in problem for problem in result["problems"])

    cases = _cases(10)
    cases[0] = EvalCase(
        case_id=cases[0].case_id,
        group=GROUP_B,
        source=cases[0].source,
        origin_id=cases[0].origin_id,
        query=cases[0].query,
        expected=(),
        top_k=B_TOP_K,
    )
    result = check_case_set(cases)
    assert any("标准答案" in problem for problem in result["problems"])


# ─────────────────────────────────────────────── 归档：缺什么、前提还在不在


def _archive(**overrides: Any) -> dict[str, Any]:
    questions = {
        "r01": {"query": "q1", "stop_reason": "max_rounds", "hits": [{"chunk_id": "a"}]},
        "r06": {"query": "q2", "stop_reason": "max_rounds", "hits": [{"chunk_id": "b"}]},
        "r08": {"query": "q3", "stop_reason": "max_rounds", "hits": [{"chunk_id": "c"}]},
    }
    questions.update(overrides.pop("questions", {}))
    return {
        "max_rounds_ids": ["r01", "r06", "r08"],
        "questions": questions,
        **overrides,
    }


def test_archive_gaps_reports_that_the_archive_cannot_be_rejudged_in_place() -> None:
    """归档只有材料与停止原因，没有要点清单 / 逐要点 coverage / 补证切分。"""

    gaps = archive_gaps(_archive())
    assert gaps["reusable_as_is"] is False
    assert gaps["missing"]["points"] == ["r01", "r06", "r08"]
    assert gaps["missing"]["coverage"] == ["r01", "r06", "r08"]
    assert gaps["missing"]["followup_split"] == ["r01", "r06", "r08"]


def test_archive_gaps_notices_when_a_run_does_record_coverage() -> None:
    archive = _archive(
        questions={
            "r01": {"query": "q1", "stop_reason": "max_rounds", "hits": [{}], "coverage": []}
        }
    )
    gaps = archive_gaps(archive)
    assert gaps["missing"]["coverage"] == ["r06", "r08"]


def test_check_archived_premise_pins_the_three_questions() -> None:
    check_archived_premise(_archive())  # 不抛
    with pytest.raises(RuntimeError):
        check_archived_premise(_archive(max_rounds_ids=["r01", "r06"]))
    with pytest.raises(RuntimeError):
        check_archived_premise(_archive(questions={"r08": {"query": "q3", "hits": []}}))


# ─────────────────────────────────────────────── 预算与分账


def test_predict_model_calls_fits_the_cap_and_sums_the_breakdown() -> None:
    predicted = predict_model_calls()
    assert predicted["nominal"] == 3 * NOMINAL_CALLS_PER_CASE + 10 * 2 * NOMINAL_CALLS_PER_CASE
    assert predicted["worst"] == 3 * WORST_CALLS_PER_CASE + 10 * 2 * WORST_CALLS_PER_CASE
    assert predicted["worst_fits"] is True
    assert predicted["worst"] <= predicted["budget"]
    assert predicted["breakdown"]["A"]["cases"] == 3
    assert predicted["breakdown"]["B"]["conditions"] == 2


def test_predict_model_calls_flags_an_over_budget_plan() -> None:
    predicted = predict_model_calls(b_cases=20)
    assert predicted["worst_fits"] is False


def test_case_groups_are_the_two_documented_ones() -> None:
    assert {GROUP_A, GROUP_B} == {"A", "B"}
