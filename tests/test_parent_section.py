"""small-to-big：检索结果带上命中切片所属的整节全文。

只测对外行为：一次检索的响应里 ``parent`` 长什么样，以及关掉开关后响应体是否
与改造前一致。父节的拼装顺序、去重方式属于实现细节，不在这里断言。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any


from app.models.chunk import DEFAULT_INDEX_VERSION
from app.schemas.rag import RetrievedChunk

TENANT = "tenant-demo"
KB = "kb-1"
DOC = "doc-1"
SECTION_ID = "a" * 64
HEADING = "部署实施指南 > 设备上架"


def _chunk(chunk_id: str, content: str) -> dict[str, Any]:
    return {
        "document_id": DOC,
        "chunk_id": chunk_id,
        "kb_id": KB,
        "title": "部署实施指南",
        "content": content,
        "score": 0.9,
        "index_version": DEFAULT_INDEX_VERSION,
    }


class _Session:
    """按请求语句形态回答两种查询：切片 meta 与同节切片正文。"""

    def __init__(self, *, metas: dict[str, dict], siblings: list[tuple[str, str, int, str]]):
        self.metas = metas
        self.siblings = siblings
        self.calls = 0

    def execute(self, _statement: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            # 第一次查切片 meta：真实驱动返回的行支持属性访问，这里照做。
            return _Rows(
                [SimpleNamespace(id=chunk_id, meta=meta) for chunk_id, meta in self.metas.items()]
            )
        return _Rows(self.siblings)


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _direct(service: Any, session: _Session, chunks: list[dict], include_parent: bool):
    """同步调用异步的拼装函数；测试不需要事件循环的其他能力。"""

    async def run() -> list[dict]:
        return service._attach_parent_sections(
            session, tenant_id=TENANT, chunks=chunks, include_parent=include_parent
        )

    return asyncio.run(run())


def _session() -> _Session:
    return _Session(
        metas={
            "chunk-2": {
                "section_id": SECTION_ID,
                "heading_path": HEADING,
                "order_index": 1,
            }
        },
        siblings=[
            (DOC, SECTION_ID, 0, "上架前先复核型号与位置。"),
            (DOC, SECTION_ID, 1, "机架款要上柜并用导轨。"),
            (DOC, SECTION_ID, 2, "壁挂款孔距一致，打完孔要清粉尘。"),
        ],
    )


def test_parent_contains_the_whole_section_in_order() -> None:
    """父节正文按 order_index 顺序拼接，且是整节而不只是命中的那一片。"""

    chunks = [_chunk("chunk-2", "机架款要上柜并用导轨。")]

    result = _direct(importlib.import_module("app.services.rag_service"), _session(), chunks, True)

    parent = result[0]["parent"]
    assert parent["section_id"] == SECTION_ID
    assert parent["heading_path"] == HEADING
    assert "上架前先复核型号与位置。" in parent["content"]
    assert "壁挂款孔距一致" in parent["content"], "父节要包含命中片之外的其余片"
    # 顺序：第一句在第二句之前。
    assert parent["content"].index("上架前先复核") < parent["content"].index("机架款要上柜")


def test_include_parent_false_leaves_the_response_untouched() -> None:
    """关掉开关时响应体与改造前逐字一致——这是「开关有效」的定义。"""

    chunks = [_chunk("chunk-2", "机架款要上柜并用导轨。")]
    before = [dict(chunk) for chunk in chunks]

    result = _direct(importlib.import_module("app.services.rag_service"), _session(), chunks, False)

    assert result == before
    assert "parent" not in result[0]


def test_chunk_without_section_path_gets_null_parent() -> None:
    """无标题文档没有章节，不该被塞一个假父节——调用方会以为拿到了完整上下文。"""

    class _NoMetaSession(_Session):
        def __init__(self) -> None:
            super().__init__(metas={"chunk-9": {}}, siblings=[])

    chunks = [_chunk("chunk-9", "无标题正文。")]

    result = _direct(
        importlib.import_module("app.services.rag_service"), _NoMetaSession(), chunks, True
    )

    assert result[0]["parent"] is None


def test_parent_never_crosses_documents() -> None:
    """两篇文档共用同名标题时不能把正文拼成一个假章节。

    section_id 是 heading_path 的哈希，同库两篇文档可以有相同标题，所以父节的
    取法必须按 (文档, 章节) 而不是只按章节。
    """

    class _CrossDocSession(_Session):
        def __init__(self) -> None:
            super().__init__(
                metas={"chunk-7": {"section_id": SECTION_ID, "heading_path": HEADING}},
                siblings=[
                    (DOC, SECTION_ID, 0, "本文档的正文。"),
                    ("doc-other", SECTION_ID, 0, "另一篇同名章节的正文，不该出现。"),
                ],
            )

    chunks = [_chunk("chunk-7", "本文档的正文。")]

    result = _direct(
        importlib.import_module("app.services.rag_service"), _CrossDocSession(), chunks, True
    )

    assert "另一篇同名章节的正文" not in result[0]["parent"]["content"]


def test_retrieved_chunk_accepts_the_parent_field() -> None:
    """契约：``parent`` 能被响应模型接受，且结构与声明一致。"""

    payload = {
        **_chunk("chunk-2", "机架款要上柜并用导轨。"),
        "parent": {
            "section_id": SECTION_ID,
            "heading_path": HEADING,
            "content": "整节正文。",
        },
    }

    validated = RetrievedChunk.model_validate(payload)

    assert validated.parent is not None
    assert validated.parent.section_id == SECTION_ID
    assert validated.parent.heading_path == HEADING


def test_default_is_include_parent() -> None:
    """请求默认开：默认关着就没人会开，small-to-big 等于没做。"""

    request = importlib.import_module("app.schemas.rag").RetrieveRequest(
        kb_ids=[KB], query="怎么上架"
    )

    assert request.include_parent is True
