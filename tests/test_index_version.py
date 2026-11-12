"""索引版本与检索文本的写入契约。

``chunks.index_version`` 是数据范围的一部分（后续范围检索按
``(tenant_id, kb_id, index_version)`` 过滤），默认值 ``v1``。这里用 SQLite 起一张
真实的 ``chunks`` 表、把行写进去再读回来，断言的是**数据库里的行**，不是内存里的
字典。

同一条契约的另一半在索引编排里：送进 embedding 的是**检索文本**（文档名 + 章节
路径 + 正文），落库的 ``content`` 仍是**切片原文**，并且每个切片行都带
``index_version='v1'``。

为什么手写建表 DDL：``chunks`` 的真实列类型是 PostgreSQL 的 ``JSONB`` 与
``Vector(1536)``，SQLite 编译器渲染不了 ``JSONB``。本测试只需要这几列，因此用
SQLite 能接受的等价 DDL 建表；ORM 的写入 / 读回仍走真实的 ``PgVectorStore`` 与
``Chunk`` 映射。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models.chunk import Chunk
from app.providers.parsers.base import ParsedDocument
from app.providers.vectorstores.pgvector import PgVectorStore
from app.repositories import document_repository
from app.services import indexing_service
from app.services.indexing_service import IndexingService
from app.utils.text_splitter import SplitPiece

TENANT_ID = "tenant_demo"
KB_ID = "kb-1"
DOCUMENT_ID = "doc-1"
EMBEDDING_DIM = 1536
ORIGINAL_CONTENT = "切片原文：错误码 E1024 表示证书过期。"

# 与 ORM 映射等价的 SQLite 建表语句（JSONB / Vector 用 SQLite 能接受的类型）。
CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSON NOT NULL,
    index_version VARCHAR(32) NOT NULL DEFAULT 'v1'
)
"""


@pytest.fixture
def chunk_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(CHUNKS_DDL))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _chunk(index_version: str | None = "v1") -> dict[str, object]:
    chunk: dict[str, object] = {
        "id": "chunk-1",
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": DOCUMENT_ID,
        "content": ORIGINAL_CONTENT,
        "embedding": [0.1] * EMBEDDING_DIM,
        "meta": {"heading_path": "错误码 > E1024", "chunk_type": "prose"},
    }
    if index_version is not None:
        chunk["index_version"] = index_version
    return chunk


def test_add_chunks_persists_the_original_content_with_index_version_v1(
    chunk_session: Session,
) -> None:
    """写进去的是切片原文；索引版本同时落库为 v1，两者互不替换。"""

    asyncio.run(PgVectorStore().add_chunks(chunk_session, [_chunk()]))
    chunk_session.commit()

    row = chunk_session.execute(select(Chunk)).scalar_one()
    assert row.content == ORIGINAL_CONTENT, "chunks.content 必须是切片原文"
    assert row.index_version == "v1"
    assert row.meta["heading_path"] == "错误码 > E1024", "切块 provenance 仍随行落库"


def test_add_chunks_falls_back_to_the_default_index_version(chunk_session: Session) -> None:
    """调用方没给索引版本时由列默认值兜底，不写 NULL。"""

    asyncio.run(PgVectorStore().add_chunks(chunk_session, [_chunk(index_version=None)]))
    chunk_session.commit()

    row = chunk_session.execute(select(Chunk)).scalar_one()
    assert row.index_version == "v1"


# --------------------------------------------------------------- 索引编排的写入契约


class _FakeSession:
    """索引编排只用到 ``commit`` / ``rollback``；这里只记录次数，不碰数据库。

    票 #132 起流水线跑在 ``AsyncSession`` 上，替身因此是异步面。
    """

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _StubEmbeddings:
    """记录送进 embedding 的文本，返回与条数对应的固定向量，绝不发网络请求。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.1] * 1536 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _RecordingStore:
    """记录 purge / add 的最小存储替身。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, _document_id: str, *, tenant_id: str) -> None:
        self.calls.append("purge")

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append("add")
        self.written.append(chunks)


class _StubSplitter:
    def __init__(self, pieces: list[SplitPiece]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[SplitPiece]:
        return self.pieces


def test_indexing_embeds_retrieval_text_and_stores_the_original_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一份带标题与章节的文档：向量输入是检索文本，落库的是原文，且带 v1。"""

    document = SimpleNamespace(
        id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="错误码查询",
        doc_version=1,
    )
    pieces = [
        SplitPiece(
            text="错误码 E1024 表示证书过期。",
            metadata={"heading_path": "错误码 > E1024"},
        ),
        SplitPiece(text="直接联系运维。", metadata={"heading_path": "错误码 > E1024"}),
    ]
    embeddings = _StubEmbeddings()
    store = _RecordingStore()

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(*_a: Any, **_k: Any) -> bool:
        return True

    async def _update_status(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(document_repository, "get_by_id_async", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "update_status_async", _update_status)
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: ParsedDocument(content="raw", source_type="markdown"),
    )

    service = IndexingService(splitter=_StubSplitter(pieces), embeddings=embeddings, stores=[store])
    count = asyncio.run(service.index_existing_document(_FakeSession(), DOCUMENT_ID))

    assert count == 2
    assert store.calls == ["purge", "add"]

    # 送进 embedding 的必须是检索文本：文档名 + 章节路径 + 正文。
    assert embeddings.texts == [
        "文档：错误码查询 章节：错误码 > E1024 正文：错误码 E1024 表示证书过期。",
        "文档：错误码查询 章节：错误码 > E1024 正文：直接联系运维。",
    ]

    chunks = store.written[0]
    # 落库的 content 仍是切片原文，绝不能被检索文本替换。
    assert [chunk["content"] for chunk in chunks] == [
        "错误码 E1024 表示证书过期。",
        "直接联系运维。",
    ]
    assert embeddings.texts != [chunk["content"] for chunk in chunks], "两者必须是不同的文本"
    assert [chunk["index_version"] for chunk in chunks] == ["v1", "v1"]
