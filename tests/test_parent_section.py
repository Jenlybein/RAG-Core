"""small-to-big 的分级扩展：返回范围按命中情况分三档。

只测对外行为：一次检索的响应里 ``parent`` 给了多大范围、为什么给这么大。
具体取哪几片属于实现细节，不在这里断言。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.chunk import DEFAULT_INDEX_VERSION
from app.schemas.rag import RetrievedChunk

TENANT = "tenant-demo"
KB = "kb-1"
DOC = "doc-1"
SECTION_A = "a" * 64
SECTION_B = "b" * 64
PARENT_ID = "p" * 64
HEADING_A = "部署实施指南 > 设备上架"
HEADING_B = "部署实施指南 > 供电接入"


def _chunk(chunk_id: str, content: str, document_id: str = DOC) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "kb_id": KB,
        "title": "部署实施指南",
        "content": content,
        "score": 0.9,
        "index_version": DEFAULT_INDEX_VERSION,
    }


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Session:
    """按调用顺序回答两类查询：切片 meta、同节切片（带 chunk_id 便于定位）。"""

    def __init__(self, *, metas: dict[str, dict], sections: list[tuple]) -> None:
        self.metas = metas
        self.sections = sections
        self.calls = 0

    def execute(self, _statement: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return _Rows(
                [SimpleNamespace(id=cid, meta=meta, kb_id=KB) for cid, meta in self.metas.items()]
            )
        return _Rows(self.sections)


def _decorate(session: _Session, chunks: list[dict]) -> list[dict]:
    service = importlib.import_module("app.services.rag_service")

    async def run() -> list[dict]:
        return service._decorate_results(
            session,
            tenant_id=TENANT,
            chunks=chunks,
            include_parent=True,
            expand_table_rows=False,
        )

    return asyncio.run(run())


def _five_piece_section() -> list[tuple]:
    """同节 5 片，order 0..4。"""
    return [(DOC, SECTION_A, order, f"第{order}片正文。", f"chunk-{order}") for order in range(5)]


def test_single_hit_gets_neighbours_not_the_whole_section() -> None:
    """同节只命中 1 片时，返回「该片 + 前后各一片」而不是整节。

    一刀切返回整节会把 token 花在没命中的片上；命中 1 片却有 5 片时差距是 3 倍。
    """

    session = _Session(
        metas={"chunk-2": {"section_id": SECTION_A, "heading_path": HEADING_A}},
        sections=_five_piece_section(),
    )

    result = _decorate(session, [_chunk("chunk-2", "第2片正文。")])

    parent = result[0]["parent"]
    assert parent["expansion"] == "neighbors"
    assert parent["piece_count"] == 3, "命中 + 前后各一 = 3 片"
    assert "第1片正文。" in parent["content"] and "第3片正文。" in parent["content"]
    assert "第0片正文。" not in parent["content"], "再往外就是噪声"
    assert "第4片正文。" not in parent["content"]


def test_two_hits_in_one_parent_get_the_whole_parent() -> None:
    """同一父节内命中 ≥2 片说明答案确实跨了子节，给整个父节。"""

    session = _Session(
        metas={
            "chunk-1": {"section_id": SECTION_A, "heading_path": HEADING_A},
            "chunk-3": {"section_id": SECTION_A, "heading_path": HEADING_A},
        },
        sections=_five_piece_section(),
    )

    result = _decorate(
        session, [_chunk("chunk-1", "第1片正文。"), _chunk("chunk-3", "第3片正文。")]
    )

    for row in result:
        assert row["parent"]["expansion"] == "parent"
        assert row["parent"]["piece_count"] == 5
    assert "第0片正文。" in result[0]["parent"]["content"]


def test_cross_section_hits_get_each_whole_section() -> None:
    """跨节命中时每节都给整节：跨节题本来就需要多节上下文。"""

    session = _Session(
        metas={
            "chunk-2": {"section_id": SECTION_A, "heading_path": HEADING_A},
            "chunk-b1": {"section_id": SECTION_B, "heading_path": HEADING_B},
        },
        sections=[
            *_five_piece_section(),
            (DOC, SECTION_B, 0, "甲节正文。", "chunk-b0"),
            (DOC, SECTION_B, 1, "乙节正文。", "chunk-b1"),
        ],
    )

    result = _decorate(
        session, [_chunk("chunk-2", "第2片正文。"), _chunk("chunk-b1", "乙节正文。")]
    )

    assert all(row["parent"]["expansion"] == "parent" for row in result)
    assert all(row["parent"]["cross_section"] is True for row in result)
    assert result[0]["parent"]["section_id"] == SECTION_A
    assert result[1]["parent"]["section_id"] == SECTION_B
    assert "第0片正文。" in result[0]["parent"]["content"], "跨节时不该只给邻居"


def test_two_h3_sections_of_one_parent_are_assembled_in_document_order() -> None:
    """父节是 h2：同属一个 h2 的两个 h3 命中时，按文档级 chunk_index 拼回整节。

    `order_index` 只是节内序号，跨 h3 用它排序会拼出错乱的上下文——所以索引期
    另存了文档级的 `chunk_index`。
    """

    session = _Session(
        metas={
            "a-0": {
                "section_id": SECTION_A,
                "parent_section_id": PARENT_ID,
                "heading_path": "部署实施指南 > 上架",
            },
            "b-1": {
                "section_id": SECTION_B,
                "parent_section_id": PARENT_ID,
                "heading_path": "部署实施指南 > 供电",
            },
        },
        sections=[
            (DOC, PARENT_ID, 0, "先复核型号。", "a-0"),
            (DOC, PARENT_ID, 1, "再上柜。", "a-1"),
            (DOC, PARENT_ID, 2, "最后接电。", "b-0"),
            (DOC, PARENT_ID, 3, "空开额定要够。", "b-1"),
        ],
    )

    result = _decorate(session, [_chunk("a-0", "先复核型号。"), _chunk("b-1", "空开额定要够。")])

    for row in result:
        assert row["parent"]["expansion"] == "parent"
        assert row["parent"]["piece_count"] == 4
        assert row["parent"]["section_id"] == PARENT_ID
    content = result[0]["parent"]["content"]
    assert content.index("先复核型号。") < content.index("最后接电。")


def test_single_piece_section_returns_that_piece() -> None:
    """整节只有一片时，邻居窗口退化为这一片本身。"""

    session = _Session(
        metas={"chunk-only": {"section_id": SECTION_B, "heading_path": HEADING_B}},
        sections=[(DOC, SECTION_B, 0, "唯一一片正文。", "chunk-only")],
    )

    result = _decorate(session, [_chunk("chunk-only", "唯一一片正文。")])

    assert result[0]["parent"]["piece_count"] == 1
    assert result[0]["parent"]["content"] == "唯一一片正文。"


def test_parent_never_crosses_documents() -> None:
    """两篇文档共用同名标题时不能把正文拼成一个假章节。

    section_id 是 heading_path 的哈希，同库两篇文档可以有相同标题，所以父节必须
    按 (文档, 章节) 取，而不是只按章节。
    """

    session = _Session(
        metas={
            "chunk-7": {"section_id": SECTION_A, "heading_path": HEADING_A},
            "chunk-8": {"section_id": SECTION_A, "heading_path": HEADING_A},
        },
        sections=[
            (DOC, SECTION_A, 0, "本文档的正文。", "chunk-7"),
            ("doc-other", SECTION_A, 0, "另一篇同名章节的正文，不该出现。", "chunk-8"),
        ],
    )

    result = _decorate(session, [_chunk("chunk-7", "本文档的正文。")])

    assert "另一篇同名章节的正文" not in result[0]["parent"]["content"]


def test_chunk_without_section_path_gets_null_parent() -> None:
    """无标题文档没有章节，不该被塞一个假父节。"""

    session = _Session(metas={"chunk-9": {}}, sections=[])

    result = _decorate(session, [_chunk("chunk-9", "无标题正文。")])

    assert result[0]["parent"] is None


def test_include_parent_false_leaves_the_response_untouched() -> None:
    """关掉开关时内部字典与改造前逐字一致——这是「开关有效」的定义。"""

    session = _Session(
        metas={"chunk-2": {"section_id": SECTION_A, "heading_path": HEADING_A}},
        sections=_five_piece_section(),
    )
    chunks = [_chunk("chunk-2", "第2片正文。")]
    before = [dict(chunk) for chunk in chunks]
    service = importlib.import_module("app.services.rag_service")

    async def run() -> list[dict]:
        return service._decorate_results(
            session,
            tenant_id=TENANT,
            chunks=chunks,
            include_parent=False,
            expand_table_rows=False,
        )

    result = asyncio.run(run())

    assert result == before
    assert "parent" not in result[0]


@pytest.mark.parametrize("field", ["expansion", "piece_count", "cross_section"])
def test_parent_payload_declares_expansion_fields(field: str) -> None:
    """契约：扩展信息是响应的一部分，调用方据此判断「上下文给够了没有」。"""

    payload = {
        **_chunk("chunk-2", "第2片正文。"),
        "parent": {
            "section_id": SECTION_A,
            "heading_path": HEADING_A,
            "content": "整节正文。",
            "expansion": "neighbors",
            "piece_count": 3,
            "cross_section": False,
        },
    }

    validated = RetrievedChunk.model_validate(payload)

    assert getattr(validated.parent, field) is not None


def test_request_defaults_include_parent() -> None:
    """默认开：默认关着就没人会开，small-to-big 等于没做。"""

    rag = importlib.import_module("app.schemas.rag")
    request = rag.RetrieveRequest(kb_ids=[KB], query="怎么上架")

    assert request.include_parent is True
