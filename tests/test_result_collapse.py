"""折叠纯函数的行为契约。

这层没有 IO，所以每个场景都能用手写数据钉死。改折叠规则时会立刻有一条测试红。

**单元由 `heading_path` 的前两段决定**（见 `unit_path_of`）——不是 `parent_section_id`。
后者是上一级，对 h2 级的切片会指到 h1，按它归组会把整篇文档并成一个单元。
"""

from __future__ import annotations

import pytest

from app.services.result_collapse import (
    EXPANSION_CHUNK,
    EXPANSION_NEIGHBORS,
    EXPANSION_PARENT,
    EXPANSION_TABLE,
    Hit,
    collapse,
    unit_id_of,
    unit_key,
    unit_path_of,
)

DOC = "doc-1"
HEAD_A = "指南 > 上架"
HEAD_B = "指南 > 供电"
HEAD_C = "手册 > 概览"
UNIT_A = unit_id_of(HEAD_A)
UNIT_B = unit_id_of(HEAD_B)
UNIT_C = unit_id_of(HEAD_C)


def _hit(
    chunk_id: str,
    score: float,
    *,
    chunk_type: str = "prose",
    heading: str = HEAD_A,
    section_id: str = "s" * 8,
    table_part: int = 0,
    content: str | None = None,
    document_id: str = DOC,
) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        document_id=document_id,
        score=score,
        content=content if content is not None else f"{chunk_id} 的正文。",
        chunk_type=chunk_type,
        section_id=section_id,
        parent_section_id="h1" * 4,
        table_part=table_part,
        heading_path=heading,
    )


def _rows(*triples: tuple[int, str, str]) -> tuple[tuple[int, str, str], ...]:
    return tuple(triples)


def test_three_pieces_of_one_unit_collapse_into_one_result() -> None:
    """同一个 h2 单元的三个片只占一个名额，代表片是分最高的那片。"""

    hits = [_hit("c-1", 0.9), _hit("c-2", 0.7), _hit("c-3", 0.5)]

    units = collapse(
        hits,
        sections={
            ("section", DOC, UNIT_A): _rows(
                (0, "第一片。", "c-1"),
            )
        },
    )

    assert len(units) == 1
    assert units[0].chunk_id == "c-1"
    assert units[0].score == 0.9
    assert units[0].section_id == UNIT_A
    assert units[0].source_chunk_ids == ("c-1", "c-2", "c-3")


def test_two_h3_sections_of_one_h2_share_one_unit() -> None:
    """同一个 h2 下的两个 h3 属于同一个交付单元——这是折叠的意义所在。

    用「整篇文档一个单元」或「每个 h3 一个单元」都不对：前者吞掉主题，
    后者等于没折。
    """

    hits = [
        _hit("a-1", 0.9, heading="指南 > 上架 > 机架款"),
        _hit("a-2", 0.8, heading="指南 > 上架 > 壁挂款"),
        _hit("b-1", 0.7, heading="指南 > 供电"),
    ]

    units = collapse(hits)

    assert [unit.section_id for unit in units] == [UNIT_A, UNIT_B]


def test_two_h2_sections_of_one_document_are_separate_units() -> None:
    """同一篇文档里的两个 h2 是**两个**单元，不能被并成一个。"""

    hits = [_hit("a-1", 0.9, heading=HEAD_A), _hit("b-1", 0.8, heading=HEAD_B)]

    units = collapse(hits)

    assert len(units) == 2


def test_deep_h3_and_its_h2_land_in_the_same_unit() -> None:
    """h3 很深的片与它所在 h2 直挂的片，归到同一个单元。"""

    hits = [
        _hit("deep", 0.9, heading="指南 > 上架 > 供电 > 空开"),
        _hit("flat", 0.8, heading="指南 > 上架"),
    ]

    assert unit_key(hits[0]) == unit_key(hits[1])


def test_multiple_pieces_of_one_unit_give_the_whole_unit() -> None:
    """同一单元命中 ≥2 片时给整节。"""

    rows = _rows((0, "第一片。", "c-1"), (1, "第二片。", "c-2"), (2, "第三片。", "c-3"))
    hits = [_hit("c-1", 0.9), _hit("c-2", 0.8)]

    units = collapse(hits, sections={("section", DOC, UNIT_A): rows})

    assert units[0].expansion == EXPANSION_PARENT
    assert units[0].content == "第一片。\n\n第二片。\n\n第三片。"


def test_single_hit_gets_the_neighbour_window() -> None:
    """只命中一片时给「该片 + 前后各一片」，不返回整节。"""

    rows = _rows(
        (0, "第零片。", "c-0"),
        (1, "第一片。", "c-1"),
        (2, "第二片。", "c-2"),
        (3, "第三片。", "c-3"),
    )
    hits = [_hit("c-1", 0.9)]

    units = collapse(hits, sections={("section", DOC, UNIT_A): rows})

    assert units[0].expansion == EXPANSION_NEIGHBORS
    assert units[0].content == "第零片。\n\n第一片。\n\n第二片。"
    assert "第三片" not in units[0].content


def test_rows_of_one_table_collapse_into_one_unit() -> None:
    """同一张表的 4 个行级片折叠成 1 个单元，内容取整表。"""

    table_key = ("table", DOC, UNIT_A, "1")
    hits = [
        _hit("row-1", 0.95, chunk_type="table_row", table_part=1),
        _hit("row-2", 0.93, chunk_type="table_row", table_part=1),
        _hit("row-3", 0.9, chunk_type="table_row", table_part=1),
        _hit("row-4", 0.88, chunk_type="table_row", table_part=1),
    ]

    units = collapse(hits, tables={table_key: "| 型号 | 价格 |\n| XR-2100 | 9600 |"})

    assert len(units) == 1
    assert units[0].expansion == EXPANSION_TABLE
    assert units[0].is_table
    assert "XR-2100" in units[0].content
    assert units[0].chunk_id == "row-1"
    assert len(units[0].source_chunk_ids) == 4


def test_two_different_tables_stay_separate() -> None:
    """不同 table_part 是两张表，不能被并成一个名额。"""

    hits = [
        _hit("row-a", 0.9, chunk_type="table_row", table_part=1),
        _hit("row-b", 0.8, chunk_type="table_row", table_part=2),
    ]
    tables = {
        ("table", DOC, UNIT_A, "1"): "表一",
        ("table", DOC, UNIT_A, "2"): "表二",
    }

    units = collapse(hits, tables=tables)

    assert len(units) == 2
    assert [unit.content for unit in units] == ["表一", "表二"]


def test_hits_across_two_units_each_get_the_whole_unit() -> None:
    """跨两个单元时各自成单元，都取整节。"""

    hits = [
        _hit("a-1", 0.9, heading=HEAD_A),
        _hit("b-1", 0.85, heading=HEAD_B),
    ]
    sections = {
        ("section", DOC, UNIT_A): _rows((0, "甲节第一片。", "a-1"), (1, "甲节第二片。", "a-2")),
        ("section", DOC, UNIT_B): _rows((2, "乙节正文。", "b-1")),
    }

    units = collapse(hits, sections=sections)

    assert len(units) == 2
    assert all(unit.expansion == EXPANSION_PARENT for unit in units)
    assert units[0].content == "甲节第一片。\n\n甲节第二片。"
    assert units[1].content == "乙节正文。"


def test_chunk_without_heading_stands_alone() -> None:
    """无标题文档没有章节，每片各自成单元——不造一个假单元。"""

    hits = [_hit("c-1", 0.9, heading=""), _hit("c-2", 0.8, heading="")]

    units = collapse(hits)

    assert len(units) == 2
    assert [unit.chunk_id for unit in units] == ["c-1", "c-2"]


def test_collapsed_order_follows_first_appearance() -> None:
    """折叠后的顺序沿用调用方排好的次序，不重新排序。"""

    hits = [
        _hit("b-1", 0.9, heading=HEAD_B),
        _hit("a-1", 0.8, heading=HEAD_A),
        _hit("b-2", 0.7, heading=HEAD_B),
    ]
    sections = {
        ("section", DOC, UNIT_B): _rows((0, "乙一。", "b-1"), (1, "乙二。", "b-2")),
        ("section", DOC, UNIT_A): _rows((2, "甲一。", "a-1")),
    }

    units = collapse(hits, sections=sections)

    assert [unit.chunk_id for unit in units] == ["b-1", "a-1"]


def test_mixed_table_rows_and_section_pieces() -> None:
    """同表多行与同节多片同时出现时，各自折成一个名额，且互不吞并。"""

    hits = [
        _hit("row-1", 0.95, chunk_type="table_row", table_part=1),
        _hit("row-2", 0.9, chunk_type="table_row", table_part=1),
        _hit("p-1", 0.8),
        _hit("p-2", 0.7),
    ]
    tables = {("table", DOC, UNIT_A, "1"): "整表"}
    sections = {("section", DOC, UNIT_A): _rows((0, "正文一。", "p-1"), (1, "正文二。", "p-2"))}

    units = collapse(hits, sections=sections, tables=tables)

    assert len(units) == 2
    assert units[0].is_table and units[0].content == "整表"
    assert not units[1].is_table and units[1].content == "正文一。\n\n正文二。"


def test_missing_content_falls_back_to_the_hits_themselves() -> None:
    """取不到同节内容时用命中片自身拼；绝不返回空内容。"""

    hits = [_hit("c-1", 0.9, content="只有我这一片。")]

    units = collapse(hits, sections={})

    assert units[0].content == "只有我这一片。"
    assert units[0].expansion == EXPANSION_CHUNK


def test_top_k_truncates_after_collapsing() -> None:
    """截断发生在折叠之后——先折再数，否则名额还是被同节碎片占着。"""

    hits = [
        _hit("a-1", 0.9, heading=HEAD_A),
        _hit("b-1", 0.8, heading=HEAD_B),
        _hit("c-1", 0.7, heading=HEAD_C),
    ]

    assert len(collapse(hits, top_k=2)) == 2


def test_merge_table_rows_off_keeps_each_row() -> None:
    """关掉合并时行级片各自成单元，但身份仍是它所属的节，不是 chunk id。"""

    hits = [
        _hit("row-1", 0.9, chunk_type="table_row", table_part=1, section_id="s-1"),
        _hit("row-2", 0.8, chunk_type="table_row", table_part=1, section_id="s-1"),
    ]

    units = collapse(hits, merge_table_rows=False)

    assert len(units) == 2
    assert all(unit.section_id == "s-1" for unit in units), "身份不能回落成 chunk id"


def test_empty_input_yields_nothing() -> None:
    assert collapse([]) == []


@pytest.mark.parametrize(
    ("hit", "expected"),
    [
        (_hit("c-1", 0.9), ("section", DOC, UNIT_A)),
        (
            _hit("row-1", 0.9, chunk_type="table_row", table_part=3),
            ("table", DOC, UNIT_A, "3"),
        ),
        (_hit("c-2", 0.9, heading=""), ("chunk", "c-2")),
    ],
)
def test_unit_key_rules(hit: Hit, expected: tuple) -> None:
    assert unit_key(hit) == expected


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("指南 > 上架", "指南 > 上架"),
        ("指南 > 上架 > 机架款", "指南 > 上架"),
        ("指南 > 上架 > 供电 > 空开", "指南 > 上架"),
        ("只管一章", "只管一章"),
        ("", ""),
    ],
)
def test_unit_path_of_truncates_to_two_segments(heading: str, expected: str) -> None:
    assert unit_path_of(heading) == expected
