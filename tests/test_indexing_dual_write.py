"""索引双写与按文档删除：向量存储与关键词检索必须被同一份代码遍历。

接缝是既有的**构造注入**：``IndexingService`` 的所有依赖都从构造参数进来，
测试注入记录型替身即可，不需要网络、Broker 或数据库。默认接线单独测一条
——因为「以后加第三个存储只往列表里塞一个元素」这句话，只有在默认接线里
真的有两个元素时才成立。

本文件同时钉住票 23 的两条口径：

* 关键词文档吃的是**检索文本**（与送进 embedding 的文本一字不差），
  切片原文仍原样留在 chunk 字典里；
* 索引阶段**不降级**：关键词一侧写失败就是整单失败，状态不允许变成成功。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.providers.parsers.base import ParsedDocument
from app.repositories import document_repository
from app.services import indexing_service
from app.services.chunk_store import KeywordSearchChunkStore, build_chunk_stores
from app.services.indexing_service import IndexingService
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece
from tests import fake_db
from tests.fake_db import RecordingSession

TENANT_ID = "tenant_demo"
KB_ID = "kb-1"
DOC_ID = "doc-1"
ORIGINAL = "退款申请流程：提交工单后 3 个工作日内到账。"
HEADING = "退款 > 流程"
RETRIEVAL_TEXT = f"文档：售后手册 章节：{HEADING} 正文：{ORIGINAL}"


# ─────────────────────────────────────────────────────────────────── 替身


class _FakeSession:
    """最小**异步**会话替身（票 #132 起流水线跑在 ``AsyncSession`` 上）。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None

    async def delete(self, _row: Any) -> None:
        return None


class _RecordingStore:
    """向量存储替身：记录 purge / add 的顺序。"""

    def __init__(self, calls: list[str], name: str = "vector") -> None:
        self.calls = calls
        self.name = name
        self.deleted: list[tuple[str, str]] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str) -> None:
        self.calls.append(f"purge:{self.name}")
        self.deleted.append((document_id, tenant_id))

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append(f"add:{self.name}")
        self.written.append(chunks)


class _RecordingKeywordProvider(NoopKeywordSearchProvider):
    """关键词 Provider 替身：记录收到的 chunk 字典与删除调用。"""

    def __init__(self, calls: list[str], *, fail_on_add: bool = False) -> None:
        self.calls = calls
        self.written: list[list[dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_on_add = fail_on_add

    @property
    def available(self) -> bool:
        return True

    async def add_chunks(self, chunks: list[dict]) -> None:
        self.calls.append("add:keyword")
        if self.fail_on_add:
            raise RuntimeError("elasticsearch unavailable")
        self.written.append(chunks)

    async def delete_by_document_id(self, document_id: str, *, tenant_id: str) -> None:
        self.calls.append("purge:keyword")
        self.deleted.append((document_id, tenant_id))


class _StubEmbeddings:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.1] * 1536 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _StubSplitter:
    def __init__(self, pieces: list[SplitPiece]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[SplitPiece]:
        return self.pieces


def _patch_indexing(monkeypatch: pytest.MonkeyPatch, *, pieces: list[SplitPiece]) -> None:
    document = SimpleNamespace(
        id=DOC_ID, tenant_id=TENANT_ID, kb_id=KB_ID, title="售后手册", content="raw", doc_version=1
    )

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(document_repository, "get_by_id_async", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "update_status", _record_update_status)
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: ParsedDocument(content="raw", source_type="markdown"),
    )


_STATUS_CALLS: list[tuple[int, str | None]] = []


async def _record_update_status(
    _db: Any, _target: Any, status: int, error: str | None = None
) -> None:
    _STATUS_CALLS.append((status, error))


@pytest.fixture(autouse=True)
def _clear_status_calls() -> None:
    _STATUS_CALLS.clear()


# ─────────────────────────────────────── 写入契约：向量与关键词同形状


def test_keyword_store_satisfies_the_same_write_contract() -> None:
    """关键词 Provider 不需要数据库会话，适配器把它对齐到切片写入契约。"""

    calls: list[str] = []
    provider = _RecordingKeywordProvider(calls)
    store = KeywordSearchChunkStore(provider)

    chunks = [{"id": "chunk-1", "content": ORIGINAL, "retrieval_text": RETRIEVAL_TEXT}]
    asyncio.run(store.add_chunks(_FakeSession(), chunks))
    asyncio.run(store.delete_by_document_id(_FakeSession(), DOC_ID, tenant_id=TENANT_ID))

    assert provider.written == [chunks]
    assert provider.deleted == [(DOC_ID, TENANT_ID)]
    assert calls == ["add:keyword", "purge:keyword"]


def test_build_chunk_stores_pairs_vector_store_with_keyword_provider() -> None:
    """默认接线的形状：两个元素，顺序是「向量在前、关键词在后」。"""

    calls: list[str] = []
    vector = _RecordingStore(calls)
    provider = _RecordingKeywordProvider(calls)

    stores = build_chunk_stores(vector, provider)

    assert len(stores) == 2
    assert stores[0] is vector
    assert isinstance(stores[1], KeywordSearchChunkStore)


# ─────────────────────────────────────────── 索引编排：默认接线就是双写


def test_indexing_writes_to_both_stores_through_the_same_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认接线跑一遍：两处都先删后写，且关键词文档吃的是检索文本。"""

    calls: list[str] = []
    pieces = [SplitPiece(text=ORIGINAL, metadata={"heading_path": HEADING})]
    _patch_indexing(monkeypatch, pieces=pieces)

    vector = _RecordingStore(calls)
    provider = _RecordingKeywordProvider(calls)
    monkeypatch.setattr(indexing_service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(indexing_service, "get_keyword_search_provider", lambda: provider)

    embeddings = _StubEmbeddings()
    service = IndexingService(splitter=_StubSplitter(pieces), embeddings=embeddings)

    count = asyncio.run(service.index_existing_document(_FakeSession(), DOC_ID))

    assert count == 1
    assert [call for call in calls if call.startswith(("purge:", "add:"))] == [
        "purge:vector",
        "purge:keyword",
        "add:vector",
        "add:keyword",
    ], "先删完两处，再开始写"
    assert vector.deleted == [(DOC_ID, TENANT_ID)]
    assert provider.deleted == [(DOC_ID, TENANT_ID)]

    written = provider.written[0]
    assert len(written) == 1
    assert written[0]["retrieval_text"] == RETRIEVAL_TEXT
    assert embeddings.texts == [RETRIEVAL_TEXT], "两路必须吃同一份文本"
    assert written[0]["content"] == ORIGINAL, "chunk 里的 content 仍是切片原文"
    assert written[0]["document_id"] == DOC_ID
    assert written[0]["kb_id"] == KB_ID
    assert written[0]["tenant_id"] == TENANT_ID
    assert written[0]["index_version"] == "v1"
    assert written[0]["meta"]["title"] == "售后手册"


def test_indexing_fails_as_a_whole_when_the_keyword_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引阶段不降级：关键词写失败 → 抛出，状态绝不打成成功。"""

    calls: list[str] = []
    pieces = [SplitPiece(text=ORIGINAL, metadata={"heading_path": HEADING})]
    _patch_indexing(monkeypatch, pieces=pieces)

    provider = _RecordingKeywordProvider(calls, fail_on_add=True)
    monkeypatch.setattr(indexing_service, "get_vector_store", lambda: _RecordingStore(calls))
    monkeypatch.setattr(indexing_service, "get_keyword_search_provider", lambda: provider)

    service = IndexingService(splitter=_StubSplitter(pieces), embeddings=_StubEmbeddings())

    with pytest.raises(RuntimeError, match="elasticsearch unavailable"):
        asyncio.run(service.index_existing_document(_FakeSession(), DOC_ID))

    assert [code for code, _ in _STATUS_CALLS] == [], "失败时不许回写 SUCCESS"
    assert "add:keyword" in calls


# ───────────────────────────────── 删除文档：两处存储都要清干净（ASGI 接缝）


def test_deleting_a_document_purges_both_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE /documents/{id} 遍历同一份存储列表，关键词一侧同样被清空。"""

    main = importlib.import_module("app.main")
    routes = importlib.import_module("app.api.routes.documents")

    calls: list[str] = []
    # 删除路由自票 #130 起在异步栈上：用共享替身 + ``override_all_db``（四个入口一次换全），
    # 别再养一个只服务这个用例的本地假会话。
    session = RecordingSession()
    vector = _RecordingStore(calls)
    provider = _RecordingKeywordProvider(calls)
    document = SimpleNamespace(
        id=DOC_ID,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="售后手册",
        doc_version=1,
        source_type="text",
        source_file_path=None,
        status=DocumentStatus.SUCCESS,
        index_claimed_at=None,
    )

    async def _lookup(_db: Any, _id: str, *, tenant_id: str) -> Any:
        return document

    monkeypatch.setattr(document_repository, "get_by_id_for_tenant_async", _lookup)
    monkeypatch.setattr(routes, "get_vector_store", lambda: vector)
    monkeypatch.setattr(routes, "get_keyword_search_provider", lambda: provider)
    monkeypatch.setattr(routes, "remove_document_file", lambda _path: calls.append("remove-file"))

    fake_db.override_all_db(main.app, session)
    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            response = client.delete(f"/api/v1/documents/{DOC_ID}")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert calls[:2] == ["purge:vector", "purge:keyword"]
    assert vector.deleted == [(DOC_ID, TENANT_ID)]
    assert provider.deleted == [(DOC_ID, TENANT_ID)]
