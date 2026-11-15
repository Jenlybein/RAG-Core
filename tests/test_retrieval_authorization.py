"""第 13 步 · T1（expand）：新授权/计量词表、四档套餐种子、参数组合别名与数据搬迁。

这一票是 **expand**：新形式**加在旁边**，旧的 ``PLAN_CODES`` / ``PLAN_PRESETS`` /
``PlanSpec`` / ``KeyPolicySnapshot`` 一个都不碰。所以本文件只测新模块自己的外部行为，
不碰任何运行时判定——真相源切换留给 T8 contract。

四组断言都拿**独立真值**（spec 里逐项拍过板的数字与映射）比对，不用被测模块自己的常量
反推，避免「断言就是实现的复述」。
"""

from __future__ import annotations

import pytest

from app.services.retrieval_authorization import (
    CAPABILITIES,
    METER_BOOKS,
    METER_LEVELS,
    METER_WINDOWS,
    PARAM_PRESETS,
    TIER_SEEDS,
    RetrievalParams,
    meter_column_names,
    migrate_legacy_params,
)


# ───────────────────────────────────────────────────────────────── 词表与维度


def test_capability_vocabulary_is_the_five_endpoint_level_bits() -> None:
    """五个授权位：retrieve 是基线，其余四条对应四条加工端点与重排。"""

    assert set(CAPABILITIES) == {
        "retrieve",
        "rewrite",
        "decompose",
        "research",
        "rerank",
    }


def test_meter_dimensions_are_three_books_by_two_levels_by_two_windows() -> None:
    """三本账 × 两格 = 6 个列名；两级是**两张表各存一份同名列**，故共 12 个计数器。

    两级（Key / 租户）不是 12 个不同列名：套餐表与密钥配置表各自有 ``retrieve_qps``
    等六列，列名相同、层级由所在表区分（ADR-0018 两级配额的既有形状）。
    """

    assert set(METER_BOOKS) == {"retrieve", "rerank", "llm"}
    assert set(METER_LEVELS) == {"key", "tenant"}
    assert set(METER_WINDOWS) == {"qps", "daily"}
    assert len(meter_column_names()) == 6
    assert len(set(meter_column_names())) == 6
    # 12 = 6 个列名 × 2 张表（两级）。
    assert len(meter_column_names()) * len(METER_LEVELS) == 12


def test_meter_column_names_are_book_first_then_window() -> None:
    """列名形状是「账本_窗口」，与既有 retrieve_qps / retrieve_daily 同形。"""

    assert "retrieve_qps" in meter_column_names()
    assert "retrieve_daily" in meter_column_names()
    assert "rerank_qps" in meter_column_names()
    assert "llm_daily" in meter_column_names()


# ─────────────────────────────────────────────────────────── 四档套餐种子


def test_there_are_exactly_four_tiers_named_free_plus_pro_max() -> None:
    assert set(TIER_SEEDS) == {"free", "plus", "pro", "max"}


def test_free_grants_only_the_baseline_retrieve_bit_and_no_rerank_or_llm_quota() -> None:
    """free 只允许检索（无 rerank）：另外两位额度为 0，授权位只有 retrieve。"""

    free = TIER_SEEDS["free"]
    assert free.capabilities == frozenset({"retrieve"})
    assert (free.rerank_qps, free.rerank_daily) == (0, 0)
    assert (free.llm_qps, free.llm_daily) == (0, 0)
    assert free.max_top_k == 5


def test_plus_is_the_baseline_and_pro_and_max_are_five_and_twenty_times_it() -> None:
    """plus = 1x、pro = 5x、max = 20x；三本账都按同一倍数放大。"""

    plus, pro, maximum = TIER_SEEDS["plus"], TIER_SEEDS["pro"], TIER_SEEDS["max"]
    assert (plus.retrieve_qps, plus.retrieve_daily) == (10, 5000)
    for attribute in (
        "retrieve_qps",
        "retrieve_daily",
        "rerank_qps",
        "rerank_daily",
        "llm_qps",
        "llm_daily",
    ):
        base = getattr(plus, attribute)
        assert getattr(pro, attribute) == base * 5, attribute
        assert getattr(maximum, attribute) == base * 20, attribute


def test_max_top_k_is_editable_per_tier() -> None:
    """free 5 / plus 10 / pro 15 / max 20 —— 档位白名单退役后唯一的质量封顶。"""

    assert [TIER_SEEDS[code].max_top_k for code in ("free", "plus", "pro", "max")] == [
        5,
        10,
        15,
        20,
    ]


def test_plus_pro_max_grant_all_five_bits() -> None:
    for code in ("plus", "pro", "max"):
        assert TIER_SEEDS[code].capabilities == frozenset(CAPABILITIES), code


# ───────────────────────────────────────────────────── 参数组合别名与搬迁


def test_the_three_legacy_profile_names_are_parameter_aliases_only() -> None:
    """speed / balanced / quality 只是三项参数的组合别名，不再是后端档位。"""

    assert PARAM_PRESETS["speed"] == RetrievalParams(mode="vector", top_k=5, rerank=False)
    assert PARAM_PRESETS["balanced"] == RetrievalParams(mode="hybrid", top_k=5, rerank=False)
    assert PARAM_PRESETS["quality"] == RetrievalParams(mode="hybrid", top_k=5, rerank=True)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("speed", RetrievalParams(mode="vector", top_k=5, rerank=False)),
        ("balanced", RetrievalParams(mode="hybrid", top_k=5, rerank=False)),
        ("quality", RetrievalParams(mode="hybrid", top_k=5, rerank=True)),
    ],
)
def test_named_profiles_expand_into_the_three_parameters(
    profile: str, expected: RetrievalParams
) -> None:
    """旧 profile 按预设展开；custom_options 一个都不读（完整替换）。"""

    result = migrate_legacy_params(
        profile=profile,
        custom_options={"mode": "vector", "top_k": 99, "rerank": False},
        defaults=RetrievalParams(mode="bm25", top_k=3, rerank=False),
    )
    assert result == expected


def test_custom_profile_overrides_each_key_and_drops_the_rewrite_key() -> None:
    """custom 逐键覆盖部署缺省；rewrite 键丢弃（改写已是端点，不是参数）。"""

    result = migrate_legacy_params(
        profile="custom",
        custom_options={"mode": "hybrid", "top_k": 10, "rerank": True, "rewrite": True},
        defaults=RetrievalParams(mode="vector", top_k=5, rerank=False),
    )
    assert result == RetrievalParams(mode="hybrid", top_k=10, rerank=True)


def test_custom_profile_with_missing_keys_falls_back_per_key() -> None:
    result = migrate_legacy_params(
        profile="custom",
        custom_options={"rerank": True},
        defaults=RetrievalParams(mode="bm25", top_k=7, rerank=False),
    )
    assert result == RetrievalParams(mode="bm25", top_k=7, rerank=True)


def test_unknown_or_missing_profile_falls_back_to_deployment_defaults() -> None:
    defaults = RetrievalParams(mode="vector", top_k=5, rerank=False)
    for profile in (None, "platinum", "", "quality "):
        assert migrate_legacy_params(profile=profile, custom_options={}, defaults=defaults) == defaults


def test_non_boolean_and_out_of_range_manual_values_do_not_silently_apply() -> None:
    """字符串 "false"、负数 top_k、未知 mode 都不许悄悄生效（沿用旧 custom 的收敛口径）。"""

    defaults = RetrievalParams(mode="hybrid", top_k=5, rerank=False)
    result = migrate_legacy_params(
        profile="custom",
        custom_options={
            "mode": "not-a-mode",
            "top_k": -3,
            "rerank": "false",
        },
        defaults=defaults,
    )
    assert result == defaults
