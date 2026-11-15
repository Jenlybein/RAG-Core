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
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import Chunk, DEFAULT_INDEX_VERSION
from app.models.document_version import DocumentVersion
from app.providers.parsers.base import ParsedDocument
from app.providers.vectorstores.pgvector import PgVectorStore
from app.repositories import document_repository, knowledge_base_repository
from app.services import indexing_service
from app.services.indexing_service import IndexingService
from app.utils.text_splitter import SplitPiece
from tests.fake_db import AsyncRecordingSession, ZERO_COUNT

TENANT_ID = "tenant_demo"
KB_ID = "kb-1"
DOCUMENT_ID = "doc-1"
EMBEDDING_DIM = 1536
ORIGINAL_CONTENT = "切片原文：错误码 E1024 表示证书过期。"

# 与 ORM 映射等价的 SQLite 建表语句（JSONB / Vector 用 SQLite 能接受的类型）。
# 列必须与模型同形：ORM 的 INSERT 会把每一列都点出来，缺一列就是「表不存在」的
# OperationalError，而不是一条更好懂的断言失败（#172 加稿号列时踩到）。
CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSON NOT NULL,
    index_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    doc_version INTEGER NOT NULL DEFAULT '1'
)
"""


@pytest_asyncio.fixture
async def chunk_session() -> AsyncIterator[AsyncSession]:
    """真 SQL 的 ``chunks`` 表（SQLite），会话是**异步**的（票 #132 起写切片走 AsyncSession）。

    ``pytest_asyncio.fixture`` 而不是 ``pytest.fixture``：strict 模式下异步夹具必须显式
    声明，否则用例拿到的是那个异步生成器本身。
    """

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(text(CHUNKS_DDL))
    async with AsyncSession(engine) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


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


def _draft_chunk(chunk_id: str, doc_version: int) -> dict[str, object]:
    """同一篇文档的一片的第 ``doc_version`` 稿版本（id 不同，其余同形）。"""

    chunk = _chunk()
    chunk["id"] = chunk_id
    chunk["doc_version"] = doc_version
    return chunk


@pytest.mark.asyncio
async def test_add_chunks_persists_the_original_content_with_index_version_v1(
    chunk_session: AsyncSession,
) -> None:
    """写进去的是切片原文；索引版本同时落库为 v1，两者互不替换。"""

    await PgVectorStore().add_chunks(chunk_session, [_chunk()])
    await chunk_session.commit()

    row = (await chunk_session.execute(select(Chunk))).scalar_one()
    assert row.content == ORIGINAL_CONTENT, "chunks.content 必须是切片原文"
    assert row.index_version == "v1"
    assert row.meta["heading_path"] == "错误码 > E1024", "切块 provenance 仍随行落库"


@pytest.mark.asyncio
async def test_add_chunks_falls_back_to_the_default_index_version(
    chunk_session: AsyncSession,
) -> None:
    """调用方没给索引版本时由列默认值兜底，不写 NULL。"""

    await PgVectorStore().add_chunks(chunk_session, [_chunk(index_version=None)])
    await chunk_session.commit()

    row = (await chunk_session.execute(select(Chunk))).scalar_one()
    assert row.index_version == "v1"


@pytest.mark.asyncio
async def test_add_chunks_persists_the_draft_number_in_the_real_column(
    chunk_session: AsyncSession,
) -> None:
    """稿号落在 ``chunks.doc_version`` 这一列上，而不是 JSONB 里的一个键（#173）。

    列是 #172 的迁移加的；写入侧在这一票跟上，于是「这一片属于第几稿」有了
    唯一的存放处——按稿删除、按稿过滤都读它。
    """

    chunk = _chunk()
    chunk["doc_version"] = 3

    await PgVectorStore().add_chunks(chunk_session, [chunk])
    await chunk_session.commit()

    row = (await chunk_session.execute(select(Chunk))).scalar_one()
    assert row.doc_version == 3
    assert "doc_version" not in (row.meta or {}), "JSONB 里不再留第二份稿号"


@pytest.mark.asyncio
async def test_add_chunks_falls_back_to_the_first_draft(chunk_session: AsyncSession) -> None:
    """调用方没给稿号时由列默认值兜底（第 1 稿），绝不写 NULL。"""

    await PgVectorStore().add_chunks(chunk_session, [_chunk()])
    await chunk_session.commit()

    row = (await chunk_session.execute(select(Chunk))).scalar_one()
    assert row.doc_version == 1


@pytest.mark.asyncio
async def test_delete_by_document_id_scopes_to_one_draft(chunk_session: AsyncSession) -> None:
    """按「文档 + 稿次」删（#173）：只删这一稿的切片行，别的稿原样留着。

    这是「每稿各留切片」的删除侧：重建第 2 稿只清第 2 稿，第 1 稿一行都不动——
    否则回滚到第 1 稿时那些切片永远回不来了。
    """

    store = PgVectorStore()
    await store.add_chunks(
        chunk_session, [_draft_chunk("chunk-v1", 1), _draft_chunk("chunk-v2", 2)]
    )
    await chunk_session.flush()

    await store.delete_by_document_id(
        chunk_session, DOCUMENT_ID, tenant_id=TENANT_ID, doc_version=2
    )

    rows = (
        await chunk_session.execute(select(Chunk.id, Chunk.doc_version).order_by(Chunk.id))
    ).all()
    assert [(row.id, row.doc_version) for row in rows] == [("chunk-v1", 1)]


@pytest.mark.asyncio
async def test_delete_by_document_id_without_a_draft_removes_every_draft(
    chunk_session: AsyncSession,
) -> None:
    """``doc_version=None`` = 这篇文档的所有稿：删文档那条老路径仍能清干净。"""

    store = PgVectorStore()
    await store.add_chunks(
        chunk_session, [_draft_chunk("chunk-v1", 1), _draft_chunk("chunk-v2", 2)]
    )
    await chunk_session.flush()

    await store.delete_by_document_id(chunk_session, DOCUMENT_ID, tenant_id=TENANT_ID)

    rows = (await chunk_session.execute(select(Chunk.id))).all()
    assert rows == []


# --------------------------------------------------------------- 索引编排的写入契约


class _FakeSession:
    """索引编排只用到 ``commit`` / ``rollback``；这里只记录次数，不碰数据库。

    票 #132 起流水线跑在 ``AsyncSession`` 上，替身因此是异步面。
    """

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0
        #: ``add`` 进来的行（首个版本行，#175）。真会话里 ``add`` 是同步的。
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> Any:
        """建关系边会读一次「本版本的节目录」（#152）；这个替身表示空版本。"""

        return ZERO_COUNT


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
        self.purged: dict[str, Any] | None = None

    async def delete_by_document_id(
        self,
        _db: Any,
        _document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        self.calls.append("purge")
        self.purged = {
            "tenant_id": tenant_id,
            "index_version": index_version,
            "doc_version": doc_version,
        }

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
        current_version=1,
        source_filename="售后手册.md",
        # 真行上一定有这一列（首个版本行会把它抄进版本行，#175）。
        source_file_path=None,
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

    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "update_status", _update_status)
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: ParsedDocument(content="raw", source_type="markdown"),
    )

    # 写入目标版本来自库上的生效版本（#142）；本文件的会话替身只做「不落库」那件事，
    # 于是把这一处协作者也换成固定值。版本解析本身见 ``tests/test_index_build_writes.py``。
    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return DEFAULT_INDEX_VERSION

    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
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


# ------------------------------------------------- 重建正文来自版本表（#173 / ADR-0005 D6）


class _TextRecordingSplitter:
    """记录「切的是哪段正文」的切块器：正文来源是这一票要断言的东西。"""

    def __init__(self, pieces: list[SplitPiece]) -> None:
        self.pieces = pieces
        self.inputs: list[str] = []

    def split(self, text: str) -> list[SplitPiece]:
        self.inputs.append(text)
        return self.pieces


def _document(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": DOCUMENT_ID,
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "title": "错误码查询",
        "content": "旧稿正文：E1024 表示证书过期。",
        "source_type": "markdown",
        "current_version": 2,
        "source_filename": "售后手册.md",
        # 真行上一定有这一列（首个版本行会把它抄进版本行，#175）。
        "source_file_path": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _version_row(version: int, content: str) -> DocumentVersion:
    return DocumentVersion(
        id=f"ver-{version}",
        document_id=DOCUMENT_ID,
        version=version,
        content=content,
        checksum="0" * 64,
    )


def _patch_rebuild_collaborators(
    monkeypatch: pytest.MonkeyPatch, document: SimpleNamespace
) -> None:
    """把索引编排的协作者换成替身：只留下「正文从哪儿来」这一处真的逻辑。"""

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(*_a: Any, **_k: Any) -> bool:
        return True

    async def _update_status(*_a: Any, **_k: Any) -> None:
        return None

    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return DEFAULT_INDEX_VERSION

    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "update_status", _update_status)
    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )


def test_rebuild_takes_the_body_from_the_version_row_not_the_document_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文档行是旧稿、版本表是新稿：切的是**版本表**那一份（ADR-0005 D6）。

    指针与正文的真值分家正是这一票要堵的洞：``documents.content`` 只是热路径的快照，
    重建、重解析、回滚一律从版本行读正文。这里把两处写成不同内容，切块器收到哪一份
    就说明了真值在哪一份。
    """

    _patch_rebuild_collaborators(monkeypatch, _document(current_version=2))
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("有版本行时不该再读文档行快照")),
    )
    splitter = _TextRecordingSplitter(
        [SplitPiece(text="新稿正文：E1024 表示证书已过期。", metadata={"heading_path": "错误码"})]
    )
    store = _RecordingStore()
    session = AsyncRecordingSession(
        rows={DocumentVersion: [_version_row(2, "新稿正文：E1024 表示证书已过期。")]}
    )

    service = IndexingService(splitter=splitter, embeddings=_StubEmbeddings(), stores=[store])
    asyncio.run(service.index_existing_document(session, DOCUMENT_ID, doc_version=2))

    assert splitter.inputs == ["新稿正文：E1024 表示证书已过期。"]
    assert store.written[0][0]["content"] == "新稿正文：E1024 表示证书已过期。"
    assert store.written[0][0]["doc_version"] == 2


def test_rebuild_falls_back_to_the_document_row_when_the_draft_has_no_version_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有版本行时仍走文档行正文：迁移之后、建版本的写路径落地之前的老数据不能炸。

    #175 起每次改稿都会写一行版本行，到那时这条兜底路径只剩历史数据与兜底脚本会走；
    它保留的理由是「读不到真值时不猜」——宁可读那份与指针同源的快照，也不能因为
    一行历史数据缺失让整篇文档永远索引不了。
    """

    _patch_rebuild_collaborators(monkeypatch, _document(current_version=2))
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda doc, *, reparse=False: ParsedDocument(
            content=doc.content, source_type="markdown", metadata={"from": "db"}
        ),
    )
    splitter = _TextRecordingSplitter(
        [SplitPiece(text="旧稿正文：E1024 表示证书过期。", metadata={"heading_path": "错误码"})]
    )
    store = _RecordingStore()

    service = IndexingService(splitter=splitter, embeddings=_StubEmbeddings(), stores=[store])
    asyncio.run(service.index_existing_document(AsyncRecordingSession(), DOCUMENT_ID))

    assert splitter.inputs == ["旧稿正文：E1024 表示证书过期。"]


def test_rebuild_rejects_an_explicit_draft_that_has_no_version_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式指定了第 1 稿、而指针已经走到第 2 稿、库里也没有第 1 稿的版本行：拒绝。

    这就是「迟到任务写错稿」的那一格：指针在它到达之后前移了，文档行里现在装的是
    第 2 稿。若这时回落去读文档行，写出来的切片会**带着第 1 稿的编号装着第 2 稿的
    正文**——比直接失败难查得多。错误码复用既有的「文档不存在」（#175 才加「稿不存在」）。
    """

    _patch_rebuild_collaborators(monkeypatch, _document(current_version=2))
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("指定了过期稿次时不该静默读文档行")),
    )
    store = _RecordingStore()

    service = IndexingService(
        splitter=_StubSplitter([]), embeddings=_StubEmbeddings(), stores=[store]
    )

    with pytest.raises(AppError) as caught:
        asyncio.run(
            service.index_existing_document(AsyncRecordingSession(), DOCUMENT_ID, doc_version=1)
        )

    assert caught.value.code is ErrorCode.NOT_FOUND
    assert store.written == [], "一行切片都不许写"


def test_the_draft_from_the_payload_wins_over_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指针已切回第 1 稿，而载荷说的是第 2 稿：这次索引仍算第 2 稿（#173）。

    这就是「迟到的任务」那一格：任务在指针前移之前投递、在回滚之后才被消费。正文、
    切片稿号、清除范围三者都必须跟着**载荷**走——只要有一处去读指针，就会写出一批
    「标着第 1 稿、装着第 2 稿正文」的切片，或者把第 1 稿的切片一起删掉。
    """

    _patch_rebuild_collaborators(monkeypatch, _document(current_version=1))
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("带稿号的重建不该读文档行快照")),
    )
    splitter = _TextRecordingSplitter(
        [SplitPiece(text="第 2 稿正文：证书有效期 90 天。", metadata={"heading_path": "错误码"})]
    )
    store = _RecordingStore()
    session = AsyncRecordingSession(
        rows={DocumentVersion: [_version_row(2, "第 2 稿正文：证书有效期 90 天。")]}
    )

    service = IndexingService(splitter=splitter, embeddings=_StubEmbeddings(), stores=[store])
    asyncio.run(service.index_existing_document(session, DOCUMENT_ID, doc_version=2))

    assert splitter.inputs == ["第 2 稿正文：证书有效期 90 天。"]
    assert store.written[0][0]["doc_version"] == 2, "切片稿号跟载荷走"
    assert store.purged is not None and store.purged["doc_version"] == 2, "清除范围也跟载荷走"


@pytest.mark.asyncio
async def test_reindexing_the_second_draft_keeps_the_first_drafts_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真 SQL 上跑一次 v2 重建：v1 的切片仍在、内容一字未变（ADR-0005 D4 选 B）。

    这是「每稿各留切片」的正面证据：清除按「文档 + 稿次」收窄，写入另起一组行。少了这
    一条，回滚到第 1 稿时那些切片已经没了——而「回滚可秒级」正是选 B 的理由。
    """

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        # chunks 用 SQLite 能接受的等价 DDL（JSONB / Vector 见本模块文档）；
        # 版本表直接借 ORM 建——它没有 SQLite 不认识的列类型。
        await connection.execute(text(CHUNKS_DDL))
        await connection.run_sync(DocumentVersion.__table__.create)
    try:
        async with AsyncSession(engine) as session:
            store = PgVectorStore()
            # 第 1 稿的切片已经写好了（线上正在用的那一版）。
            await store.add_chunks(session, [_draft_chunk("chunk-v1", 1)])
            session.add(_version_row(2, "第 2 稿正文：证书有效期 90 天。"))
            await session.commit()

            _patch_rebuild_collaborators(monkeypatch, _document(current_version=1))
            splitter = _TextRecordingSplitter(
                [
                    SplitPiece(
                        text="第 2 稿正文：证书有效期 90 天。", metadata={"heading_path": "错误码"}
                    )
                ]
            )
            service = IndexingService(
                splitter=splitter, embeddings=_StubEmbeddings(), stores=[store]
            )

            await service.index_existing_document(session, DOCUMENT_ID, doc_version=2)

            rows = (
                await session.execute(
                    select(Chunk.doc_version, Chunk.content).order_by(Chunk.doc_version)
                )
            ).all()
            assert [(row.doc_version, row.content) for row in rows] == [
                (1, ORIGINAL_CONTENT),
                (2, "第 2 稿正文：证书有效期 90 天。"),
            ], "v1 原样留着，v2 另起一组"
    finally:
        await engine.dispose()
