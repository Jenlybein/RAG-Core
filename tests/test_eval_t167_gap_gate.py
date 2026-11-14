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
    """手算一组：缺口 3 配声明 1（欠）、3 配 3（齐）、2 配 4（过）、以及一题没缺口。"""

    records = [
        {"gaps": ["A1", "A2", "A3"], "insufficient": 1},
        {"gaps": ["A1", "A2", "A3"], "insufficient": 3},
        {"gaps": ["A1", "A2"], "insufficient": 4},
        {"gaps": [], "insufficient": 1},
    ]

    reading = ev.declaration_alignment(records)

    assert reading["cases_with_gaps"] == 3
    assert reading["pairs"] == {"3→1": 1, "3→3": 1, "2→4": 1}
    assert reading["exact"] == 1
    assert reading["under"] == 1
    assert reading["over"] == 1
    assert reading["under_rate"] == round(1 / 3, 4)
    assert reading["exact_rate"] == round(1 / 3, 4)
    # 没缺口却声明了不足：另一侧的过度声明，单独记，不混进上面的对齐度
    assert reading["declared_without_gaps"] == 1


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
    assert metrics["声明对齐度"]["under"] == 2
    assert metrics["声明对齐度"]["cases_with_gaps"] == 4


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


# ─────────────────────────────────────────────────────────────── 4. 预算与空跑


def test_model_call_estimate_stays_under_the_cap() -> None:
    predicted = ev.predict_model_calls()

    assert predicted["budget"] == ev.MODEL_CALL_CAP == 110
    assert predicted["nominal"] == ev.CASE_COUNT * ev.NOMINAL_CALLS_PER_CASE
    assert predicted["worst"] == ev.CASE_COUNT * ev.WORST_CALLS_PER_CASE
    assert predicted["nominal"] <= predicted["budget"]
    assert predicted["worst"] <= predicted["budget"], "最坏也不许超跑——超了就减题"


def test_worst_case_per_case_matches_the_production_constant() -> None:
    """最坏 5 次/题是生产常量（含强模型兜底位），不是这里手编的数。"""

    from app.services import research_limits

    assert ev.WORST_CALLS_PER_CASE == research_limits.MAX_MODEL_CALLS_WITH_FALLBACK
    assert ev.NOMINAL_CALLS_PER_CASE == research_limits.MAX_MODEL_CALLS


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


# ─────────────────────────────────────────────────────────────── 5. 报告结构


def test_report_renders_every_required_section_even_without_data() -> None:
    payload = {
        "generated_at": "2026-11-15T00:00:00+00:00",
        "llm_chain": {"llm_model": "m", "llm_endpoint_source": "model_configs"},
        "model_calls": {"nominal": 80, "worst": 100, "actual": 0, "budget": 110},
        "records": [],
        "evidences": [],
        "metrics": ev.summarize([]),
    }

    text = ev.render_report(payload)

    for heading in (
        "## 一、五个指标",
        "## 二、三个问题",
        "## 三、题集与机械证据",
        "## 四、逐题明细",
        "## 五、这份对照不能回答什么",
        "## 六、怎么复现",
    ):
        assert heading in text
    assert "文本级" in text
    assert "37.5%" in text
