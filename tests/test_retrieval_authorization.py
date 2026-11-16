"""五个授权位 + 三本账的词表，以及四档种子的倍率口径（第 13 步 · ADR-0032）。

这个文件守的是**词表只有一处**这条纪律：能力名、列名映射、账本名都从
``app.services.retrieval_authorization`` / ``app.services.quota_gate`` 取，任何别处再写
一份字面量都算重复定义。四档的数值则从 ``PLAN_PRESETS`` 取——它是唯一真相源（迁移
``d7e3b8a1f642`` 写的就是同一组数，由 ``test_four_tier_plan_migration.py`` 对账）。
"""

from __future__ import annotations

import pytest

from app.models.plan import PLAN_CODES, PLAN_PRESETS
from app.services.quota_gate import METER_BOOKS, METER_LEVELS, METER_WINDOWS
from app.services.retrieval_authorization import (
    CAPABILITIES,
    CAPABILITY_COLUMNS,
    ENDPOINT_CAPABILITY,
)


def test_the_five_authorization_bits_are_the_decided_names() -> None:
    """授权位是五个、名字固定、顺序固定（控制面与种子的列顺序都读这一处）。"""

    assert CAPABILITIES == ("retrieve", "rewrite", "decompose", "research", "rerank")
    assert len(set(CAPABILITIES)) == 5


def test_every_capability_has_exactly_one_column_and_no_second_table() -> None:
    """能力名 → 列名只有一份映射，且列名是 ``<能力>_allowed``。

    ``KeyPolicySnapshot`` 与 ``PlanSpec`` 上这五列同名同义，所以一份映射就够——判定不再
    区分「问配置」还是「问套餐」。这条用例挡的是「有人又手写一遍列名」。
    """

    assert CAPABILITY_COLUMNS == {name: f"{name}_allowed" for name in CAPABILITIES}


def test_four_endpoints_map_to_four_bits_and_rerank_is_not_one() -> None:
    """四个端点各有一个位；``rerank`` **不是端点**，它是质量位。"""

    assert ENDPOINT_CAPABILITY == {
        "retrieve": "retrieve",
        "rewrite": "rewrite",
        "decompose": "decompose",
        "research": "research",
    }
    assert "rerank" not in ENDPOINT_CAPABILITY
    assert set(ENDPOINT_CAPABILITY.values()) <= set(CAPABILITIES)


def test_the_three_books_and_their_two_levels_and_windows() -> None:
    """三本账按**资源**开户：内核调用、重排调用、大模型调用；每本两级 × 两格。"""

    assert METER_BOOKS == ("retrieve", "rerank", "llm")
    assert METER_LEVELS == ("key", "tenant")
    assert METER_WINDOWS == ("qps", "daily")


def test_free_opens_only_the_baseline_bit_and_buys_no_processing_capacity() -> None:
    """免费档只给 ``retrieve`` 一个位；两本加工账的额度都是 0（= 没买）。"""

    free = PLAN_PRESETS["free"]
    assert free.retrieve_allowed is True
    assert [free.rewrite_allowed, free.decompose_allowed, free.research_allowed, free.rerank_allowed] == [
        False,
        False,
        False,
        False,
    ]
    assert (free.rerank_qps, free.rerank_daily, free.llm_qps, free.llm_daily) == (0, 0, 0, 0)


@pytest.mark.parametrize("column", ["retrieve_qps", "retrieve_daily", "rerank_qps", "rerank_daily", "llm_qps", "llm_daily"])
def test_pro_is_five_times_plus_and_max_is_twenty_times_plus(column: str) -> None:
    """倍率口径：plus 是基准，pro = 5×，max = 20×（逐列成立，不只是总量）。"""

    plus = getattr(PLAN_PRESETS["plus"], column)
    assert getattr(PLAN_PRESETS["pro"], column) == plus * 5, column
    assert getattr(PLAN_PRESETS["max"], column) == plus * 20, column


def test_the_four_tiers_are_ordered_by_max_top_k() -> None:
    """唯一的质量封顶随档位单调递增，且都落在平台硬上限之内。"""

    from app.services.retrieval_options import MAX_TOP_K

    caps = [PLAN_PRESETS[code].max_top_k for code in PLAN_CODES]
    assert caps == sorted(caps)
    assert len(set(caps)) == 4
    assert max(caps) <= MAX_TOP_K
