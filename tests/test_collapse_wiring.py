"""折叠接线：同一 h2 单元的多个命中只占一个名额，同一张表的多行只占一个名额。

折叠算法本身在 `test_result_collapse.py` 里用纯函数钉死了；这里只钉**接线**——
服务层有没有把该传的东西传给折叠、有没有按 `top_k` 截断、开关有没有生效。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

from app.models.chunk import DEFAULT_INDEX_VERSION
from app.services import result_collapse
from app.services.result_collapse import OVERFETCH_FACTOR
from tests.fake_db import AsyncSessionAdapter

TENANT = "tenant-demo"
KB = "kb-1"
DOC = "doc-1"
HEAD_A = "指南 > 上架"
HEAD_B = "指南 > 供电"
UNIT_A = result_collapse.unit_id_of(HEAD_A)
UNIT_B = result_collapse.unit_id_of(HEAD_B)


def _chunk(chunk_id: str, content: str, score: float) -> dict[str, Any]:
    return {
        "document_id": DOC,
        "chunk_id": chunk_id,
        "kb_id": KB,
        "title": "部署实施指南",
        "content": content,
        "score": score,
        "index_version": "v1",
    }


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Session:
    """按调用顺序回答：meta → 单元行 → 定位用的 meta。

    顺序与 `collapse_results` 的调用顺序一致；用队列而不是解析 SQL，是为了让
    「多查了一次」这种回归立刻表现为 IndexError 而不是静默通过。
    """

    def __init__(self, *, metas: dict[str, dict], rows: list[tuple]) -> None:
        self.metas = metas
        self.rows = rows
        self.calls = 0

    def execute(self, _statement: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return _Rows(
                [SimpleNamespace(id=cid, meta=meta, kb_id=KB) for cid, meta in self.metas.items()]
            )
        if self.calls == 2:
            return _Rows(self.rows)
        raise AssertionError("折叠路径不该再查第三次：meta 已经查过一次了")


def _run(session: _Session, chunks: list[dict], **kwargs) -> list[dict]:
    collapse = importlib.import_module("app.services.retrieval.collapse")
    # 票 #140：折叠是**检索内核**的最后一步，参数全部显式（不再吃一个值对象）——
    # 开关与条数由组合根从 ``RetrievalOptions`` 上读出来再交进去。
    call: dict[str, Any] = {
        "top_k": 5,
        "fold_to_sections": True,
        "expand_table_rows": True,
    }
    call.update(kwargs)

    async def go() -> list[dict]:
        # 第 11 步票 #127：折叠在异步栈上（``await``），会话替身因此包一层异步面。
        return await collapse.collapse_results(
            AsyncSessionAdapter(session),
            tenant_id=TENANT,
            chunks=chunks,
            index_versions={KB: DEFAULT_INDEX_VERSION},
            **call,
        )

    return asyncio.run(go())


def _meta(heading: str, **extra: Any) -> dict:
    base = {"heading_path": heading, "chunk_type": "prose", "page": None, "doc_version": None}
    base.update(extra)
    return base


def _row(
    index: int, content: str, chunk_id: str, heading: str, kind: str = "prose", table_part: int = 0
) -> tuple:
    return (DOC, heading, index, content, chunk_id, kind, table_part)


def test_two_hits_in_one_unit_occupy_one_slot() -> None:
    """同一 h2 命中两片 → 一条结果，内容是整节。"""

    session = _Session(
        metas={
            "c-1": _meta(HEAD_A, page=3, doc_version=1),
            "c-2": _meta(HEAD_A),
        },
        rows=[
            _row(0, "第零片。", "c-0", HEAD_A),
            _row(1, "第一片。", "c-1", HEAD_A),
            _row(2, "第二片。", "c-2", HEAD_A),
        ],
    )
    chunks = [_chunk("c-1", "第一片。", 0.9), _chunk("c-2", "第二片。", 0.8)]

    results = _run(session, chunks)

    assert len(results) == 1
    assert results[0]["chunk_id"] == "c-1", "代表片是分最高的那片"
    assert results[0]["section_id"] == UNIT_A, "身份是节，不是片"
    assert results[0]["expansion"] == "parent"
    assert results[0]["source_chunk_ids"] == ["c-1", "c-2"]
    assert results[0]["content"] == "第零片。\n\n第一片。\n\n第二片。"
    assert results[0]["page"] == 3 and results[0]["doc_version"] == 1
    assert results[0]["heading_path"] == HEAD_A


def test_collapsed_result_has_no_parent_or_table_field() -> None:
    """折叠后 content 就是大块，parent / table_context 是重复数据，不能露出来。"""

    session = _Session(metas={"c-1": _meta(HEAD_A)}, rows=[_row(0, "第一片。", "c-1", HEAD_A)])

    results = _run(session, [_chunk("c-1", "第一片。", 0.9)])

    assert "parent" not in results[0]
    assert "table_context" not in results[0]


def test_single_hit_gets_the_neighbour_window() -> None:
    """只命中一片时给「该片 + 前后各一片」。

    刻意用**五片**：三片的节里「前后各一片」正好等于整节，两个断言会同时通过，
    写成 always-whole-section 也能骗过测试。
    """

    session = _Session(
        metas={"c-2": _meta(HEAD_A)},
        rows=[
            _row(0, "第零片。", "c-0", HEAD_A),
            _row(1, "第一片。", "c-1", HEAD_A),
            _row(2, "第二片。", "c-2", HEAD_A),
            _row(3, "第三片。", "c-3", HEAD_A),
            _row(4, "第四片。", "c-4", HEAD_A),
        ],
    )

    results = _run(session, [_chunk("c-2", "第二片。", 0.9)])

    assert results[0]["expansion"] == "neighbors"
    assert results[0]["content"] == "第一片。\n\n第二片。\n\n第三片。"
    assert "第零片" not in results[0]["content"], "窗口外的不该给"
    assert "第四片" not in results[0]["content"]


def test_rows_of_one_table_occupy_one_slot() -> None:
    """同一张表的两行 → 一条结果，内容是整表。"""

    session = _Session(
        metas={
            "row-1": _meta(HEAD_A, chunk_type="table_row", table_part=1),
            "row-2": _meta(HEAD_A, chunk_type="table_row", table_part=1),
        },
        rows=[
            _row(0, "| 型号 | 价格 |\n| XR-2100 | 9600 |", "tbl", HEAD_A, "table", 1),
            _row(1, "型号=XR-2100，指导价=9600", "row-1", HEAD_A, "table_row", 1),
            _row(2, "型号=XR-2000，指导价=1280", "row-2", HEAD_A, "table_row", 1),
        ],
    )
    chunks = [
        _chunk("row-1", "型号=XR-2100，指导价=9600", 0.9),
        _chunk("row-2", "型号=XR-2000，指导价=1280", 0.8),
    ]

    results = _run(session, chunks)

    assert len(results) == 1
    assert results[0]["expansion"] == "table"
    assert "XR-2100" in results[0]["content"]
    assert len(results[0]["source_chunk_ids"]) == 2


def test_top_k_truncates_after_collapsing() -> None:
    """先折叠再截断：否则名额还是被同节碎片占着。"""

    head_c = "手册 > 概览"
    session = _Session(
        metas={
            "a-1": _meta(HEAD_A),
            "b-1": _meta(HEAD_B),
            "c-1": _meta(head_c),
        },
        rows=[
            _row(0, "甲。", "a-1", HEAD_A),
            _row(1, "乙。", "b-1", HEAD_B),
            _row(2, "丙。", "c-1", head_c),
        ],
    )
    chunks = [
        _chunk("a-1", "甲。", 0.9),
        _chunk("b-1", "乙。", 0.8),
        _chunk("c-1", "丙。", 0.7),
    ]

    results = _run(session, chunks, top_k=2)

    assert [row["chunk_id"] for row in results] == ["a-1", "b-1"]


def test_collapse_off_returns_the_raw_pieces() -> None:
    """关掉折叠时返回小片本身，不按节归组——留给「只要精确到那一行」的调用方。"""

    session = _Session(metas={"c-1": _meta(HEAD_A), "c-2": _meta(HEAD_A)}, rows=[])
    # 关掉折叠时只查一次 meta（补定位字段），第二次 execute 不该发生。
    session.rows = []

    class _Once(_Session):
        def execute(self, statement: Any) -> Any:
            self.calls += 1
            assert self.calls == 1, "不折叠时不该查单元行"
            return _Rows(
                [SimpleNamespace(id=cid, meta=meta, kb_id=KB) for cid, meta in self.metas.items()]
            )

    chunks = [_chunk("c-1", "第一片。", 0.9), _chunk("c-2", "第二片。", 0.8)]

    results = _run(_Once(metas=session.metas, rows=[]), chunks, fold_to_sections=False)

    assert [row["chunk_id"] for row in results] == ["c-1", "c-2"]
    assert [row["content"] for row in results] == ["第一片。", "第二片。"]
    assert all("section_id" not in row for row in results)


def test_overfetch_factor_is_a_constant_not_a_knob() -> None:
    """over-fetch 因子写死：15 道题的样本量下，可调旋钮必然被调成过拟合值。"""

    assert OVERFETCH_FACTOR == 3
