"""#159 两组对照脚本的离线守卫：读数形状、选择规则、预算预估。

这两组的脚本都要**连库、烧模型调用**才能跑，所以「脚本自己怎么算」必须能在离线用纯函数
钉住：数字是从响应里读出来的（不是按开关推断的），配对是逐题的，预算预估与实跑口径一致。
"""

from __future__ import annotations

import pytest

from scripts import eval_refs, eval_t159_orchestration


def _outcome(
    question_id: str,
    *,
    materials: int = 2,
    block: dict | None = None,
    degraded_reason: str | None = None,
    latency_ms: float = 100.0,
) -> eval_refs.Outcome:
    """一道题的一次运行：交付单元从命中里来，编排块从响应里来。"""

    hits = [
        eval_refs.Hit(
            chunk_id=f"c-{index}",
            document="refund_policy",
            heading_path=f"会员订单退款政策 > {index + 1}. 节",
            content="材料正文" * 10,
        )
        for index in range(materials)
    ]
    return eval_refs.Outcome(
        id=question_id,
        query="q",
        mechanism="cross_section",
        hits=hits,
        orchestration=block,
        degraded_reason=degraded_reason,
        latency_ms=latency_ms,
        model_calls=1,
    )


def _block(
    *,
    points: int = 2,
    completeness: str = "complete",
    primary: tuple[str, ...] = ("c-0", "c-1"),
    supporting: tuple[str, ...] = (),
) -> dict:
    evidence = [
        {
            "evidence_id": f"E{index + 1}",
            "point_id": "A1",
            "role": "primary",
            "chunk_id": chunk_id,
            "content": "核心片正文" * 20,
        }
        for index, chunk_id in enumerate(primary)
    ] + [
        {
            "evidence_id": f"E{len(primary) + index + 1}",
            "point_id": "A2",
            "role": "supporting",
            "chunk_id": chunk_id,
            "content": "辅助片正文" * 20,
        }
        for index, chunk_id in enumerate(supporting)
    ]
    return {
        "completeness": completeness,
        "points": [
            {"point_id": f"A{index + 1}", "text": "要点", "has_primary": True}
            for index in range(points)
        ],
        "evidence": evidence,
    }


# ───────────────────────────────────────── 第一组：材料读数从响应里读，不按开关推断


def test_off_side_has_no_block_and_no_extra_model_call() -> None:
    """关着时：块是 ``None``，编排那边的调用数是 0——「没有这一块」不是「块是空的」。"""

    facts = eval_t159_orchestration.material_facts(_outcome("r01"), orchestration_enabled=False)
    assert facts["materials"] == 2
    assert facts["material_chars"] > 0
    assert facts["completeness"] is None
    assert facts["points"] is None
    assert facts["orchestration_llm_calls"] == 0


def test_on_side_reads_points_evidence_and_unassigned_materials() -> None:
    """开着且跑成了：要点数、核心 / 辅助片数、**没被挂上任何要点**的材料条数都读出来。"""

    facts = eval_t159_orchestration.material_facts(
        _outcome("r01", block=_block(primary=("c-0",), supporting=("c-1",))),
        orchestration_enabled=True,
    )
    assert facts["points"] == 2
    assert facts["primary"] == 1
    assert facts["supporting"] == 1
    assert facts["covered_materials"] == 2
    assert facts["unassigned_materials"] == 0
    assert facts["completeness"] == "complete"
    assert facts["orchestration_llm_calls"] == 2


def test_materials_that_are_never_assigned_are_counted_as_unassigned() -> None:
    """编排只把 1 条材料挂到要点上 → 另一条是「没被用上」，不能算成「材料量下降」的自由项。"""

    facts = eval_t159_orchestration.material_facts(
        _outcome("r01", materials=3, block=_block(primary=("c-0",))),
        orchestration_enabled=True,
    )
    assert facts["covered_materials"] == 1
    assert facts["unassigned_materials"] == 2


def test_a_degraded_on_side_does_not_invent_a_block() -> None:
    """编排失败 → 没有块，且调用次数**不能**记成 0：至少付了一次，最多两次。"""

    facts = eval_t159_orchestration.material_facts(
        _outcome("r01", degraded_reason="orchestration failed"), orchestration_enabled=True
    )
    assert facts["completeness"] is None
    assert facts["orchestration_llm_calls"] is None


def test_off_side_degrades_are_not_attributed_to_orchestration() -> None:
    """关着时那道降级串不可能出现；真出现了就说明编排这一步没被关掉。"""

    facts = eval_t159_orchestration.material_facts(
        _outcome("r01", degraded_reason="keyword search failed"), orchestration_enabled=False
    )
    assert facts["orchestration_llm_calls"] == 0


def test_both_sides_are_paired_per_question() -> None:
    """逐题配对按题号，不按位置：同一道题的三臂读数放在同一行。"""

    rows = eval_t159_orchestration.compare_rows(
        {
            "off": [_outcome("r01"), _outcome("r02")],
            "on_deploy": [
                _outcome("r01", degraded_reason="orchestration failed"),
                _outcome("r02", degraded_reason="orchestration failed"),
            ],
            "on_diagnostic": [_outcome("r01", block=_block()), _outcome("r02", block=_block())],
        }
    )
    assert [row["id"] for row in rows] == ["r01", "r02"]
    assert rows[0]["on_diagnostic"]["completeness"] == "complete"
    assert rows[0]["on_deploy"]["completeness"] is None
    assert rows[0]["off"]["completeness"] is None


def test_completeness_distribution_and_degraded_rate_only_count_the_on_arms() -> None:
    """完整度三态与降级率都是**开编排各臂**的读数；off 臂没有块，不该混进同一个分布里。"""

    rows = eval_t159_orchestration.compare_rows(
        {
            "off": [_outcome("r01"), _outcome("r02"), _outcome("r03")],
            "on_deploy": [
                _outcome("r01", degraded_reason="orchestration failed"),
                _outcome("r02", degraded_reason="orchestration failed"),
                _outcome("r03", degraded_reason="orchestration failed"),
            ],
            "on_diagnostic": [
                _outcome("r01", block=_block(completeness="complete")),
                _outcome("r02", block=_block(completeness="partial")),
                _outcome("r03", block=_block(completeness="missing")),
            ],
        }
    )
    summary = eval_t159_orchestration.aggregate(rows)

    assert summary["completeness_by_arm"]["on_diagnostic"] == {
        "complete": 1,
        "partial": 1,
        "missing": 1,
    }
    assert summary["degraded_by_arm"] == {"off": 0, "on_deploy": 3, "on_diagnostic": 0}
    assert summary["arms"]["on_deploy"]["degraded"]["rate"] == pytest.approx(1.0)


def test_orchestration_calls_are_counted_as_a_lower_bound_when_degraded() -> None:
    """降级处不猜 2 次：记账写下界，产物里同时给出上界（第二次可能已经发出去了）。"""

    rows = eval_t159_orchestration.compare_rows(
        {
            "off": [_outcome("r01")],
            "on_deploy": [_outcome("r01", degraded_reason="orchestration failed")],
            "on_diagnostic": [_outcome("r01", block=_block())],
        }
    )
    calls = eval_t159_orchestration.aggregate(rows)["arms"]
    assert calls["on_deploy"]["orchestration_llm_calls_lower_bound"] == 1
    assert calls["on_diagnostic"]["orchestration_llm_calls_lower_bound"] == 2
    assert calls["off"]["orchestration_llm_calls_lower_bound"] == 0


def test_predicted_calls_matches_what_the_run_will_really_pay() -> None:
    """开跑前打印的预估必须与实跑口径同一套：off 1 次嵌入；两个开臂各多 ≤2 次模型调用。"""

    predicted = eval_t159_orchestration.predicted_calls(
        refs_questions=8, gate_questions=12, rerank=False, warmup_per_batch=4, batches=2
    )
    assert predicted["off_calls"] == 20
    assert predicted["on_deploy_calls_upper"] == 20 + 40
    assert predicted["on_diagnostic_calls_upper"] == 20 + 40
    # 暖机 ×2 题集（每集 1 嵌入 + 1 嵌入 + 2 结构化）+ 三臂检索 + 两开臂编排
    assert predicted["total_upper"] == 2 * 4 + 20 + 60 + 60


def test_predicted_calls_charge_rerank_when_it_is_on() -> None:
    """重排开着时每一臂每题再多一次：预估不许漏掉它。"""

    predicted = eval_t159_orchestration.predicted_calls(
        refs_questions=2, gate_questions=2, rerank=True, warmup_per_batch=4, batches=2
    )
    assert predicted["off_calls"] == 8
    assert predicted["on_deploy_calls_upper"] == 8 + 8


# ───────────────────────────────────────────────── 选 12 道「有明确多要点」的基线题


def test_gate_questions_are_cross_section_with_two_or_more_relevant_sections() -> None:
    """挑选规则写死在用例里：跨节题型 + 标注横跨 ≥2 节，按题号顺序取前 N 道。"""

    from scripts.eval_retrieval import Question

    questions = [
        Question(
            id="q01", type="cross_section", query="a", note="", labels=(("d", "h1"), ("d", "h2"))
        ),
        Question(id="q02", type="cross_section", query="b", note="", labels=(("d", "h1"),)),
        Question(
            id="q03", type="exact_code", query="c", note="", labels=(("d", "h1"), ("d", "h2"))
        ),
        Question(
            id="q04", type="cross_section", query="d", note="", labels=(("d", "h1"), ("d", "h2"))
        ),
    ]
    assert [q.id for q in eval_t159_orchestration.select_gate_questions(questions)] == [
        "q01",
        "q04",
    ]


def test_gate_questions_are_capped_at_the_ticket_number() -> None:
    """票面写的是 12 道：超过就截断，且顺序确定（重跑选同一批）。"""

    from scripts.eval_retrieval import Question

    questions = [
        Question(
            id=f"q{index:02d}",
            type="cross_section",
            query="q",
            note="",
            labels=(("d", "h1"), ("d", "h2")),
        )
        for index in range(20)
    ]
    selected = eval_t159_orchestration.select_gate_questions(questions)
    assert len(selected) == eval_t159_orchestration.GATE_LIMIT == 12
    assert [q.id for q in selected] == [f"q{index:02d}" for index in range(12)]


def test_a_dry_run_prints_the_prediction_without_touching_any_database() -> None:
    """``--dry-run`` 只选题 + 打印预估：不连库、不烧一次模型调用。"""

    assert eval_t159_orchestration.main(["--dry-run"]) == 0


# ─────────────────────────────────── 编排块的正文要进产物（人工抽检的唯一依据）


def test_the_full_block_is_kept_so_a_human_can_audit_the_completeness_verdict() -> None:
    """抽检要回答「核心片是否真的覆盖要点」，所以证据**正文**必须原样进产物。

    只留条数的话，抽检只能重跑一遍——那就不是抽检，是第二次实验。
    """

    block = _block()
    kept = eval_t159_orchestration.audit_block(block)
    assert kept["points"]
    assert [item["chunk_id"] for item in kept["primary"]] == ["c-0", "c-1"]
    assert all(item["content"] for item in kept["primary"])


def test_research_style_objects_without_a_block_are_handled() -> None:
    """Research 侧的响应用的是 ``evidence_state``，不是编排块：不能把两者混成一个读数。"""

    outcome = _outcome("r01")
    outcome.evidence_state = "complete"
    facts = eval_t159_orchestration.material_facts(outcome, orchestration_enabled=True)
    assert facts["completeness"] is None


# ─────────────────────────────── 诊断臂的「重跑跑不完的题」：只补那一列，不换掉难看的那档


def _retry_row(question_id: str, *, diagnostic_calls: int | None) -> dict:
    return {
        "id": question_id,
        "query": "q",
        "mechanism": "cross_section",
        "off": _facts(orchestration_llm_calls=0),
        "on_deploy": _facts(orchestration_llm_calls=None),
        "on_diagnostic": _facts(orchestration_llm_calls=diagnostic_calls),
        "off_latency_ms": 1.0,
        "on_deploy_latency_ms": 2.0,
        "on_diagnostic_latency_ms": 3.0,
        "off_model_calls": 1,
        "on_deploy_model_calls": 1,
        "on_diagnostic_model_calls": 1,
        "off_degraded_reason": None,
        "on_deploy_degraded_reason": "orchestration failed",
        "on_diagnostic_degraded_reason": "orchestration failed"
        if diagnostic_calls is None
        else None,
    }


def _facts(*, orchestration_llm_calls: int | None) -> dict:
    """aggregate 会读的那几个字段（一份「没有块」的最小事实）。"""

    return {
        "materials": 2,
        "material_chars": 20,
        "points": None,
        "evidence": None,
        "primary": None,
        "supporting": None,
        "covered_materials": None,
        "unassigned_materials": None,
        "evidence_chars": None,
        "completeness": None,
        "orchestration_llm_calls": orchestration_llm_calls,
    }


def _retry_payload() -> dict:
    batch = {
        "kb_ids": ["kb"],
        "tenant_id": "tenant",
        "keyword_alias": "alias",
        "rows": [
            _retry_row("r01", diagnostic_calls=2),
            _retry_row("r02", diagnostic_calls=None),
        ],
    }
    return {"in_batches": {"refs": batch}}


def test_missing_diagnostic_ids_only_lists_questions_without_a_block() -> None:
    """重跑名单只来自诊断臂：块在的题不重跑，另外两臂不参与这个判断。"""

    plan = eval_t159_orchestration.missing_diagnostic_ids(_retry_payload())
    assert plan == {"refs": ["r02"]}


def test_merge_rerun_replaces_only_the_diagnostic_arm() -> None:
    """把重跑读数并回去时：诊断臂那一列换新，off / on_deploy 两列一个字不许动。"""

    payload = _retry_payload()
    retried = eval_t159_orchestration.merge_diagnostic_arm(
        payload,
        batch_name="refs",
        outcomes=[_outcome("r02", block=_block())],
        timeout_ms=60_000,
    )
    batch = payload["in_batches"]["refs"]
    row = batch["rows"][1]
    assert retried == ["r02"]
    assert row["on_diagnostic"]["completeness"] == "complete"
    assert row["on_diagnostic_budget_ms"] == 60_000
    # 抽检要用的编排块正文也随重跑一起并回产物。
    assert row["on_diagnostic_block"]["points"]
    # 另外两臂保持原样（含它们难看的读数）。
    assert row["on_deploy"]["orchestration_llm_calls"] is None
    assert row["off"]["orchestration_llm_calls"] == 0
    # 合计跟着重算：两道题里一道 complete、一道仍无块。
    assert batch["aggregate"]["completeness_by_arm"]["on_diagnostic"] == {
        "complete": 1,
        "partial": 0,
        "missing": 0,
    }
