"""重建索引的跨层回归：``POST /documents/{id}/reindex`` 之后 Worker 必须真的能索引。

这条测试守住 Finding 0 的用户可见症状：路由与 Worker 都调用 ``try_claim_for_indexing``
时，路由先把租约盖成新鲜的，Worker 的 CAS 于是 ``rowcount=0`` 并「成功」地返回 0；
调用方看到 ``status=3``，切片却永远不长。现在的契约是**路由只准备入队**
（``prepare_for_indexing``，不盖租约），**Worker 是唯一抢占者**。

接缝：ASGI 边界发一次真实 reindex 请求；投递缝换成记录器；随后在同一个内存
SQLite 库上跑真实的 ``IndexingService.index_existing_document``，只把切块器 /
embedding / 向量存储换成离线替身。不连 PostgreSQL、不发网络请求。

**两条会话来自两套栈**（票 #132）：reindex 路由仍是同步会话（随票 #130 搬），Worker
流水线已经是 ``AsyncSession``——同一个库文件上各开一个引擎，前后串行，所以「路由只准备
入队、Worker 才抢租约」这件事仍然是在真 SQL 上验的。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.models.document import Document
from app.repositories import knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece

TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
DOC_ID = "doc-reindex-e2e"
REQUEST_ID = "reindex-e2e-req"

PIECES = [
    SplitPiece(text="first piece", metadata={"heading_path": "Intro"}),
    SplitPiece(text="second piece", metadata={"heading_path": "Intro"}),
]


class _StubEmbeddings:
    """固定向量：数量与切片一一对应，绝不发模型请求。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * 3 for index in range(len(texts))]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _StubSplitter:
    def split(self, _text: str) -> list[SplitPiece]:
        return list(PIECES)


class _RecordingStore:
    """记录 purge / add：调用顺序即「先删后写」，且必须带租户。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.tenant_ids: list[str] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str, index_version: str | None = None) -> None:
        self.calls.append("purge")
        self.deleted.append(document_id)
        self.tenant_ids.append(tenant_id)

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append("add")
        self.written.append(chunks)

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("索引链路不应调用检索")


@pytest.fixture
def reindex_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """真实 reindex 路由（临时文件 SQLite documents 表）+ 离线 Worker 装配。

    请求一路是**异步会话**（票 #130：reindex 路由只剩这一套栈），Worker 流水线也是
    ``AsyncSession``——同一个库文件上各开一个引擎，前后串行。种子数据用同步会话写进去
    （建表 + 一行），随后**释放**它：之后每一次读都另开连接，读到的才是请求 / Worker
    真正提交过的状态，而不是种子那一笔的旧快照。
    """

    from app.db.async_session import get_async_db

    main = importlib.import_module("app.main")
    routes = importlib.import_module("app.api.routes.documents")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    database_path = tmp_path / "reindex.db"

    seed_engine = create_engine(f"sqlite:///{database_path}")
    Document.__table__.create(seed_engine)
    with sessionmaker(bind=seed_engine)() as seed_session:
        seed_session.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title="Reindex me",
                content="body text for reindexing",
                source_type="text",
                status=DocumentStatus.SUCCESS,
            )
        )
        seed_session.commit()
    seed_engine.dispose()

    # 请求级的会话工厂绑在同一个库文件上：每个请求一个新会话，用完即关。
    request_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    request_sessions = async_sessionmaker(bind=request_engine, expire_on_commit=False)

    async def _request_session() -> Any:
        async with request_sessions() as session:
            yield session

    enqueued: list[dict[str, Any]] = []
    store = _RecordingStore()
    monkeypatch.setattr(routes, "enqueue_document", lambda **kwargs: enqueued.append(kwargs))

    # 写入目标版本来自库上的生效版本（#142）。这条用例的临时库里只有 ``documents``，
    # 于是把「问库要版本」这一步换成固定的 v1——与迁移回填、索引流水线的老行为同值。
    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return DEFAULT_INDEX_VERSION

    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )

    main.app.dependency_overrides[get_async_db] = _request_session
    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                enqueued=enqueued,
                store=store,
                database_path=database_path,
            )
    finally:
        main.app.dependency_overrides.clear()
        asyncio.run(request_engine.dispose())


def _read_document(database_path: Path) -> SimpleNamespace | None:
    """另开一条连接读这一行：请求与 Worker 都已提交，读到的才是它们写的状态。

    返回的是**快照**而不是 ORM 对象：会话一关，那个对象就是游离的，而这条用例只关心
    三个字段。
    """

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(engine) as session:
            doc = session.get(Document, DOC_ID)
            if doc is None:
                return None
            return SimpleNamespace(
                status=doc.status,
                error_message=doc.error_message,
                index_claimed_at=doc.index_claimed_at,
            )
    finally:
        engine.dispose()


async def _run_worker_pipeline(service: IndexingService, database_path: Path) -> int:
    """在**异步**引擎上跑真实流水线（Worker 那一侧的会话类型），跑完即 dispose。"""

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with session_factory() as async_session:
            return await service.index_existing_document(async_session, DOC_ID)
    finally:
        await engine.dispose()


def test_reindex_route_then_worker_produces_chunks_and_success(
    reindex_env: SimpleNamespace,
) -> None:
    """force 重建：路由返回 3，Worker 随后必须抢到租约并写入切片、落到 SUCCESS。"""

    response = reindex_env.client.post(
        f"/api/v1/documents/{DOC_ID}/reindex",
        params={"force": "true"},
        headers={"X-Request-ID": REQUEST_ID},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {
        "document_id": DOC_ID,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "reparse": False,
    }
    assert reindex_env.enqueued == [
        {
            "document_id": DOC_ID,
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": REQUEST_ID,
            "reparse": False,
        }
    ]

    doc = _read_document(reindex_env.database_path)
    assert doc is not None
    assert doc.status == DocumentStatus.PROCESSING
    assert doc.index_claimed_at is None, "路由只准备入队，绝不盖租约"

    service = IndexingService(
        splitter=_StubSplitter(),
        embeddings=_StubEmbeddings(),
        stores=[reindex_env.store],
    )
    count = asyncio.run(_run_worker_pipeline(service, reindex_env.database_path))

    assert count == len(PIECES), "Worker 必须真的抢到索引权并写入切片"
    assert reindex_env.store.calls == ["purge", "add"], "先删旧切片，再写新切片"
    assert reindex_env.store.deleted == [DOC_ID]
    assert reindex_env.store.tenant_ids == [TENANT_ID]
    assert len(reindex_env.store.written[0]) == len(PIECES)

    doc = _read_document(reindex_env.database_path)
    assert doc is not None
    assert doc.status == DocumentStatus.SUCCESS
    assert doc.error_message is None
    assert doc.index_claimed_at is not None
