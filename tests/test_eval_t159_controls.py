"""#159 两组对照脚本的离线守卫：读数形状、选择规则、预算预估。

这两组的脚本都要**连库、烧模型调用**才能跑，所以「脚本自己怎么算」必须能在离线用纯函数
钉住：数字是从响应里读出来的（不是按开关推断的），配对是逐题的，预算预估与实跑口径一致。
"""

from __future__ import annotations

import pytest

from scripts import eval_refs, eval_t159_orchestration, eval_t159_split_versions


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


# ══════════════════════════════════════════ 第二组：切分点变化的 v1 / v2


def _section(heading_path: str) -> str | None:
    """从 heading_path 尾段取节号（与 ``eval_refs.section_of_heading_path`` 同一口径）。"""

    return eval_refs.section_of_heading_path(heading_path)


def _chunk_row(
    *,
    chunk_id: str,
    document: str = "points_policy",
    heading_path: str,
    section_id: str,
    content: str = "正文",
    chunk_type: str = "prose",
) -> eval_t159_split_versions.ChunkRow:
    return eval_t159_split_versions.ChunkRow(
        chunk_id=chunk_id,
        document=document,
        heading_path=heading_path,
        section=_section(heading_path),
        section_id=section_id,
        chunk_type=chunk_type,
        content=content,
    )


def test_the_one_split_point_change_is_the_h3_boundary() -> None:
    """v1 = 生产切法（h3 成节）；v2 = 只改一处：h3 不再成节，正文并入父级 h2。

    这一处是被语料与题集**逼出来**的：points_policy 的 2.1 / 2.2 是 h3，而题集有两道题
    明确指向「第 2.2 节」（r04 / r08）。换表格行级片或 small-to-big 粒度在这份纯散文语料
    上都不会改任何交付单元——那不是「一处切分点」，那是换个说法。
    """

    text = (eval_t159_split_versions.REFS_CORPUS_DIR / "points_policy.md").read_text(
        encoding="utf-8"
    )
    pieces_v1 = eval_t159_split_versions.build_splitter_for_version(
        "structure", chunk_size=800, document_id="doc", section_level=3
    ).split(text)
    pieces_v2 = eval_t159_split_versions.build_splitter_for_version(
        "structure", chunk_size=800, document_id="doc", section_level=2
    ).split(text)
    paths_v1 = {piece.metadata["heading_path"] for piece in pieces_v1}
    paths_v2 = {piece.metadata["heading_path"] for piece in pieces_v2}
    assert "积分规则 > 2. 积分抵扣与使用上限 > 2.1 抵扣比例" in paths_v1
    assert "积分规则 > 2. 积分抵扣与使用上限 > 2.2 抵扣上限" in paths_v1
    assert "积分规则 > 2. 积分抵扣与使用上限 > 2.1 抵扣比例" not in paths_v2
    assert "积分规则 > 2. 积分抵扣与使用上限" in paths_v2
    merged = [
        piece
        for piece in pieces_v2
        if piece.metadata["heading_path"] == "积分规则 > 2. 积分抵扣与使用上限"
    ]
    assert any("抵扣比例固定为 100 积分抵 1 元" in piece.text for piece in merged)
    assert any("银卡及以上时，200 元的固定上限提高到 500 元" in piece.text for piece in merged)


def test_documents_without_h3_are_cut_identically_under_both_versions() -> None:
    """只改 h3 边界 ⇒ 没有 h3 的文档两版逐片相同（证明这一处改动没有连带别的东西）。"""

    text = (eval_t159_split_versions.REFS_CORPUS_DIR / "refund_policy.md").read_text(
        encoding="utf-8"
    )
    pieces_v1 = [
        piece.text
        for piece in eval_t159_split_versions.build_splitter_for_version(
            "structure", chunk_size=800, document_id="doc", section_level=3
        ).split(text)
    ]
    pieces_v2 = [
        piece.text
        for piece in eval_t159_split_versions.build_splitter_for_version(
            "structure", chunk_size=800, document_id="doc", section_level=2
        ).split(text)
    ]
    assert pieces_v1 == pieces_v2


def test_inventory_counts_sections_and_section_ids() -> None:
    """每个版本先落一份清单：切片 / 节 / 节的身份证（``section_id``）都在。"""

    rows = [
        _chunk_row(chunk_id="c1", heading_path="积分规则 > 1. 积分获取与计算", section_id="s1"),
        _chunk_row(chunk_id="c2", heading_path="积分规则 > 2. 积分抵扣与使用上限", section_id="s2"),
        _chunk_row(chunk_id="c3", heading_path="积分规则 > 3. 积分退还与失效", section_id="s3"),
    ]
    inv = eval_t159_split_versions.inventory(rows)
    assert inv["chunks"] == 3
    assert inv["sections"] == 3
    assert inv["section_ids"] == {
        "points_policy:1": "s1",
        "points_policy:2": "s2",
        "points_policy:3": "s3",
    }


def test_section_identity_diff_reports_sections_that_vanish_into_a_parent() -> None:
    """节身份**应当有差异**：v2 把 2.1 / 2.2 并进 2，那两个节的 id 在 v2 里不存在。"""

    v1 = eval_t159_split_versions.inventory(
        [
            _chunk_row(
                chunk_id="c1",
                heading_path="积分规则 > 2. 积分抵扣与使用上限",
                section_id="s2",
            ),
            _chunk_row(
                chunk_id="c2",
                heading_path="积分规则 > 2. 积分抵扣与使用上限 > 2.1 抵扣比例",
                section_id="s21",
            ),
            _chunk_row(
                chunk_id="c3",
                heading_path="积分规则 > 2. 积分抵扣与使用上限 > 2.2 抵扣上限",
                section_id="s22",
            ),
        ]
    )
    v2 = eval_t159_split_versions.inventory(
        [
            _chunk_row(
                chunk_id="c9", heading_path="积分规则 > 2. 积分抵扣与使用上限", section_id="s2"
            )
        ]
    )
    diff = eval_t159_split_versions.section_identity_diff(v1, v2)
    assert diff["same"] == 1
    assert diff["only_v1"] == ["points_policy:2.1", "points_policy:2.2"]
    assert diff["only_v2"] == []
    # 同一个节的身份证跨版本必须相等（那是 #143 的承诺，改切法不该动摇它）。
    assert diff["changed"] == []


def test_section_identity_diff_flags_an_unstable_id_as_a_defect() -> None:
    """同一个 (文档, 节号) 两版算出不同 ``section_id`` ⇒ 身份算法坏了，不是切法变了。"""

    v1 = eval_t159_split_versions.inventory(
        [_chunk_row(chunk_id="c1", heading_path="积分规则 > 1. 积分获取与计算", section_id="s1")]
    )
    v2 = eval_t159_split_versions.inventory(
        [
            _chunk_row(
                chunk_id="c2", heading_path="积分规则 > 1. 积分获取与计算", section_id="s1-bad"
            )
        ]
    )
    diff = eval_t159_split_versions.section_identity_diff(v1, v2)
    assert diff["changed"] == ["points_policy:1"]


def _hit_outcome(
    question_id: str, units: list[tuple[str, str]], *, contents: list[str] | None = None
) -> eval_refs.Outcome:
    """一道题的命中：``units`` 是 (文档, 节号)；``contents`` 缺省按单元编一段可读正文。"""

    hits = []
    for index, (document, section) in enumerate(units):
        body = (
            contents[index]
            if contents is not None and index < len(contents)
            else f"{document} 第 {section} 节的正文"
        )
        hits.append(
            eval_refs.Hit(
                chunk_id=f"c-{document}-{section}",
                document=document,
                heading_path=f"{document} > {section}. 节",
                content=body,
            )
        )
    return eval_refs.Outcome(id=question_id, query="q", mechanism="cross_document", hits=hits)


def test_hit_diff_lists_added_and_removed_delivery_units() -> None:
    """命中集合按「文档:节」比：v2 少一个 2.2 单元、多一个 2 单元。"""

    off = _hit_outcome("r04", [("points_policy", "2.2"), ("points_policy", "4")])
    on = _hit_outcome("r04", [("points_policy", "2"), ("points_policy", "4")])
    diff = eval_t159_split_versions.hit_diff(off, on)
    assert diff["removed"] == ["points_policy:2.2"]
    assert diff["added"] == ["points_policy:2"]


def test_a_disappeared_unit_is_traced_to_the_parent_that_absorbed_it() -> None:
    """「单元没了」分两种：内容还在（被父节吞了）vs 内容真的没取回。两者处置完全不同。"""

    v1 = _hit_outcome(
        "r04",
        [("points_policy", "2.2")],
        contents=["### 2.2 抵扣上限\n\n银卡及以上时，200 元的固定上限提高到 500 元。"],
    )
    v2 = _hit_outcome(
        "r04",
        [("points_policy", "2")],
        contents=[
            "## 2. 积分抵扣与使用上限\n\n### 2.1 抵扣比例\n\n抵扣比例固定为 100 积分抵 1 元。"
            " ### 2.2 抵扣上限\n\n银卡及以上时，200 元的固定上限提高到 500 元。"
        ],
    )
    absorbed = eval_t159_split_versions.absorbed_units(v1, v2)
    assert absorbed["absorbed"] == ["points_policy:2.2"]
    assert absorbed["lost"] == []


def test_content_that_is_really_missing_is_not_called_absorbed() -> None:
    """父节里没有这段正文 ⇒ 这是真的丢了一条材料，不许拿「合并」当借口。"""

    v1 = _hit_outcome("r04", [("points_policy", "2.2")], contents=["这段正文不在 v2 的任何命中里"])
    v2 = _hit_outcome("r04", [("points_policy", "2")], contents=["完全别的内容"])
    absorbed = eval_t159_split_versions.absorbed_units(v1, v2)
    assert absorbed["absorbed"] == []
    assert absorbed["lost"] == ["points_policy:2.2"]


def test_coverage_of_reports_which_expected_sections_were_found() -> None:
    """期望节覆盖按 ``文档:节`` 对账：命中里有就是有，没取回就列进 missing。"""

    coverage = eval_t159_split_versions.coverage_of(
        ["promo_rules:1", "points_policy:2.2"],
        ["points_policy:2.2", "promo_rules:1", "refund_policy:2"],
    )
    assert coverage["found"] == ["points_policy:2.2", "promo_rules:1"]
    assert coverage["missing"] == ["refund_policy:2"]
    assert coverage["found_count"] == 2 and coverage["expected_count"] == 3


def test_coverage_of_counts_a_merged_unit_as_missing_under_this_version_s_labels() -> None:
    """v2 交付的是 2（2.2 被并进去）：按 v2 的节号对账，期望的 2.2 就算「没取回」。

    这正是这组对照要看的那件事：读数会掉，但**材料还在**（``absorbed_units`` 管那个），
    两件事必须分开报。
    """

    coverage = eval_t159_split_versions.coverage_of(
        ["points_policy:2", "promo_rules:1"], ["points_policy:2.2", "promo_rules:1"]
    )
    assert coverage["found"] == ["promo_rules:1"]
    assert coverage["missing"] == ["points_policy:2.2"]


def test_reference_labels_resolve_a_section_that_still_exists_and_report_the_rest() -> None:
    """标签按 ``(document, section)`` 解析（#157 的纪律）：节还在就照样解析得到。"""

    rows = [
        _chunk_row(chunk_id="c2", heading_path="积分规则 > 2. 积分抵扣与使用上限", section_id="s2"),
        _chunk_row(
            chunk_id="c9",
            document="promo_rules",
            heading_path="促销 > 1. 活动类型与叠加原则",
            section_id="p1",
        ),
    ]
    question = {
        "id": "r04",
        "expected": [
            {"document": "points_policy", "section": "2.2", "role": "core"},
            {"document": "promo_rules", "section": "1", "role": "pointer"},
        ],
    }
    labels = eval_t159_split_versions.reference_labels(question, rows)
    assert labels["reference"] == ["c9"]
    assert labels["unresolved"] == ["points_policy:2.2"]


def test_reference_labels_can_fall_back_to_the_parent_section_for_a_merged_unit() -> None:
    """诊断口径：2.2 并进 2 之后，父节那一片就是承载那段正文的材料（**单独标注**）。"""

    rows = [
        _chunk_row(chunk_id="c2", heading_path="积分规则 > 2. 积分抵扣与使用上限", section_id="s2"),
    ]
    question = {
        "id": "r04",
        "expected": [{"document": "points_policy", "section": "2.2", "role": "core"}],
    }
    strict = eval_t159_split_versions.reference_labels(question, rows)
    relaxed = eval_t159_split_versions.reference_labels(question, rows, allow_parent_fallback=True)
    assert strict["reference"] == []
    assert strict["unresolved"] == ["points_policy:2.2"]
    assert relaxed["reference"] == ["c2"]
    assert relaxed["unresolved"] == []
    assert relaxed["fallback"] == ["points_policy:2.2"]


def test_split_versions_dry_run_prints_plan_without_touching_any_database() -> None:
    """``--dry-run`` 打印要建什么、要跑什么、预算多少：不连库、不烧一次嵌入。"""

    assert eval_t159_split_versions.main(["--dry-run"]) == 0
