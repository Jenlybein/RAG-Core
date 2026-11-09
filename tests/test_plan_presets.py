"""票 #74：套餐三档的常量 ↔ 行映射对账。

套餐定义进库之后，**库里那张表才是真相**，``PLAN_PRESETS`` 只是首次启动时的种子。
所以这里守两条：

1. 种子本身符合票面（下面那份字面量真值表）；
2. 「播种用的行 → 映射回上下文」必须逐列等于常量。**漏一列就算失败**——这一列在
   面板上改得动、后端读不到，正是「套餐进库」要消灭的那种病。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.models.plan import (
    PLAN_BUSINESS_COLUMNS,
    PLAN_CODES,
    PLAN_PRESETS,
    PlanSpec,
    plan_row_of,
    plan_spec_of,
    preset_mismatches,
)

# 票面真值表：11 个业务列 × 三档。这份字面量是**独立来源**，刻意不从常量里推导——
# 否则「常量等于它自己」的同义反复什么都守不住。name 是展示名，票面没定字面值，
# 另有用例只要求非空且互不相同。
SPEC_TABLE: dict[str, dict[str, Any]] = {
    "free": {
        "allowed_profiles": ("speed",),
        "hybrid_allowed": False,
        "rerank_allowed": False,
        "query_rewrite_allowed": False,
        "research_allowed": False,
        "retrieve_qps": 3,
        "retrieve_daily": 500,
        "max_kb": 1,
        "max_kb_per_retrieve": 1,
        "max_documents_per_kb": 30,
        "max_processing_documents": 1,
    },
    "standard": {
        "allowed_profiles": ("speed", "balanced", "custom"),
        "hybrid_allowed": True,
        "rerank_allowed": False,
        "query_rewrite_allowed": False,
        "research_allowed": False,
        "retrieve_qps": 10,
        "retrieve_daily": 5000,
        "max_kb": 5,
        "max_kb_per_retrieve": 3,
        "max_documents_per_kb": 200,
        "max_processing_documents": 2,
    },
    "pro": {
        "allowed_profiles": ("speed", "balanced", "quality", "custom"),
        "hybrid_allowed": True,
        "rerank_allowed": True,
        "query_rewrite_allowed": True,
        "research_allowed": True,
        "retrieve_qps": 50,
        "retrieve_daily": 100000,
        "max_kb": 50,
        "max_kb_per_retrieve": 5,
        "max_documents_per_kb": 5000,
        "max_processing_documents": 10,
    },
}


def _tampered_value(column: str, current: Any) -> Any:
    """给某一列换一个**仍然合法**但不同的值：合法才不会在构造行时先崩掉。"""

    if column == "allowed_profiles":
        return ("speed", "quality", "custom", "balanced")
    if isinstance(current, bool):
        return not current
    return int(current) + 1


# ──────────────────────────────────────────────────────────── 种子本身符合票面


def test_the_three_plans_are_exactly_the_decided_codes() -> None:
    assert PLAN_CODES == ("free", "standard", "pro")
    assert set(PLAN_PRESETS) == set(PLAN_CODES)


@pytest.mark.parametrize("code", PLAN_CODES)
def test_each_plan_carries_the_decided_values(code: str) -> None:
    """逐列比对票面：11 个业务列一个不漏。"""

    spec = PLAN_PRESETS[code]
    assert len(PLAN_BUSINESS_COLUMNS) == 11
    for column in PLAN_BUSINESS_COLUMNS:
        assert getattr(spec, column) == SPEC_TABLE[code][column], f"{code}.{column}"


def test_plan_display_names_are_present_and_distinct() -> None:
    names = [PLAN_PRESETS[code].name for code in PLAN_CODES]
    assert all(names), "展示名不能为空：面板上要显示它"
    assert len(set(names)) == len(names)


# ────────────────────────────────────────────────────────────── 行 ↔ 上下文映射


def test_the_spec_fields_are_exactly_the_columns() -> None:
    """映射对象是「列的镜像」：字段名与列名一一对应，改名就两边一起改。"""

    field_names = [field.name for field in dataclasses.fields(PlanSpec)]
    assert field_names == ["code", "name", *PLAN_BUSINESS_COLUMNS]


@pytest.mark.parametrize("code", PLAN_CODES)
def test_mapping_a_seeded_row_back_yields_the_constant(code: str) -> None:
    """播种用怎么建行，回来就怎么映射：这是「表即真相」的往返。"""

    row = plan_row_of(PLAN_PRESETS[code])

    assert plan_spec_of(row) == PLAN_PRESETS[code]


def test_reconciliation_passes_after_seeding_all_three_plans() -> None:
    rows = [plan_row_of(PLAN_PRESETS[code]) for code in PLAN_CODES]

    assert preset_mismatches(rows) == []


def test_a_mapper_that_forgets_a_column_cannot_even_construct_the_spec() -> None:
    """构造式兜底：字段没有默认值，漏传一列就是 TypeError，而不是静默变成 None。"""

    complete = {
        field.name: getattr(PLAN_PRESETS["pro"], field.name)
        for field in dataclasses.fields(PlanSpec)
    }
    for column in PLAN_BUSINESS_COLUMNS:
        partial = {key: value for key, value in complete.items() if key != column}
        with pytest.raises(TypeError):
            PlanSpec(**partial)


# ──────────────────────────────── 对账必须逐列生效：丢一列就得失败，不能静默通过


@pytest.mark.parametrize("column", PLAN_BUSINESS_COLUMNS)
def test_dropping_any_single_business_column_fails_reconciliation(column: str) -> None:
    """把某一列改成「播种时漏掉」的样子（值不是常量里的那个），对账必须点名那一列。

    这条用例是「漏一列即失败」的可执行定义：它逐列把 11 个业务列各试一遍，
    只要对账逻辑少覆盖任何一列，对应的那条用例就会拿到空列表而失败。
    """

    rows = [plan_row_of(PLAN_PRESETS[code]) for code in PLAN_CODES]
    free_row = rows[0]
    assert free_row.code == "free"
    setattr(free_row, column, _tampered_value(column, getattr(free_row, column)))

    # 只有这一列不同，对账就只报这一列：报得出名字才算「逐列」。
    assert preset_mismatches(rows) == [f"free.{column}"]


def test_a_dropped_name_is_reported_too() -> None:
    rows = [plan_row_of(PLAN_PRESETS[code]) for code in PLAN_CODES]
    rows[0].name = "别的名字"

    assert preset_mismatches(rows) == ["free.name"]


def test_two_rows_with_the_same_code_are_reported() -> None:
    """手工修库修出两行同码时不能装作没看见（唯一索引之外的那道兜底）。"""

    rows = [plan_row_of(PLAN_PRESETS[code]) for code in PLAN_CODES]
    rows.append(plan_row_of(PLAN_PRESETS["free"]))

    assert preset_mismatches(rows) == ["free: 库里有多档同码"]


def test_a_plan_that_was_never_seeded_is_reported() -> None:
    """表里少一档也要报：否则最高档被误删之后，谁都不会发现。"""

    rows = [plan_row_of(PLAN_PRESETS[code]) for code in ("free", "standard")]

    assert preset_mismatches(rows) == ["pro: 库里没有这一档"]


def test_a_row_whose_code_is_not_a_known_plan_is_reported() -> None:
    rows = [plan_row_of(PLAN_PRESETS[code]) for code in PLAN_CODES]
    extra = plan_row_of(PLAN_PRESETS["free"])
    extra.code = "platinum"
    rows.append(extra)

    assert preset_mismatches(rows) == ["platinum: 未知档位（不在 PLAN_PRESETS 里）"]
