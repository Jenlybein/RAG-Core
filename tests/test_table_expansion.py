"""票 #44：行级表片负责召回、整表片负责返回。

只测对外行为：命中的是行级片时，调用方拿到的 content 是不是整表；
以及关掉开关时响应体是否与改造前一致。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

TENANT = "tenant-demo"
DOC = "doc-1"
SECTION_ID = "b" * 64
TABLE_TEXT = "| 型号 | 配件 | 单价 |\n| --- | --- | --- |\n| XR-2100 | 万兆光模块 | 1180 |"


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Session:
    """按调用顺序回答两类查询：切片 meta、整表正文。"""

    def __init__(self, *, metas: dict[str, dict], tables: list[tuple]) -> None:
        self.metas = metas
        self.tables = tables
        self.calls = 0

    def execute(self, _statement: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return _Rows(
                [
                    SimpleNamespace(id=cid, meta=meta, kb_id="kb-1")
                    for cid, meta in self.metas.items()
                ]
            )
        return _Rows(self.tables)


def _chunk(chunk_id: str, content: str) -> dict[str, Any]:
    return {
        "document_id": DOC,
        "chunk_id": chunk_id,
        "kb_id": "kb-1",
        "title": "价格表",
        "content": content,
        "score": 0.9,
        "index_version": "v1",
    }


def _decorate(session: _Session, chunks: list[dict], *, expand: bool) -> list[dict]:
    service = importlib.import_module("app.services.rag_service")

    async def run() -> list[dict]:
        return service._decorate_results(
            session,
            tenant_id=TENANT,
            chunks=chunks,
            include_parent=False,
            expand_table_rows=expand,
        )

    return asyncio.run(run())


def _row_session() -> _Session:
    return _Session(
        metas={
            "chunk-row": {
                "section_id": SECTION_ID,
                "chunk_type": "table_row",
                "table_part": 1,
                "heading_path": "价格表 > 配件价格",
            }
        },
        tables=[(DOC, SECTION_ID, 1, TABLE_TEXT)],
    )


def test_row_hit_returns_the_whole_table() -> None:
    """命中行级片时，正文换成整表：列含义与相邻行都回来了。"""

    chunks = [_chunk("chunk-row", "型号=XR-2100，配件=万兆光模块，单价=1180")]

    result = _decorate(_row_session(), chunks, expand=True)

    # 整表放 table_context；content 保持切片原文——后者是可回溯的引用依据。
    assert result[0]["table_context"] == TABLE_TEXT
    assert result[0]["content"] == "型号=XR-2100，配件=万兆光模块，单价=1180"
    assert "|" in result[0]["table_context"], "整表应当保留表格形态"


def test_expansion_off_keeps_the_row() -> None:
    """关掉展开时拿到的仍是原始那一行，响应体与改造前一致。"""

    chunks = [_chunk("chunk-row", "型号=XR-2100，配件=万兆光模块，单价=1180")]
    before = [dict(chunk) for chunk in chunks]

    result = _decorate(_row_session(), chunks, expand=False)

    assert result == before
    assert "table_context" not in result[0]


def test_whole_table_hit_is_not_expanded_again() -> None:
    """命中的本来就是整表片时，不重复展开、也不改 content。"""

    session = _Session(
        metas={
            "chunk-table": {
                "section_id": SECTION_ID,
                "chunk_type": "table",
                "table_part": 1,
                "heading_path": "价格表 > 配件价格",
            }
        },
        tables=[(DOC, SECTION_ID, 1, TABLE_TEXT)],
    )
    chunks = [_chunk("chunk-table", TABLE_TEXT)]

    result = _decorate(session, chunks, expand=True)

    assert result[0]["content"] == TABLE_TEXT
    assert "table_context" not in result[0], "整表片不需要再给自己配一份整表"


def test_missing_table_falls_back_to_the_row() -> None:
    """整表片找不到时保留原始那一行：宁可少上下文，也不要把结果变成空。"""

    session = _Session(
        metas={
            "chunk-row": {
                "section_id": SECTION_ID,
                "chunk_type": "table_row",
                "table_part": 1,
                "heading_path": "价格表 > 配件价格",
            }
        },
        tables=[],
    )
    chunks = [_chunk("chunk-row", "型号=XR-2100，配件=万兆光模块，单价=1180")]

    result = _decorate(session, chunks, expand=True)

    assert result[0]["content"] == "型号=XR-2100，配件=万兆光模块，单价=1180"
    assert "table_context" not in result[0]


def test_request_defaults_expand_table_rows() -> None:
    """默认开：行级片是给检索用的，不该把单行直接喂给模型。"""

    rag = importlib.import_module("app.schemas.rag")

    request = rag.RetrieveRequest(kb_ids=["kb-1"], query="XR-2100 多少钱")

    assert request.expand_table_rows is True
