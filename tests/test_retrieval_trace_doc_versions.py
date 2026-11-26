"""#179 · 归属索引记下「这次命中的是哪几稿」。

用户故事第 3 条要的是一句事后能回答的话：「那次查的是第几稿」——**即使那一稿的切片后来
被保留策略清掉**（#178 会真的删掉旧稿的切片与版本行）。`chunk_ids` 只能回答「命中过哪些
片」，片被清掉之后就只剩一串没有意义的 id；所以这一列存的是**文档 → 稿号**：

```text
doc_versions = {"doc-1": 2, "doc-2": 1}
```

形状与既有的 `index_versions`（`{kb_id: 版本}`）**完全同形**，理由也同一条（ADR-0010 §5）：
两个平行数组（`document_ids` 一个、`versions` 一个）拼得出「A 文档配 B 文档稿号」这种不存在
的组合，而且不报错。稿号是整数，映射的键是文档 id——都是结构，不是原文，ADR-0021「只存
结构」的红线不受影响。

三条要守的东西：

1. **列的形状**：JSONB 映射、可空（迁移之前的老行没有稿号可记）、不与平行数组并存；
2. **写入**：每次检索都写，且写的就是**交付单元上那个真实列读出来的稿号**（#174）；
3. **历史行为不变**：老行是 NULL，读取方不必为「这一列有没有值」写分支。

数据库一律替身、Provider 一律离线：整套不连 PostgreSQL、不连 ES。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models.usage_event import UsageEvent
from app.services import trace_identity
from tests.fake_db import AsyncRecordingSession, AsyncSessionAdapter, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

#: 一次检索里交付的两个单元（折叠之后每单元代表一节，形状与 `collapse_results` 一致：
#: `document_id` 与 `doc_version` 都是交付单元上的真实字段，#174 起稿号来自 `chunks.doc_version`）。
DELIVERED_UNITS = [
    {"chunk_id": "chunk-1", "document_id": "doc-1", "doc_version": 2, "source_chunk_ids": []},
    {"chunk_id": "chunk-2", "document_id": "doc-1", "doc_version": 2, "source_chunk_ids": []},
    {"chunk_id": "chunk-3", "document_id": "doc-2", "doc_version": 1, "source_chunk_ids": []},
]

TENANT_ID = "tenant_docver"
KB_ID = "kb-docver"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
IDENTITY = RetrievalIdentity(TENANT_ID)


# ─────────────────────────────────────────────────────────────────── 列的形状


def test_the_trace_table_adds_a_doc_versions_mapping_column() -> None:
    """第十二列 `doc_versions`：**JSONB 映射**，可空（老行没有稿号可记）。"""

    column = UsageEvent.__table__.columns["doc_versions"]

    assert str(column.type) == "JSONB", "映射必须是一个 JSON 对象，不是两个数组"
    assert column.nullable is True, "迁移之前写下的行没有稿号可记（那时稿号恒为 1）"


def test_the_column_is_named_for_the_draft_number_inside_the_mapping() -> None:
    """列名是 `doc_versions`（映射），与「当前稿指针」`current_version`、切片的
    `chunks.doc_version` 都不是同一件事——三个名字各说各的事（术语表「文档版本」）。"""

    columns = {column.key for column in UsageEvent.__table__.columns}

    assert "doc_versions" in columns
    assert "document_versions" not in columns, "别与版本表 `document_versions` 撞名"
    assert "current_version" not in columns, "指针是文档行上的东西，不是这一行的"


# ─────────────────────────────────────────────── 派生：从交付单元算出这份映射


def test_the_mapping_pairs_each_document_with_the_draft_it_delivered() -> None:
    """`{document_id: doc_version}`：同一篇文档的两条单元折进一个键，不是两条记录。

    「一次检索命中两节，两节都来自同一篇文档的第 2 稿」是最常见的形态；把它记成
    两条（或两个数组里的两个位置）只会让「这篇文档命中的是哪一稿」变得要人去拼。
    """

    versions = trace_identity.delivered_doc_versions(DELIVERED_UNITS)

    assert versions == {"doc-1": 2, "doc-2": 1}
    assert isinstance(versions, dict), "配对关系只能从映射里读出来（ADR-0010 §5）"
    assert all(isinstance(value, int) for value in versions.values()), "值就是稿号本身"


def test_a_unit_without_a_draft_number_is_skipped_rather_than_guessed() -> None:
    """回查不到稿号（这一片已被重建替换）的单元**不猜**：那一行已经写不出它属于第几稿。

    如实记成 `null`（#174 的口径）之后，这里能做的最好的事就是**不写**这个键——
    猜一个「当前稿」正是对账脚本要抓的那种静默错归因。
    """

    units = [
        {"document_id": "doc-gone", "doc_version": None},
        {"document_id": "doc-1", "doc_version": 3},
        {"doc_version": 4},
        {},
    ]

    assert trace_identity.delivered_doc_versions(units) == {"doc-1": 3}
    assert trace_identity.delivered_doc_versions([]) == {}


# ────────────────────────────────────────────── 写入：服务层（独立短事务替身）


def _record_trace(session: Any, **overrides: Any) -> list[UsageEvent]:
    """跑一次写入（走真实的 `record_retrieval_trace`），返回写进去的行。"""

    import asyncio

    factory_mod = importlib.import_module("app.db.session_factory")
    service = importlib.import_module("app.services.retrieval_trace")

    payload = {
        "log_id": "log-docver",
        "trace_id": "trace-docver",
        "tenant_id": TENANT_ID,
        "kb_ids": [KB_ID],
        "index_versions": {KB_ID: "v1"},
        "doc_versions": {"doc-1": 2},
        "chunk_ids": ["chunk-1"],
        "top_k": 1,
        "latency_ms": 7,
        "degraded_reason": None,
        "empty_reason": None,
    }
    payload.update(overrides)

    factory_mod.set_async_session_factory(lambda: session)
    try:
        asyncio.run(service.record_retrieval_trace(**payload))
    finally:
        factory_mod.set_async_session_factory(None)
    return session.added_of(UsageEvent)


def test_the_write_path_persists_the_draft_mapping() -> None:
    """写入方把**已经算好的**映射原样落库：它不做判定，也不改口径。"""

    rows = _record_trace(AsyncRecordingSession())

    assert len(rows) == 1
    assert rows[0].doc_versions == {"doc-1": 2}
    assert rows[0].index_versions == {KB_ID: "v1"}, "两个映射列各存各的，不许互相串"


def test_an_empty_mapping_is_written_as_an_empty_object() -> None:
    """这一次一条都没命中 → `{}`（「查过、什么都没给」），不是 NULL（那是「老行」）。"""

    rows = _record_trace(AsyncRecordingSession(), doc_versions={})

    assert rows[0].doc_versions == {}
    assert rows[0].doc_versions is not None


# ─────────────────────────────────────────────── ASGI：一次检索真的写下这份映射
#
# 这一段跑的是真实路由：向量库替身交回两片（分属两篇文档、稿号不同），溯源回查把
# `chunks.doc_version` 带回来（#174 起的唯一来源），于是「写入的映射」是不是**交付单元
# 上那个真实列**读出来的，可以从库里那一行直接读到。


class _MetaRow:
    """`_meta_statement` 那一行的最小替身：列读法与 SQLAlchemy 的 `Row` 同形。"""

    def __init__(self, kb_id: str, chunk_id: str, doc_version: int) -> None:
        self.kb_id = kb_id
        self.id = chunk_id
        self.meta = {"chunk_type": "prose"}
        self.doc_version = doc_version


class _Result:
    def __init__(self, rows: list[Any], count: int = 0) -> None:
        self._rows = rows
        self._count = count

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> int:
        return self._count


class _RequestDb:
    """请求会话替身：溯源回查给出这两片（带稿号），空结果计数给 1，别的读一律空。

    ``execute`` 是**同步**的：它被 ``AsyncSessionAdapter`` 包一层之后才是异步面
    （适配器内部 ``return self._inner.execute(...)``，不会 await 它）。
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def execute(self, statement: Any) -> _Result:
        sql = str(statement).lower()
        if "count(" in sql:
            return _Result([], 1)
        if "doc_version" in sql:  # 溯源回查（只有它同时选 kb_id / id / metadata / doc_version）
            return _Result(self._rows)
        return _Result([])

    def close(self) -> None:
        pass


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    async def similarity_search_scope(
        self, _db: Any, _vector: Any, *, tenant_id: str, index_versions: dict, top_k: int
    ) -> list[dict]:
        (kb_id,) = index_versions
        return [{**row, "kb_id": kb_id} for row in self._rows[:top_k]]


HITS = [
    {
        "document_id": "doc-1",
        "chunk_id": "chunk-a",
        "title": "售后手册",
        "content": "甲",
        "score": 0.9,
    },
    {
        "document_id": "doc-2",
        "chunk_id": "chunk-b",
        "title": "部署指南",
        "content": "乙",
        "score": 0.8,
    },
]
META_ROWS = [_MetaRow(KB_ID, "chunk-a", 3), _MetaRow(KB_ID, "chunk-b", 1)]


@contextmanager
def _offline_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncRecordingSession]:
    """离线 ASGI 客户端；归属索引那条旁路走**独立的**记录替身（与请求会话刻意分开）。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    factory_mod = importlib.import_module("app.db.session_factory")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    trace_session = AsyncRecordingSession()
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore(HITS))
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)
    factory_mod.set_async_session_factory(lambda: trace_session)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        _RequestDb(META_ROWS)
    )
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            IDENTITY.use(mode="vector", top_k=2, include_parent=False)
            response = client.post(RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": "退款多久到账"})
            assert response.status_code == 200
            body = response.json()
            assert body["code"] == 0, body
            yield trace_session
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_async_session_factory(None)


def test_a_retrieval_records_the_draft_of_every_document_it_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**验收：每次检索写入。** 两篇文档、两个稿号 → 库里那一行就是这条映射。

    值来自交付单元上的真实列（`chunks.doc_version`，#174），不是「当前稿」这种应用层
    假设：这一列的全部意义是事后能回答「那次查的是第几稿」，猜一个就等于没有。
    """

    with _offline_client(monkeypatch) as session:
        rows = session.added_of(UsageEvent)

    assert len(rows) == 1, "一次检索一行"
    assert rows[0].doc_versions == {"doc-1": 3, "doc-2": 1}
    assert rows[0].chunk_ids == ["chunk-a", "chunk-b"]
    assert session.commits == 1, "映射跟着同一笔独立短事务落库"
