"""第 13 步 · T5：`/rag/decompose` 的合并规则（纯函数缝）。

分解检索把 N 条子查询各自的内核结果合成**一份**列表。规则来自 spec：

* 按交付单元身份去重（优先 ``section_id``，没有时退到 ``chunk_id``）；
* **每条子问题保底 1 条**：一个子问题的唯一命中不许被另一个子问题的更强势命中挤掉；
* 剩余名额按子问题顺序**轮转**填满 ``top_k``；
* 每条结果带 ``matched_queries``（哪几条子问题把它顶上来的）；
* 跨子问题的 ``score`` 在契约上不可比，合并函数不重算分数，只保留代表片那条的分列字段。

真值来自 worked example（手工排好的输入与期望顺序），不从被测函数反推。
"""

from __future__ import annotations

import pytest

from app.services.decompose import merge_sub_query_results


def _row(key: str, *, score: float = 0.9, section: str | None = None) -> dict:
    return {
        "document_id": f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "kb_id": "kb-a",
        "title": "退款政策",
        "content": f"{key} 的正文。",
        "score": score,
        "section_id": section if section is not None else f"section-{key}",
        "heading_path": f"退款 > {key}",
        "vector_score": score,
        "vector_rank": 1,
    }


def test_each_sub_query_gets_a_guaranteed_slot_even_when_one_is_dominant() -> None:
    """A 有 3 条高分命中、B 只有 1 条低分命中：B 不许被挤光。"""

    a = [_row(f"a{i}", score=0.99 - i * 0.01) for i in range(3)]
    b = [_row("b0", score=0.10)]

    merged = merge_sub_query_results([("查询A", a), ("查询B", b)], top_k=4)

    keys = [row["chunk_id"] for row in merged]
    assert "chunk-b0" in keys, "B 的唯一命中必须保住一个名额"
    assert len(merged) == 4


def test_remaining_slots_are_filled_by_round_robin() -> None:
    """两份各 3 条、top_k=5：先各保底 1 条，再按 A→B→A→B 轮转补齐。"""

    a = [_row(f"a{i}", score=0.9 - i * 0.01) for i in range(3)]
    b = [_row(f"b{i}", score=0.7 - i * 0.01) for i in range(3)]

    merged = merge_sub_query_results([("查询A", a), ("查询B", b)], top_k=5)

    # 保底：A 的 a0、B 的 b0；轮转补：a1 / b1 / a2（第 5 个名额）。
    assert [row["chunk_id"] for row in merged] == [
        "chunk-a0",
        "chunk-b0",
        "chunk-a1",
        "chunk-b1",
        "chunk-a2",
    ]


def test_duplicate_units_are_delivered_once_and_record_every_matched_query() -> None:
    """同一节被两条子查询命中 → 只交付一次，``matched_queries`` 记下两条。"""

    shared = dict(_row("shared"))
    only_b = _row("b")

    merged = merge_sub_query_results(
        [("查询A", [shared]), ("查询B", [dict(shared), only_b])], top_k=5
    )

    assert [row["chunk_id"] for row in merged] == ["chunk-shared", "chunk-b"]
    assert merged[0]["matched_queries"] == ["查询A", "查询B"]
    assert merged[1]["matched_queries"] == ["查询B"]


def test_top_k_is_never_exceeded_and_no_invented_rows() -> None:
    """两条子查询各 3 条、top_k=2 → 恰好 2 条；不够时不补空行。"""

    a = [_row(f"a{i}") for i in range(3)]
    b = [_row(f"b{i}") for i in range(3)]

    assert len(merge_sub_query_results([("查询A", a), ("查询B", b)], top_k=2)) == 2
    assert len(merge_sub_query_results([("查询A", a), ("查询B", b)], top_k=99)) == 6
    assert merge_sub_query_results([("查询A", [])], top_k=5) == []


def test_a_single_sub_query_keeps_its_own_order() -> None:
    """N=1 时退化为「保序截断 top_k」，与 /rag/retrieve 的结果顺序一致。"""

    rows = [_row(f"a{i}", score=0.9 - i * 0.01) for i in range(4)]

    merged = merge_sub_query_results([("查询A", rows)], top_k=3)

    assert [row["chunk_id"] for row in merged] == ["chunk-a0", "chunk-a1", "chunk-a2"]


def test_a_row_without_an_identity_is_dropped_rather_than_guessed() -> None:
    """没有 section_id 也没有 chunk_id 的行一律丢弃（沿用 Research 的 material_key 口径）。"""

    rows = [{"content": "没有身份"}, _row("ok")]

    merged = merge_sub_query_results([("查询A", rows)], top_k=5)

    assert [row["chunk_id"] for row in merged] == ["chunk-ok"]


@pytest.mark.parametrize("top_k", [0, -1])
def test_a_non_positive_top_k_yields_nothing(top_k: int) -> None:
    assert merge_sub_query_results([("查询A", [_row("a")])], top_k=top_k) == []


def test_a_duplicate_in_a_later_round_does_not_starve_the_remaining_rows() -> None:
    """轮转中途撞到重复行时，跳过它继续往后取，不许把后面可用的行饿死。"""

    shared = dict(_row("shared"))
    a = [shared, dict(shared), _row("a2"), _row("a3")]  # 第 2 条是重复的
    b = [_row("b0"), _row("b1")]

    merged = merge_sub_query_results([("查询A", a), ("查询B", b)], top_k=4)

    # 保底：A 的 shared、B 的 b0；轮转第 1 轮：A 的 rows[1] 是重复的（只推进 A 的
    # 指针、不占名额），B 的 rows[1]=b1 占位；第 2 轮：A 的 rows[2]=a2 占位。
    # 每条子查询各自一个指针，重复行不会把该流后面的行饿死。
    assert [row["chunk_id"] for row in merged] == [
        "chunk-shared",
        "chunk-b0",
        "chunk-b1",
        "chunk-a2",
    ]
