"""评测打分的纯函数接缝：P@k / R@k / MRR / R@1 / exact_hit@1 / 分题型 / 陷阱对 / 误召。

**为什么把这些函数单独抽出来测**：评测脚本本身要连真 PostgreSQL + 真 ES + 真嵌入，
跑一次几十秒到几分钟，且有网络与容器依赖。但「分数怎么算」是纯算术——如果它算错了，
再大的语料也只会给出一个更可信的假数字。所以算分部分与检索链路解耦，走离线真值表：
期望值全部由手算得出（不是把实现再跑一遍），实现改了而语义没变时这些测试不会碎。

接缝只有三个公开入口：``aggregate`` / ``by_type`` / ``exact_hit_at_1``（外加
``trap_report`` / ``absent_report`` 两个报告函数）。断言只看这些入口的返回值。
"""

from __future__ import annotations

import pytest

from scripts.eval_metrics import (
    Case,
    absent_report,
    aggregate,
    by_type,
    exact_hit_at_1,
    first_relevant_rank,
    hit_at_1,
    precision_at,
    recall_at,
    reciprocal_rank,
    trap_report,
    winner,
)


def _case(
    case_id: str,
    *,
    relevant: tuple[str, ...],
    returned: tuple[str, ...],
    type_: str = "colloquial",
    trap: tuple[str, ...] = (),
) -> Case:
    return Case(
        id=case_id,
        type=type_,
        relevant=frozenset(relevant),
        returned=returned,
        trap=frozenset(trap),
    )


# ─────────────────────────────────────────────────────── 单题的四个基础量


def test_precision_counts_hits_over_actually_returned_rows() -> None:
    """P@5 的分母是**实际返回条数**，不是请求的 k。

    语料或降级让返回只剩 2 条时，按 5 算会把「少返回」伪装成「不精确」。
    """

    assert precision_at(("a", "b"), frozenset({"b"}), 5) == pytest.approx(0.5)


def test_recall_counts_relevant_found_in_prefix_over_all_relevant() -> None:
    # 2 条相关切片，前 5 条里只找到 a → R=0.5
    assert recall_at(("x", "a", "y", "z", "w"), frozenset({"a", "b"}), 5) == pytest.approx(0.5)
    # 前 1 条里没有相关 → R@1=0；第 2 条才有 → R@2=0.5
    assert recall_at(("x", "a"), frozenset({"a", "b"}), 1) == 0.0
    assert recall_at(("x", "a"), frozenset({"a", "b"}), 2) == pytest.approx(0.5)


def test_recall_never_double_counts_a_chunk_that_repeats() -> None:
    """同一片被两路召回成两行时只算一次：召回率是「相关切片找到几成」，不是行数。"""

    assert recall_at(("a", "a"), frozenset({"a", "b"}), 5) == pytest.approx(0.5)


def test_first_relevant_rank_and_reciprocal_rank() -> None:
    returned = ("x", "y", "a")
    assert first_relevant_rank(returned, frozenset({"a"})) == 3
    assert reciprocal_rank(returned, frozenset({"a"})) == pytest.approx(1 / 3)
    assert reciprocal_rank(("x", "y"), frozenset({"a"})) == 0.0
    assert hit_at_1(("a", "x"), frozenset({"a"})) == 1.0
    # 第一条不是相关切片：MRR 可以是 1/2，但 hit@1 必须是 0
    assert hit_at_1(("x", "a"), frozenset({"a"})) == 0.0


# ─────────────────────────────────────────────────────────── 汇总与分题型


def test_aggregate_averages_only_answerable_cases() -> None:
    """``absent`` 题（relevant 为空）不进主平均：它测的是误召，不是召回。"""

    cases = [
        _case("q1", relevant=("a",), returned=("a", "b")),  # P=1/2 R=1 RR=1
        _case("q2", relevant=("b", "c"), returned=("x", "b")),  # P=1/2 R=1/2 RR=1/2
        _case("q3", relevant=(), returned=("z",)),  # 反向对照题，必须被排除
    ]
    result = aggregate(cases, k=5)
    assert result["count"] == 2
    assert result["precision_at_k"] == pytest.approx(0.5)
    assert result["recall_at_k"] == pytest.approx(0.75)
    assert result["mrr"] == pytest.approx(0.75)
    assert result["recall_at_1"] == pytest.approx(0.5)
    assert result["hit_at_1"] == pytest.approx(0.5)


def test_aggregate_on_empty_input_is_none_not_zero() -> None:
    """没有可答题时回 ``None``：0.0 会被当成「考了但全错」，那是两回事。"""

    result = aggregate([], k=5)
    assert result["count"] == 0
    assert result["precision_at_k"] is None
    assert result["recall_at_k"] is None
    assert result["mrr"] is None


def test_by_type_splits_and_keeps_types_independent() -> None:
    cases = [
        _case("c1", relevant=("a",), returned=("a",), type_="exact_code"),
        _case("c2", relevant=("b",), returned=("x", "b"), type_="exact_code"),
        _case("m1", relevant=("c",), returned=("c",), type_="model_number"),
    ]
    blocks = by_type(cases, k=5)
    assert set(blocks) == {"exact_code", "model_number"}
    assert blocks["exact_code"]["count"] == 2
    assert blocks["exact_code"]["hit_at_1"] == pytest.approx(0.5)
    assert blocks["exact_code"]["recall_at_k"] == pytest.approx(1.0)
    assert blocks["model_number"]["hit_at_1"] == pytest.approx(1.0)


def test_exact_hit_at_1_only_counts_identifier_questions() -> None:
    """编号精确性只对编号类题有意义：返回第一条是不是被问的那个切片。

    问 E1025 却把 E1024 排第一，算不中——这正是 v1 测不出来的东西。
    """

    cases = [
        _case("e1", relevant=("e1025",), returned=("e1025",), type_="exact_code"),
        _case("e2", relevant=("e1024",), returned=("e1025", "e1024"), type_="exact_code"),
        _case("r1", relevant=("prj",), returned=("prj",), type_="rare_token"),
        _case("m1", relevant=("xr",), returned=("xr",), type_="model_number"),
    ]
    result = exact_hit_at_1(cases)
    assert result["count"] == 3
    assert result["hits"] == 2
    assert result["rate"] == pytest.approx(2 / 3)


def test_exact_hit_at_1_with_no_identifier_questions_is_none() -> None:
    assert exact_hit_at_1([_case("m1", relevant=("xr",), returned=("xr",))]) == {
        "count": 0,
        "hits": 0,
        "rate": None,
    }


# ─────────────────────────────────────────────────────── 陷阱对与误召报告


def test_trap_report_shows_the_twin_rank_even_when_the_right_one_wins() -> None:
    cases = [
        _case(
            "q02",
            relevant=("e1025",),
            returned=("e1025", "e1024", "x"),
            type_="exact_code",
            trap=("e1024",),
        ),
        _case("q01", relevant=("e1024",), returned=("e1024",), trap=("e1025",)),
    ]
    report = trap_report(cases)
    assert report[0]["id"] == "q02"
    assert report[0]["twin_rank"] == 2
    assert report[0]["twin_returned"] is True
    assert report[0]["correct_at_1"] is True
    # 没进前 k 条时 twin_rank 必须是 None，而不是 0 或 99
    assert (
        trap_report([_case("q9", relevant=("a",), returned=("a",), trap=("b",))])[0]["twin_rank"]
        is None
    )


def test_absent_report_measures_false_positives_not_recall() -> None:
    cases = [
        _case("a1", relevant=(), returned=("x", "y")),
        _case("a2", relevant=(), returned=()),
    ]
    report = absent_report(cases)
    assert report["count"] == 2
    assert report["returned_total"] == 2
    assert report["returned_avg"] == pytest.approx(1.0)
    assert report["false_positive_rate"] == pytest.approx(0.5)


def test_absent_report_on_empty_input_is_none() -> None:
    report = absent_report([])
    assert report["count"] == 0
    assert report["false_positive_rate"] is None


# ─────────────────────────────────────────────────────────────── 题型赢家


def test_winner_orders_by_recall_then_mrr_then_precision() -> None:
    vector = {"recall_at_k": 0.8, "mrr": 0.9, "precision_at_k": 0.5}
    hybrid = {"recall_at_k": 0.9, "mrr": 0.4, "precision_at_k": 0.5}
    assert winner({"vector": vector, "hybrid": hybrid}) == "hybrid"

    # 召回打平时看 MRR
    vector2 = {"recall_at_k": 0.8, "mrr": 0.9, "precision_at_k": 0.5}
    bm25 = {"recall_at_k": 0.8, "mrr": 0.6, "precision_at_k": 0.5}
    assert winner({"vector": vector2, "bm25": bm25}) == "vector"

    # 三项全等才是打平
    assert winner({"vector": dict(vector2), "bm25": dict(vector2)}) == "打平"


def test_winner_keeps_a_tie_at_the_top_even_if_third_place_is_worse() -> None:
    """并列第一就是打平：不能因为第三名更差，就把「第一个」当成唯一赢家。"""

    scores = {
        "vector": {"recall_at_k": 1.0, "mrr": 1.0, "precision_at_k": 0.2},
        "hybrid": {"recall_at_k": 1.0, "mrr": 1.0, "precision_at_k": 0.2},
        "bm25": {"recall_at_k": 1.0, "mrr": 0.8333, "precision_at_k": 0.2},
    }
    assert winner(scores) == "打平"


def test_winner_treats_none_as_losing() -> None:
    scores = {"vector": None, "bm25": {"recall_at_k": 0.0, "mrr": 0.0, "precision_at_k": 0.0}}
    assert winner(scores) == "bm25"
