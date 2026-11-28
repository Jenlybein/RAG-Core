"""票 #167（缺口闸真实行为评测）的离线守卫。

守的是**口径**，不是「脚本跑得动」——这一票是一次**测量**，量错方向比跑不起来更糟：

1. **题集不变量**：A 组 10 题必须**逐字**以题库原题为前缀（追加的方面接在后面），B 组 10 题
   必须是主题不在语料里的整题；每题的「不可答方面」都有**一个关键词**。
2. **机械证据**：关键词在**库内文档正文与已入库切片正文**里 0 命中——两个来源都要查
   （只查一处会漏掉「文档正文有、切片没进」与反过来这两种情况）。命中不为 0 必须当场失败，
   不许靠感觉标「这题答不了」。
3. **指标口径**：`insufficient` 块数、声明对齐度的分布、欠声明占比、拦截率、⑤ 触发率、
   `unknown` 残留率——每个率都给分子分母，期望值手算给出，不拿被测函数反推。
4. **离线复算**：`recompute` 只读落盘的逐题记录，一次模型调用都不发。
5. **预算**：开跑前的预估（名义 + 最坏）都不超 110 次；`plan` 模式一次模型调用都不发。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import eval_t167_gap_gate as ev

REPO_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────── 1. 题集不变量


def test_case_set_has_ten_a_and_ten_b() -> None:
    cases = ev.load_cases()

    assert len(cases) == ev.CASE_COUNT == 20
    assert [case.case_id for case in cases if case.group == "A"] == [
        f"A{index:02d}" for index in range(1, 11)
    ]
    assert [case.case_id for case in cases if case.group == "B"] == [
        f"B{index:02d}" for index in range(1, 11)
    ]


def test_a_cases_are_bank_questions_plus_one_unanswerable_aspect() -> None:
    """A 组的题面必须**以题库原题开头**，且追加的方面非空。

    这样「半可答」不是一句形容词：前半句是库里能答的原题，后半句是库里没有的那个方面。
    """

    bank = {
        str(item["id"]): str(item["query"])
        for item in json.loads(ev.BASE_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    }

    a_cases = [case for case in ev.load_cases() if case.group == "A"]
    assert len(a_cases) == 10
    for case in a_cases:
        assert case.base_id in bank, f"{case.case_id} 的原题不在题库里：{case.base_id}"
        assert case.query.startswith(bank[case.base_id]), case.case_id
        assert case.appended.strip(), f"{case.case_id} 没有追加的方面"


def test_b_cases_append_nothing_and_name_an_absent_topic() -> None:
    """B 组**整题**都不在语料里：没有「追加的方面」，只有「不可答的是什么」。

    ``base_id`` 允许非空——那 6 题取自题库的 ``absent`` 标注（见下一条用例），
    它们是既有标注，不是我方构造。
    """

    bank = {
        str(item["id"])
        for item in json.loads(ev.BASE_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    }
    for case in ev.load_cases():
        if case.group != "B":
            continue
        assert case.appended.strip() == ""
        assert case.topic.strip(), f"{case.case_id} 没写「不可答的是什么」"
        assert case.base_id is None or case.base_id in bank


def test_every_case_names_one_keyword_and_they_are_all_distinct() -> None:
    """一题一个关键词：关键词是机械证据的抓手，重复会让两条证据互相冒充。"""

    keywords = [case.keyword for case in ev.load_cases()]
    assert all(keyword.strip() for keyword in keywords)
    assert len(set(keywords)) == len(keywords)


def test_questions_banked_as_absent_come_from_the_bank_itself() -> None:
    """B 组里有 6 题直接取自题库的 ``absent`` 标注（问题侧标注，写在检索之前）。

    这条不是形式主义：它把「这题真的答不了」从**我方构造**换成了**既有标注**，
    剩下的 4 题才是我们新造的。
    """

    payload = json.loads(ev.BASE_QUESTIONS.read_text(encoding="utf-8"))
    absent = {str(item["id"]) for item in payload["questions"] if item.get("type") == "absent"}

    b_cases = [case for case in ev.load_cases() if case.group == "B"]
    from_bank = [case for case in b_cases if case.base_id in absent]
    assert len(from_bank) == 6, [case.case_id for case in from_bank]
    assert len([case for case in b_cases if case.base_id is None]) == 4


def test_check_case_set_reports_every_invariant() -> None:
    checks = ev.check_case_set()

    assert checks["cases"] == 20
    assert checks["a_cases"] == 10
    assert checks["b_cases"] == 10
    assert checks["base_ids_ok"] is True
    assert checks["prefix_ok"] is True
    assert checks["keywords_unique"] is True
    assert checks["bank_absent_cases"] == 6


# ─────────────────────────────────────────────────────────────── 2. 机械证据


def test_evidence_sql_reads_document_and_chunk_bodies() -> None:
    """两个来源都要查：只查一处会漏掉「文档正文有、切片里没有」这类差异。"""

    sql = ev.evidence_sql().lower()

    assert "documents" in sql
    assert "chunks" in sql
    assert sql.count("content ilike") == 2
    assert "doc_hits" in sql
    assert "chunk_hits" in sql


def test_evidence_params_scope_to_the_searched_tenant_and_kbs() -> None:
    case = ev.load_cases()[0]
    params = ev.evidence_params(case, tenant_id="tenant_demo", kb_ids=["kb-a", "kb-b"])

    assert params["tenant_id"] == "tenant_demo"
    assert params["kb_ids"] == ["kb-a", "kb-b"]
    assert params["pattern"] == f"%{case.keyword}%"


def test_keyword_evidence_records_the_query_and_the_hit_counts() -> None:
    case = ev.load_cases()[0]

    def fetch(sql: str, params: dict[str, Any]) -> dict[str, int]:
        assert sql == ev.evidence_sql()
        assert params["pattern"] == f"%{case.keyword}%"
        return {"doc_hits": 0, "chunk_hits": 0}

    evidence = ev.keyword_evidence(case, fetch=fetch)

    assert evidence.keyword == case.keyword
    assert evidence.doc_hits == 0
    assert evidence.chunk_hits == 0
    assert evidence.sql == ev.evidence_sql()
    assert ev.require_absent(evidence) is None


@pytest.mark.parametrize(
    ("doc_hits", "chunk_hits"),
    [(1, 0), (0, 3), (2, 2)],
)
def test_require_absent_fails_loudly_when_a_keyword_appears(doc_hits: int, chunk_hits: int) -> None:
    """任何一处命中都不算 0 命中——「不可答」的机械证据必须是双零。"""

    evidence = ev.KeywordEvidence(
        case_id="A01",
        keyword="量子",
        pattern="%量子%",
        doc_hits=doc_hits,
        chunk_hits=chunk_hits,
        sql=ev.evidence_sql(),
        params={},
    )

    with pytest.raises(ev.EvidenceViolation):
        ev.require_absent(evidence)


def test_keyword_evidence_is_json_safe() -> None:
    evidence = ev.KeywordEvidence(
        case_id="B01",
        keyword="量子",
        pattern="%量子%",
        doc_hits=0,
        chunk_hits=0,
        sql=ev.evidence_sql(),
        params={"tenant_id": "tenant_demo", "kb_ids": ["kb-a"], "pattern": "%量子%"},
    )

    payload = ev.as_json(evidence)

    assert payload["keyword"] == "量子"
    assert payload["doc_hits"] == 0
    assert payload["sql"] == ev.evidence_sql()
    assert json.dumps(payload, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────── 3. 指标口径


def test_insufficient_blocks_counts_only_insufficient_citations() -> None:
    record = {
        "citations": [
            {"status": "insufficient", "text": "材料不足：…", "evidence_ids": []},
            {"status": "supported", "text": "…", "evidence_ids": ["E1"]},
            {"status": "insufficient", "text": "材料不足：…", "evidence_ids": []},
        ]
    }

    assert ev.insufficient_blocks(record) == 2


def test_insufficient_blocks_is_zero_when_the_answer_was_rejected() -> None:
    assert ev.insufficient_blocks({"citations": []}) == 0
    assert ev.insufficient_blocks({}) == 0


def test_declaration_alignment_gives_a_distribution_not_an_average() -> None:
    """手算一组：缺口 3 配声明 1（欠）、3 配 3（齐）、2 配 4（过）、一题没缺口、
    以及一题有缺口但答案被整份作废（= 未测量，不当成欠声明）。"""

    records = [
        {"gaps": ["A1", "A2", "A3"], "insufficient": 1, "answer": "…（材料不足）"},
        {"gaps": ["A1", "A2", "A3"], "insufficient": 3, "answer": "…（材料不足）"},
        {"gaps": ["A1", "A2"], "insufficient": 4, "answer": "…（材料不足）"},
        {"gaps": [], "insufficient": 1, "answer": "…（材料不足）"},
        {"gaps": ["A1"], "insufficient": 0, "answer": None},
    ]

    reading = ev.declaration_alignment(records)

    assert reading["cases_with_gaps"] == 4
    assert reading["answered"] == 3
    assert reading["unanswered"] == 1
    assert reading["pairs"] == {"3→1": 1, "3→3": 1, "2→4": 1}
    assert reading["exact"] == 1
    assert reading["under"] == 1
    assert reading["over"] == 1
    assert reading["under_rate"] == round(1 / 3, 4)
    assert reading["exact_rate"] == round(1 / 3, 4)
    # 没缺口却声明了不足：另一侧的过度声明，单独记，不混进上面的对齐度
    assert reading["declared_without_gaps"] == 1


def test_alignment_does_not_count_a_null_answer_as_an_under_declaration() -> None:
    """``answer = None`` 时声明块**结构上就是 0**：那是被别的校验挡下了，不是闸门放行。"""

    records = [{"gaps": ["A1", "A2"], "insufficient": 0, "answer": None}]

    reading = ev.declaration_alignment(records)

    assert reading["answered"] == 0
    assert reading["unanswered"] == 1
    assert reading["under"] == 0
    assert reading["pairs"] == {}


def test_summarize_gives_every_metric_with_its_denominator() -> None:
    """手算五题：缺口 4 题、拦截 1 题、⑤ 触发 2 题、`unknown` 残留 1 题。"""

    records = [
        {  # 有缺口，答案被闸拦下
            "case": "A01",
            "group": "A",
            "gaps": ["A1", "A2"],
            "coverage": [{"point_id": "A1", "status": "none"}],
            "insufficient": 0,
            "answer": None,
            "answer_rejected_reason": ev.REJECTED_GAP_NOT_DECLARED,
            "model_calls": 4,
            "stop_reason": "max_rounds",
        },
        {  # 有缺口，声明齐
            "case": "A02",
            "group": "A",
            "gaps": ["A1"],
            "coverage": [{"point_id": "A1", "status": "none"}],
            "insufficient": 1,
            "answer": "…（材料不足）",
            "answer_rejected_reason": None,
            "model_calls": 3,
            "stop_reason": "no_new_evidence",
        },
        {  # 有缺口，欠声明
            "case": "B01",
            "group": "B",
            "gaps": ["A1", "A2", "A3"],
            "coverage": [
                {"point_id": "A1", "status": "none"},
                {"point_id": "A2", "status": "none"},
                {"point_id": "A3", "status": "unknown"},
            ],
            "insufficient": 1,
            "answer": "…（材料不足）",
            "answer_rejected_reason": None,
            "model_calls": 5,
            "stop_reason": "max_rounds",
        },
        {  # 有缺口
            "case": "B02",
            "group": "B",
            "gaps": ["A1"],
            "coverage": [{"point_id": "A1", "status": "none"}],
            "insufficient": 1,
            "answer": "…（材料不足）",
            "answer_rejected_reason": None,
            "model_calls": 3,
            "stop_reason": "no_new_evidence",
        },
        {  # 没有缺口
            "case": "B03",
            "group": "B",
            "gaps": [],
            "coverage": [{"point_id": "A1", "status": "supported"}],
            "insufficient": 0,
            "answer": "…（E1）",
            "answer_rejected_reason": None,
            "model_calls": 3,
            "stop_reason": "evidence_complete",
        },
    ]

    metrics = ev.summarize(records)

    assert metrics["cases"] == 5
    assert metrics["缺口出现率"]["value"] == round(4 / 5, 4)
    assert metrics["缺口出现率"]["numerator"] == 4
    assert metrics["缺口出现率"]["denominator"] == 5
    assert metrics["拦截率"]["value"] == round(1 / 4, 4)
    assert metrics["拦截率"]["numerator"] == 1
    assert metrics["拦截率"]["denominator"] == 4
    assert metrics["⑤ 触发率"]["value"] == round(2 / 5, 4)
    assert metrics["⑤ 触发率"]["numerator"] == 2
    assert metrics["unknown 残留率"]["value"] == round(1 / 5, 4)
    assert metrics["unknown 残留率"]["numerator"] == 1
    # 有缺口的 4 题里只有 3 题真的交出了答案（A01 的答案被闸拦下）：分母是 3，不是 4
    assert metrics["声明对齐度"]["cases_with_gaps"] == 4
    assert metrics["声明对齐度"]["answered"] == 3
    assert metrics["声明对齐度"]["unanswered"] == 1
    assert metrics["声明对齐度"]["under"] == 1


def test_summarize_splits_outcomes_of_gap_cases_by_stop_and_answer() -> None:
    """拦截率是 0 时，要看**闸门被驱动之后模型做了什么**：声明了 / 被拦了 / 别的原因被拒。

    这三档分开记，才能回答「是提示词约束住了模型，还是分母太小」。
    """

    records = [
        {
            "case": "A01",
            "gaps": ["A1"],
            "answer": "…（材料不足）",
            "answer_rejected_reason": None,
            "insufficient": 1,
            "coverage": [],
            "model_calls": 4,
            "group": "A",
            "stop_reason": "max_rounds",
        },
        {
            "case": "B01",
            "gaps": ["A1"],
            "answer": None,
            "answer_rejected_reason": ev.REJECTED_GAP_NOT_DECLARED,
            "insufficient": 0,
            "coverage": [],
            "model_calls": 4,
            "group": "B",
            "stop_reason": "max_rounds",
        },
        {
            "case": "B02",
            "gaps": ["A1"],
            "answer": None,
            "answer_rejected_reason": ev.REJECTED_ASSERTION_WITHOUT_CORE,
            "insufficient": 0,
            "coverage": [],
            "model_calls": 4,
            "group": "B",
            "stop_reason": "max_rounds",
        },
    ]

    tally = ev.gap_case_outcomes(records)

    assert tally["declared"] == 1
    assert tally["blocked"] == 1
    assert tally["rejected_other"] == 1
    assert tally["blocked_by_group"] == {"A": 0, "B": 1}
    assert tally["declared_by_group"] == {"A": 1, "B": 0}


def test_recompute_rebuilds_metrics_from_the_artifact_without_calling_models() -> None:
    payload = {
        "model_calls": {"nominal": 80, "worst": 100, "budget": 110, "actual": 999},
        "records": [
            {
                "case": "A01",
                "group": "A",
                "gaps": ["A1"],
                "coverage": [{"point_id": "A1", "status": "none"}],
                "citations": [{"status": "insufficient", "text": "材料不足", "evidence_ids": []}],
                "answer": "…（材料不足）",
                "answer_rejected_reason": None,
                "model_calls": 4,
                "stop_reason": "max_rounds",
            }
        ],
        "metrics": {},
    }

    refreshed = ev.recompute_metrics(payload)

    assert refreshed["records"][0]["insufficient"] == 1
    assert refreshed["metrics"]["缺口出现率"]["value"] == 1.0
    assert refreshed["model_calls"]["actual"] == 4
    assert refreshed["model_calls"]["nominal"] == 80


# ─────────────────────────────────────────────────────────────── 4. ⑤ 的对照（关 ⑤ 再跑）


def _fivefold_record(
    case: str,
    *,
    points: list[str],
    statuses: list[str],
    gaps: list[str],
    fivefold: bool,
    answer: str | None = "…（材料不足）",
    insufficient: int = 0,
    model_calls: int = 4,
) -> dict[str, Any]:
    return {
        "case": case,
        "group": "A",
        "fivefold": fivefold,
        "condition": "fivefold" if fivefold else "control",
        "points": points,
        "coverage": [
            {"point_id": f"A{index}", "status": status, "evidence_ids": []}
            for index, status in enumerate(statuses, start=1)
        ],
        "gaps": gaps,
        "answer": answer,
        "insufficient": insufficient,
        "model_calls": model_calls,
        "stop_reason": "max_rounds",
        "citations": [],
    }


def test_unknown_frequency_counts_cases_and_points_on_the_control_condition() -> None:
    """条件 2（关 ⑤）里 `unknown` 直接可数——这是「若没有 ⑤ 会带 unknown 的题占多少」。"""

    records = [
        _fivefold_record(
            "A01",
            points=["p1", "p2"],
            statuses=["unknown", "unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
        _fivefold_record(
            "A02", points=["q1"], statuses=["unknown"], gaps=[], fivefold=False, model_calls=3
        ),
        _fivefold_record(
            "A03", points=["r1"], statuses=["none"], gaps=["A1"], fivefold=False, model_calls=3
        ),
    ]

    reading = ev.unknown_frequency(records)

    assert reading["cases"] == 3
    assert reading["cases_with_unknown"] == 2
    assert reading["rate"] == round(2 / 3, 4)
    assert reading["unknown_points"] == 3
    assert reading["point_total"] == 4
    assert reading["count_distribution"] == {"0": 1, "1": 1, "2": 1}


def test_fivefold_comparison_splits_expose_from_rescue() -> None:
    """手算三题：A01 暴露、A02 救回、A03 两条同指一个方面时取最乐观的那条（也算救回）。"""

    records = [
        # A01「数字人民币」：关 ⑤ 时 unknown → 开 ⑤ 后 none（暴露成缺口）
        _fivefold_record(
            "A01",
            points=["数字人民币退款能否退回数字钱包"],
            statuses=["none"],
            gaps=["A1"],
            fivefold=True,
            insufficient=1,
        ),
        _fivefold_record(
            "A01",
            points=["数字人民币退款可退回原数字钱包"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
        # A02「区块链」：关 ⑤ 时 unknown → 开 ⑤ 后 supported（救回）
        _fivefold_record(
            "A02",
            points=["区块链存证可用于日志防篡改"],
            statuses=["supported"],
            gaps=[],
            fivefold=True,
        ),
        _fivefold_record(
            "A02",
            points=["区块链存证能否用于日志防篡改"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
        # A03「企业微信」：两条同指一个方面，一条 none 一条 supported → 取最乐观 = 救回
        _fivefold_record(
            "A03",
            points=["企业微信告警推送的配置方式", "企业微信推送是否需要权限"],
            statuses=["none", "supported"],
            gaps=["A1"],
            fivefold=True,
            insufficient=1,
        ),
        _fivefold_record(
            "A03",
            points=["企业微信推送告警是否支持"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
    ]

    comparison = ev.fivefold_comparison(records)

    assert comparison["paired_cases"] == 3
    assert comparison["transitions"]["unknown_to_none"] == 1
    assert comparison["transitions"]["unknown_to_supported"] == 2
    assert comparison["transitions"]["total"] == 3
    assert comparison["transitions"]["to_none_rate"] == round(1 / 3, 4)
    # 净效果：缺口从 0 变 2（A01 暴露出来的那条 + A03 原有的那条）
    assert comparison["net"]["gaps_with"] == 2
    assert comparison["net"]["gaps_without"] == 0
    assert comparison["net"]["gaps_up"] == 2
    assert comparison["without_fivefold"]["cases_with_unknown"] == 3


def test_fivefold_comparison_aligns_by_keyword_not_by_point_text() -> None:
    """两次规划的措辞必然不同（实测 10/10 题），所以对齐只能靠**关键词**。

    对不上的两种情况分开记：开 ⑤ 那次的规划里没有这个方面（``not_planned``），
    以及 `unknown` 与不可答方面无关（``other_unknown``）——两者都不进改判分布。
    """

    records = [
        _fivefold_record(
            "A01",
            points=["退款还能用数字人民币吗"],
            statuses=["none"],
            gaps=["A1"],
            fivefold=True,
        ),
        _fivefold_record(
            "A01",
            points=["数字人民币退款可退回数字钱包"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
        # A02：开 ⑤ 那次的规划里完全没有「区块链」这条要点
        _fivefold_record(
            "A02",
            points=["审计日志留存期限"],
            statuses=["supported"],
            gaps=[],
            fivefold=True,
        ),
        _fivefold_record(
            "A02",
            points=["区块链存证能否防止篡改"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
        # A03：关 ⑤ 时的 unknown 与不可答方面（企业微信）无关
        _fivefold_record(
            "A03",
            points=["P2 告警响应时限"],
            statuses=["none"],
            gaps=["A1"],
            fivefold=True,
        ),
        _fivefold_record(
            "A03",
            points=["告警分级的具体划分标准"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            model_calls=3,
        ),
    ]

    comparison = ev.fivefold_comparison(records)

    assert comparison["transitions"]["unknown_to_none"] == 1
    assert comparison["transitions"]["not_planned_in_fivefold_run"] == 1
    assert comparison["transitions"]["other_unknown_points"] == 1
    assert comparison["transitions"]["total"] == 1
    assert comparison["transitions"]["plan_differs_cases"] == ["A01", "A02"]


def test_fivefold_comparison_refuses_to_fabricate_a_rate_without_pairs() -> None:
    comparison = ev.fivefold_comparison([])

    assert comparison["paired_cases"] == 0
    assert comparison["transitions"]["to_none_rate"] is None


def test_summarize_ignores_the_control_condition() -> None:
    """五个指标只算主条件；对照条件的读数不许混进「缺口出现率」的分母。"""

    records = [
        _fivefold_record("A01", points=["p1"], statuses=["none"], gaps=["A1"], fivefold=True),
        _fivefold_record(
            "A01",
            points=["p1"],
            statuses=["unknown"],
            gaps=[],
            fivefold=False,
            answer=None,
            model_calls=3,
        ),
    ]

    metrics = ev.summarize(records)

    assert metrics["cases"] == 1
    assert metrics["缺口出现率"]["denominator"] == 1


def test_control_run_disables_rejudge_only_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """关 ⑤ 是**评测进程内**的类属性替换，跑完必须还原（不然下一题会静默跑错条件）。"""

    import asyncio

    from app.services import research as research_service

    seen: dict[str, Any] = {}
    original = research_service._Run._rejudge

    async def fake_run_case(case: ev.EvalCase, *, kb_scope: Any, fivefold: bool = True):
        seen["rejudge"] = research_service._Run._rejudge
        seen["fivefold"] = fivefold
        return {"case": case.case_id}

    monkeypatch.setattr(ev, "run_case", fake_run_case)

    record = asyncio.run(ev._run_without_rejudge(ev.load_cases()[0], kb_scope=[]))

    assert record["case"] == "A01"
    assert seen["fivefold"] is False
    assert seen["rejudge"] is not original, "跑的时候必须是关掉的那一份"
    assert asyncio.run(seen["rejudge"](None)) is None, "替换物必须是直接返回的 no-op"
    assert research_service._Run._rejudge is original, "跑完必须还原"


# ─────────────────────────────────────────────────────────────── 5. 预算与空跑


def test_model_call_estimate_stays_under_the_cap() -> None:
    predicted = ev.predict_model_calls()

    assert predicted["budget"] == ev.MODEL_CALL_CAP == 170
    assert predicted["nominal"] == (
        ev.CASE_COUNT * ev.NOMINAL_CALLS_PER_CASE + 10 * ev.NOMINAL_CALLS_PER_CONTROL_CASE
    )
    assert predicted["worst"] == (
        ev.CASE_COUNT * ev.WORST_CALLS_PER_CASE + 10 * ev.WORST_CALLS_PER_CONTROL_CASE
    )
    assert predicted["nominal"] <= predicted["budget"]
    assert predicted["worst"] <= predicted["budget"], "最坏也不许超跑——超了就减题"
    assert predicted["conditions"]["main"]["cases"] == 20
    assert predicted["conditions"]["control"]["cases"] == 10


def test_worst_case_per_case_matches_the_production_constant() -> None:
    """第 13 步删掉强模型兜底位：最坏就是生产常量 ``MAX_MODEL_CALLS``（4 次/题）。关 ⑤ 少一格。"""

    from app.services import research_limits

    assert ev.WORST_CALLS_PER_CASE == research_limits.MAX_MODEL_CALLS
    assert ev.NOMINAL_CALLS_PER_CASE == research_limits.MAX_MODEL_CALLS - 1
    assert ev.WORST_CALLS_PER_CONTROL_CASE == research_limits.MAX_MODEL_CALLS - 1
    assert ev.NOMINAL_CALLS_PER_CONTROL_CASE == research_limits.MAX_MODEL_CALLS - 2


def test_plan_reports_the_budget_and_the_case_list_without_any_model_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = ev.build_plan()

    assert payload["model_calls"]["actual"] == 0
    assert payload["checks"]["base_ids_ok"] is True
    assert len(payload["cases"]) == ev.CASE_COUNT

    text = ev.render_plan(payload)
    assert "名义" in text and "最坏" in text
    assert "A01" in text and "B01" in text


# ─────────────────────────────────────────────────────────────── 6. 报告结构


def test_report_renders_every_required_section_even_without_data() -> None:
    payload = {
        "generated_at": "2026-11-15T00:00:00+00:00",
        "llm_chain": {"llm_model": "m", "llm_endpoint_source": "model_configs"},
        "model_calls": {"nominal": 110, "worst": 140, "actual": 0, "budget": 170},
        "records": [],
        "evidences": [],
        "metrics": ev.summarize([]),
        "fivefold": ev.fivefold_comparison([]),
    }

    text = ev.render_report(payload)

    for heading in (
        "## 一、五个指标",
        "## 二、三个问题",
        "## 三、⑤ 的对照",
        "## 四、题集与机械证据",
        "## 五、逐题明细",
        "## 六、这份对照不能回答什么",
        "## 七、怎么复现",
    ):
        assert heading in text
    assert "文本级" in text
    assert "37.5%" in text
    assert "暴露" in text and "救回" in text


def test_report_states_that_the_residual_unknown_rate_measures_nothing() -> None:
    """这个口径在 ⑤ 上线后必然为 0——报告必须自己说出来，否则读者会把它当成好消息。"""

    payload = {
        "generated_at": "2026-11-15T00:00:00+00:00",
        "model_calls": {"nominal": 110, "worst": 140, "actual": 0, "budget": 170},
        "records": [],
        "evidences": [],
        "metrics": ev.summarize([]),
        "fivefold": ev.fivefold_comparison([]),
    }

    text = ev.render_report(payload)

    assert "恒为 0" in text
    assert "量不到东西" in text
