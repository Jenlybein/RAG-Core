"""#142 · 索引写入带目标版本：构建写新版本，线上那一版原样不动。

接缝仍是**构造注入**（``IndexingService`` 的全部依赖从构造参数进来）+ 构建服务本身：

* ``IndexingService.index_existing_document``：写入目标版本的来源（调用方给的 → 库上的
  生效版本）、以及「purge 只清目标版本」；
* ``IndexBuildService.build``：逐文档重建到目标版本，失败落 ``failed``；
* ``resolve_index_versions``：构建跑完之后检索仍 pin 在旧版本上（**不带任何降级标记**：
  它压根不知道有构建这回事）。

数据库是 ``AsyncRecordingSession``（会回答 ``get`` / ``select``），Provider 全部替身：
不连 PostgreSQL、不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.chunk import DEFAULT_INDEX_VERSION, Chunk
from app.models.document import Document
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
    resolve_index_versions,
)
from app.models.knowledge_base import KnowledgeBase
from app.providers.parsers.base import ParsedDocument
from app.repositories import document_repository
from app.services import indexing_service
from app.services.index_build_service import BUILD_FAILED_MESSAGE, IndexBuildService
from app.services.indexing_service import IndexingService
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece
from tests.fake_db import AsyncRecordingSession

TENANT_ID = "tenant_writes"
KB_ID = "kb-1"
LIVE_VERSION = "v1"
TARGET_VERSION = "v2"


# ──────────────────────────────────────────────────────────────── 替身


class _StubEmbeddings:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.1] * 4 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _RecordingStore:
    """向量存储替身：记下 purge 带上来的版本与写进去的切片。"""

    def __init__(self) -> None:
        self.purge_versions: list[str | None] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(
        self,
        _db: Any,
        _document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
    ) -> None:
        self.purge_versions.append(index_version)

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.written.append(chunks)


class _StubSplitter:
    def __init__(self, pieces: list[SplitPiece]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[SplitPiece]:
        return self.pieces


class _StubIndexing:
    """构建循环用的流水线替身：记录 (文档, 目标版本, allow_success)。"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []
        self.fail_on = fail_on

    async def index_existing_document(
        self,
        _db: Any,
        document_id: str,
        *,
        index_version: str | None = None,
        allow_success: bool = False,
        reparse: bool = False,
    ) -> int:
        self.calls.append((document_id, index_version, allow_success))
        if document_id == self.fail_on:
            raise RuntimeError("embedding exploded")
        return 1


def _kb(active: str | None = LIVE_VERSION) -> SimpleNamespace:
    return SimpleNamespace(id=KB_ID, tenant_id=TENANT_ID, active_index_version=active)


def _document(document_id: str, *, status: int = DocumentStatus.SUCCESS) -> SimpleNamespace:
    return SimpleNamespace(
        id=document_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="售后手册",
        doc_version=1,
        status=status,
        content="正文",
    )


def _session(
    *,
    kb: Any | None = None,
    documents: list[Any] | None = None,
    versions: list[Any] | None = None,
    chunks: list[Any] | None = None,
) -> AsyncRecordingSession:
    return AsyncRecordingSession(
        rows={
            KnowledgeBase: [kb] if kb is not None else [],
            Document: list(documents or []),
            IndexBuildVersion: list(versions or []),
            Chunk: list(chunks or []),
        }
    )


def _chunk(chunk_id: str, *, version: str = TARGET_VERSION) -> Any:
    """一行切片（真 ORM 对象，不 flush）：给 ``ready`` 前的「切片数」读数用。"""

    return Chunk(
        id=chunk_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        document_id="doc-1",
        content="正文",
        embedding=[0.0] * 1536,
        meta={},
        index_version=version,
    )


class _RecordingVectorStore:
    """向量存储替身：回答某一版本的向量条数（与切片数对齐，构建才能 ready）。"""

    def __init__(self, *, vectors: int) -> None:
        self.vectors = vectors
        self.count_calls: list[str] = []

    async def count_by_scope(
        self, _db: Any, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        self.count_calls.append(index_version)
        return self.vectors


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, *, document: Any) -> list[str]:
    """把流水线的三个外部协作者换成替身；返回「被抢占的文档」记录。"""

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(_db: Any, document_id: str, **kwargs: Any) -> bool:
        claims.append(f"{document_id}:{kwargs.get('allow_success')}")
        return True

    async def _update_status(*_args: Any, **_kwargs: Any) -> None:
        return None

    claims: list[str] = []
    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "update_status", _update_status)
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: ParsedDocument(content="正文", source_type="markdown"),
    )
    return claims


def _pipeline(store: _RecordingStore) -> IndexingService:
    return IndexingService(
        splitter=_StubSplitter(
            [SplitPiece(text="退款到账时间", metadata={"heading_path": "退款"})]
        ),
        embeddings=_StubEmbeddings(),
        stores=[store],
    )


def _build_service(indexing: Any, *, vectors: int = 0) -> IndexBuildService:
    """构建服务：关键词能力缺席（这一维不参与计数），向量数由替身给定。"""

    return IndexBuildService(
        indexing=indexing,
        vector_store=_RecordingVectorStore(vectors=vectors),
        keyword_search=SimpleNamespace(available=False),
    )


# ──────────────────────────────────────────────── 写入目标版本的来源


def test_the_pipeline_writes_into_the_version_the_knowledge_base_is_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没显式给版本时取库上的生效版本：激活 v2 之后再上传的文档必须落进 v2。

    写死 ``v1`` 的症状是「上传成功、检索搜不到」——按 (库, 生效版本) 配对过滤时一条都
    匹配不上，而它看起来像「这个库没内容」（ADR-0025 决策七）。
    """

    document = _document("doc-1")
    _patch_pipeline(monkeypatch, document=document)
    store = _RecordingStore()
    session = _session(kb=_kb("v2"), documents=[document])

    asyncio.run(_pipeline(store).index_existing_document(session, "doc-1"))

    assert [chunk["index_version"] for chunk in store.written[0]] == ["v2"]
    assert store.purge_versions == ["v2"], "先删也只清目标版本"


def test_an_explicit_target_version_wins_over_the_live_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建写的是**它自己的**新版本，与库上还在生效的那一版无关。"""

    document = _document("doc-1")
    _patch_pipeline(monkeypatch, document=document)
    store = _RecordingStore()
    session = _session(kb=_kb("v1"), documents=[document])

    asyncio.run(
        _pipeline(store).index_existing_document(
            session, "doc-1", index_version="v3", allow_success=True
        )
    )

    assert [chunk["index_version"] for chunk in store.written[0]] == ["v3"]
    assert store.purge_versions == ["v3"]


def test_the_pipeline_falls_back_to_the_default_version_when_the_kb_row_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库行读不到（被删 / 跨租户）时按默认版本写，让后续流程报出真正的原因。"""

    document = _document("doc-1")
    _patch_pipeline(monkeypatch, document=document)
    store = _RecordingStore()
    session = _session(kb=None, documents=[document])

    asyncio.run(_pipeline(store).index_existing_document(session, "doc-1"))

    assert [chunk["index_version"] for chunk in store.written[0]] == [DEFAULT_INDEX_VERSION]


# ───────────────────────────────────────────────────── 构建循环：逐文档


def test_the_build_rebuilds_every_success_document_into_the_target_version() -> None:
    """只有 ``SUCCESS`` 的文档参与重建——失败 / 处理中的没有可切的正文。"""

    indexing = _StubIndexing()
    building = IndexBuildVersion(
        id="build-v2",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=TARGET_VERSION,
        status=INDEX_BUILD_BUILDING,
    )
    session = _session(
        kb=_kb(LIVE_VERSION),
        documents=[
            _document("doc-ok-1"),
            _document("doc-failed", status=DocumentStatus.FAILED),
            _document("doc-ok-2"),
            _document("doc-processing", status=DocumentStatus.PROCESSING),
        ],
        versions=[building],
        # 两篇成功文档各产出 1 片：切片数与向量数必须对得上，构建才能 ready。
        chunks=[_chunk("c1"), _chunk("c2")],
    )

    asyncio.run(_build_service(indexing, vectors=2).build(session, "build-v2", tenant_id=TENANT_ID))

    assert [call[0] for call in indexing.calls] == ["doc-ok-1", "doc-ok-2"]
    assert {call[1] for call in indexing.calls} == {TARGET_VERSION}, "不许写回线上那一版"
    assert all(call[2] is True for call in indexing.calls), (
        "重建的输入本来就是成功文档，必须允许抢占它们"
    )
    assert building.status == INDEX_BUILD_READY, "构建跑完到 ready：上线是另一步（激活）"


def test_a_build_that_fails_halfway_records_failed_and_leaves_the_live_version_alone() -> None:
    """失败只落 ``failed`` + 固定原因；库列与旧 active 行原样不动（线上完全不受影响）。"""

    indexing = _StubIndexing(fail_on="doc-2")
    kb = _kb(LIVE_VERSION)
    live = IndexBuildVersion(
        id="build-v1",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=LIVE_VERSION,
        status=INDEX_BUILD_ACTIVE,
    )
    building = IndexBuildVersion(
        id="build-v2",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=TARGET_VERSION,
        status=INDEX_BUILD_BUILDING,
    )
    session = _session(
        kb=kb,
        documents=[_document("doc-1"), _document("doc-2"), _document("doc-3")],
        versions=[live, building],
    )

    with pytest.raises(RuntimeError):
        asyncio.run(_build_service(indexing).build(session, "build-v2", tenant_id=TENANT_ID))

    assert building.status == INDEX_BUILD_FAILED
    assert building.error_message == BUILD_FAILED_MESSAGE
    assert kb.active_index_version == LIVE_VERSION, "生效版本一个字节都不许改"
    assert live.status == INDEX_BUILD_ACTIVE
    assert "doc-3" not in [call[0] for call in indexing.calls], "失败之后不再继续重建"


# ───────────────────────────────── 线上照旧：构建跑完仍 pin 在旧版本上


def test_retrieval_still_resolves_the_old_version_after_a_build_ran() -> None:
    """构建（还没激活）跑完之后，检索仍等价于「查询旧的 active」。

    这是验收里的「构建进行中发起检索，返回旧版本结果」——它不需要任何降级标记：
    检索链路根本不知道构建存在，范围仍从库上的生效版本列解析。
    """

    indexing = _StubIndexing()
    kb = _kb(LIVE_VERSION)
    building = IndexBuildVersion(
        id="build-v2",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=TARGET_VERSION,
        status=INDEX_BUILD_BUILDING,
    )
    session = _session(
        kb=kb, documents=[_document("doc-1")], versions=[building], chunks=[_chunk("c1")]
    )

    asyncio.run(_build_service(indexing, vectors=1).build(session, "build-v2", tenant_id=TENANT_ID))

    assert kb.active_index_version == LIVE_VERSION
    assert dict(resolve_index_versions([kb])) == {KB_ID: LIVE_VERSION}
    assert building.status != INDEX_BUILD_ACTIVE
