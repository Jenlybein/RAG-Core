"""折叠接线：同一个父节的多个命中只占一个名额，同一张表的多行只占一个名额。

折叠算法本身在 `test_result_collapse.py` 里用纯函数钉死了；这里只钉**接线**——
服务层有没有把该传的东西传给折叠、有没有按 `top_k` 截断、开关有没有生效。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

from app.services.rag_service import OVERFETCH_FACTOR

TENANT = "tenant-demo"
KB = "kb-1"
DOC = "doc-1"
PARENT = "p" * 8
SUB = "s" * 8


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
    """按调用顺序回答：meta → 父节行 → 整表 → 定位用的 meta。

    顺序与 `_collapse_results` 里的调用顺序一致；用队列而不是靠 SQL 解析，
    是为了让「多查了一次」这种回归立刻表现为 IndexError 而不是静默通过。
    """

    def __init__(self, *, metas: dict[str, dict], queue: list[list[Any]]) -> None:
        self.metas = metas
        self.queue = list(queue)
        self.calls = 0

    def execute(self, _statement: Any) -> Any:
        self.calls += 1
        # 第一次与最后一次都是 meta 查询（中间夹着父节 / 整表）。
        if self.calls == 1:
            return _Rows(
                [SimpleNamespace(id=cid, meta=meta, kb_id=KB) for cid, meta in self.metas.items()]
            )
        if self.calls == 2 and self.queue:
            return _Rows(self.queue.pop(0))
        if self.calls == 3 and self.queue:
            return _Rows(self.queue.pop(0))
        return _Rows(
            [SimpleNamespace(id=cid, meta=meta, kb_id=KB) for cid, meta in self.metas.items()]
        )


def _run(session: _Session, chunks: list[dict], **kwargs) -> list[dict]:
    service = importlib.import_module("app.services.rag_service")
    options = {
        "include_parent": True,
        "expand_table_rows": True,
        "top_k": 5,
    }
    options.update(kwargs)

    async def go() -> list[dict]:
        return service._collapse_results(session, tenant_id=TENANT, chunks=chunks, **options)

    return asyncio.run(go())


def _section_session() -> _Session:
    return _Session(
        metas={
            "c-1": {
                "section_id": SUB,
                "parent_section_id": PARENT,
                "chunk_type": "prose",
                "heading_path": "部署实施指南 > 上架",
                "page": 3,
                "doc_version": 1,
            },
            "c-2": {
                "section_id": SUB,
                "parent_section_id": PARENT,
                "chunk_type": "prose",
                "heading_path": "部署实施指南 > 上架",
            },
        },
        queue=[
            [
                (DOC, PARENT, 0, "第零片。", "c-0"),
                (DOC, PARENT, 1, "第一片。", "c-1"),
                (DOC, PARENT, 2, "第二片。", "c-2"),
            ]
        ],
    )


def test_two_hits_in_one_parent_occupy_one_slot() -> None:
    """同一父节命中两片 → 一条结果，内容是整节。"""

    chunks = [_chunk("c-1", "第一片。", 0.9), _chunk("c-2", "第二片。", 0.8)]

    results = _run(_section_session(), chunks)

    assert len(results) == 1
    assert results[0]["chunk_id"] == "c-1", "代表片是分最高的那片"
    assert results[0]["section_id"] == PARENT, "身份是节，不是片"
    assert results[0]["expansion"] == "parent"
    assert results[0]["source_chunk_ids"] == ["c-1", "c-2"]
    assert results[0]["content"] == "第零片。\n\n第一片。\n\n第二片。"
    assert results[0]["page"] == 3 and results[0]["doc_version"] == 1
    assert results[0]["heading_path"] == "部署实施指南 > 上架"


def test_collapsed_result_has_no_parent_or_table_field() -> None:
    """折叠后 content 就是大块，parent / table_context 是重复数据，不能露出来。"""

    results = _run(_section_session(), [_chunk("c-1", "第一片。", 0.9)])

    assert "parent" not in results[0]
    assert "table_context" not in results[0]


def test_single_hit_gets_the_neighbour_window() -> None:
    """只命中一片时给「该片 + 前后各一片」，不返回整节。"""

    results = _run(_section_session(), [_chunk("c-1", "第一片。", 0.9)])

    assert results[0]["expansion"] == "neighbors"
    assert results[0]["content"] == "第零片。\n\n第一片。\n\n第二片。"


def test_rows_of_one_table_occupy_one_slot() -> None:
    """同一张表的两行 → 一条结果，内容是整表。"""

    session = _Session(
        metas={
            "row-1": {
                "section_id": SUB,
                "parent_section_id": PARENT,
                "chunk_type": "table_row",
                "table_part": 1,
                "heading_path": "价格表 > 整机指导价",
            },
            "row-2": {
                "section_id": SUB,
                "parent_section_id": PARENT,
                "chunk_type": "table_row",
                "table_part": 1,
                "heading_path": "价格表 > 整机指导价",
            },
        },
        queue=[[(DOC, SUB, 1, "| 型号 | 价格 |\n| XR-2100 | 9600 |")]],
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

    session = _Session(
        metas={
            "a-1": {"section_id": SUB, "parent_section_id": PARENT, "chunk_type": "prose"},
            "b-1": {
                "section_id": "s2",
                "parent_section_id": "p2",
                "chunk_type": "prose",
            },
            "c-1": {
                "section_id": "s3",
                "parent_section_id": "p3",
                "chunk_type": "prose",
            },
        },
        queue=[
            [
                (DOC, PARENT, 0, "甲。", "a-1"),
                (DOC, "p2", 1, "乙。", "b-1"),
                (DOC, "p3", 2, "丙。", "c-1"),
            ]
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

    chunks = [_chunk("c-1", "第一片。", 0.9), _chunk("c-2", "第二片。", 0.8)]

    results = _run(_section_session(), chunks, include_parent=False)

    assert [row["chunk_id"] for row in results] == ["c-1", "c-2"]
    assert [row["content"] for row in results] == ["第一片。", "第二片。"]
    assert all("section_id" not in row for row in results)


def test_overfetch_factor_is_a_constant_not_a_knob() -> None:
    """over-fetch 因子写死：15 道题的样本量下，可调旋钮必然被调成过拟合值。"""

    assert OVERFETCH_FACTOR == 3
