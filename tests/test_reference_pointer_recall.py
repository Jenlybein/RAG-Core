"""#152 · 路标切片：判定、标记、以及「不出现在检索结果里」的两处过滤。

路标切片是「除了一句显式引用之外没有实质内容」的片（「详见《促销规则》2.4 节。」）。
它的唯一用途是**建边**：

* 索引期把 ``meta.chunk_type`` 标成 ``reference_pointer``（:func:`is_reference_pointer` 的判定见
  ``tests/test_section_graph_extraction.py`` 的正反例）；
* 召回侧**两路各过滤一次**：向量 SQL 与关键词查询。只过滤一路的话，另一种检索方式照样
  会把「命中一句『参见别处』」当成一次成功召回——而那正是 ADR-0027 决策 3 要挡的假命中。

另外钉住两件容易反向错的事：

1. **没有这个键的老切片必须留在召回里**（``NULL != 'x'`` 在 SQL 里是 ``NULL``，整行会被
   静默排除——所以用 ``coalesce``）；
2. **带实质内容的句子不是路标**：它自己回答得了问题，过滤掉它是净损失。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from app.models.chunk import CHUNK_TYPE_REFERENCE_POINTER, DEFAULT_INDEX_VERSION
from app.providers.keyword_search.query_builder import (
    CHUNK_TYPE_FIELD,
    build_bm25_query,
    build_es_mapping,
    build_keyword_documents,
)
from app.providers.vectorstores.pgvector import PgVectorStore, not_reference_pointer
from app.services import indexing_service
from app.services.section_graph import is_reference_pointer

TENANT = "tenant-a"
KB = "kb-1"
DOC = "doc-a"
POINTER_TEXT = "详见《促销活动与优惠券使用规则》2.4 节。"
SUBSTANTIVE_TEXT = "退款超期需人工审核，审核通过后三个工作日内到账，详见《促销规则》2.4 节。"


# ─────────────────────────────────────────────────────── 索引期：标记


class _Store:
    def __init__(self) -> None:
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, _document_id: str, **_kwargs: Any) -> None:
        return None

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.written.append(chunks)


class _Embeddings:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _Splitter:
    def __init__(self, pieces: list[Any]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[Any]:
        return self.pieces


class _EdgeBuilder:
    async def rebuild_document(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None


def _index(monkeypatch: Any, texts: list[str]) -> list[dict[str, Any]]:
    """跑一遍索引编排，返回写进存储的 chunk 字典（只关心 meta.chunk_type）。"""

    from app.utils.text_splitter import SplitPiece

    pieces = [
        SplitPiece(text=text, metadata={"heading_path": "2.1 退款", "chunk_type": "prose"})
        for text in texts
    ]
    document = SimpleNamespace(
        id=DOC, tenant_id=TENANT, kb_id=KB, title="售后手册", content="raw", doc_version=1
    )

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return DEFAULT_INDEX_VERSION

    monkeypatch.setattr(indexing_service.document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(
        indexing_service.document_repository, "try_claim_for_indexing_async", _claim
    )

    async def _update_status(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(indexing_service.document_repository, "update_status", _update_status)
    monkeypatch.setattr(
        indexing_service.knowledge_base_repository,
        "get_effective_index_version",
        _effective_version,
    )
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: SimpleNamespace(
            content="raw", source_type="markdown", metadata={}
        ),
    )

    store = _Store()
    service = indexing_service.IndexingService(
        splitter=_Splitter(pieces),
        embeddings=_Embeddings(),
        stores=[store],
        edge_builder=_EdgeBuilder(),
    )
    asyncio.run(service.index_existing_document(_Session(), DOC))
    return store.written[0]


def test_a_pure_pointer_chunk_is_marked_reference_pointer(monkeypatch: Any) -> None:
    chunks = _index(monkeypatch, [POINTER_TEXT])

    assert chunks[0]["meta"]["chunk_type"] == CHUNK_TYPE_REFERENCE_POINTER
    assert is_reference_pointer(POINTER_TEXT) is True, "标记与判定必须同源"


def test_a_chunk_with_substance_keeps_its_own_type(monkeypatch: Any) -> None:
    chunks = _index(monkeypatch, [SUBSTANTIVE_TEXT])

    assert chunks[0]["meta"]["chunk_type"] == "prose", "它自己回答得了问题，不该被过滤掉"


# ───────────────────────────────────────────── 向量召回：按类型排除


def _compiled(clause: Any) -> str:
    """把条件编译成带字面量的 SQL（与库无关，只为看结构）。"""

    return " ".join(
        str(
            clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        ).split()
    )


class _CapturingSession:
    async def execute(self, statement: Any) -> Any:
        self.statement = statement
        return SimpleNamespace(all=lambda: [])


def test_the_vector_recall_excludes_pointer_chunks() -> None:
    """范围召回的 SQL 里带一条「不是路标」的条件（#152）。"""

    session = _CapturingSession()
    store = PgVectorStore()

    asyncio.run(
        store.similarity_search_scope(
            session,  # type: ignore[arg-type]
            [0.1] * 1536,
            tenant_id=TENANT,
            index_versions={KB: "v1"},
            top_k=5,
        )
    )

    sql = _compiled(session.statement.whereclause)
    assert "chunk_type" in sql
    assert "reference_pointer" in sql
    assert "!=" in sql or "<>" in sql


def test_chunks_without_a_type_stay_in_recall() -> None:
    """``coalesce`` 的理由：没有这个键的老切片 ``NULL != 'x'`` 会整行被静默排除。"""

    sql = _compiled(not_reference_pointer())

    assert "coalesce" in sql.lower()
    assert "''" in sql, "缺键时按空串比较，于是「不是路标」成立"


# ───────────────────────────────────────────── 关键词召回：按类型排除


def test_the_bm25_query_filters_out_pointer_chunks() -> None:
    query = build_bm25_query(
        tenant_id=TENANT, index_versions={KB: "v1"}, search_query="到账时限", top_k=5
    )

    must_not = query["query"]["bool"]["must_not"]

    assert must_not == [{"term": {CHUNK_TYPE_FIELD: CHUNK_TYPE_REFERENCE_POINTER}}]
    # filter 里的租户与配对不能因为这个改动而被挤掉（越权面不能变）。
    assert query["query"]["bool"]["filter"][0] == {"term": {"tenant_id": TENANT}}
    assert "should" in query["query"]["bool"]["filter"][1]["bool"]


def test_the_keyword_mapping_declares_the_type_as_a_keyword() -> None:
    properties = build_es_mapping()["mappings"]["properties"]

    assert properties[CHUNK_TYPE_FIELD] == {"type": "keyword"}, (
        "过滤字段不能分词：分词之后 term 查询匹配不到"
    )


def test_keyword_documents_carry_the_chunk_type_for_filtering() -> None:
    documents = build_keyword_documents(
        [
            {
                "id": "chunk-1",
                "tenant_id": TENANT,
                "kb_id": KB,
                "document_id": DOC,
                "content": POINTER_TEXT,
                "retrieval_text": POINTER_TEXT,
                "meta": {"title": "售后手册", "chunk_type": CHUNK_TYPE_REFERENCE_POINTER},
            },
            {
                "id": "chunk-2",
                "tenant_id": TENANT,
                "kb_id": KB,
                "document_id": DOC,
                "content": SUBSTANTIVE_TEXT,
                "retrieval_text": SUBSTANTIVE_TEXT,
                "meta": {"title": "售后手册"},
            },
        ]
    )

    assert documents[0][CHUNK_TYPE_FIELD] == CHUNK_TYPE_REFERENCE_POINTER
    assert documents[1][CHUNK_TYPE_FIELD] == "", "没有类型的切片不进 must_not 的黑名单"
