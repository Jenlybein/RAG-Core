"""#141 · 检索 pin 在「各库的生效版本」上，配对形状不变。

**接缝只有两个**（父 spec「测试接缝表」：D 票不加新缝）：

* 纯函数 ``resolve_index_versions``：把**本次已经取到的知识库行**解析成
  ``kb_id → 版本`` 的映射；任何一行没有生效版本就拒绝（回填守卫）；
* ASGI ``POST /api/v1/rag/retrieve``：请求进、映射出——向量库收到的就是那份映射，
  归属索引里写下的也是那份映射。

为什么这里断言「向量库收到了什么映射」而不是「SQL 长什么样」：SQL 的配对形状由
``tests/test_vector_scope_search.py`` 守着，本文件守的是**映射的来源与去向**——
来源从常量换成库行（ADR-0025 决策四），去向是内核与归属索引。两边各测一半，
合起来才是「这次查的是哪一次构建」。

数据库一律是替身，Provider 一律离线：整套不连 PostgreSQL、不连 ES、不发网络。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.models.chunk import DEFAULT_INDEX_VERSION
from app.models.index_version import (
    UNRESOLVED_INDEX_VERSION_MESSAGE,
    IndexVersionUnresolved,
    effective_index_version,
    resolve_index_versions,
)
from app.models.retrieval_trace import RetrievalTrace
from app.providers.vectorstores.pgvector import build_scope_filter
from tests.fake_db import AsyncRecordingSession, AsyncSessionAdapter
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_pin"
KB_A = "kb-a"
KB_B = "kb-b"
QUERY = "退款多久到账"

IDENTITY = RetrievalIdentity(TENANT_ID)

#: 服务端状态不一致（回填漏做）时的响应码：``ErrorCode.SERVER_ERROR``。
SERVER_ERROR_CODE = 50000
#: 请求体多带一个字段时的响应码：``ErrorCode.REQUEST_VALIDATION_ERROR``。
VALIDATION_ERROR_CODE = 20002


def _kb(kb_id: str, version: str | None) -> SimpleNamespace:
    """一行知识库：字段与 ``KnowledgeBase`` 对齐（检索只读这几个）。"""

    return SimpleNamespace(
        id=kb_id,
        tenant_id=TENANT_ID,
        name=f"库 {kb_id}",
        description=None,
        settings={},
        active_index_version=version,
    )


def _row(kb_id: str, version: str, chunk_id: str, *, score: float = 0.9) -> dict:
    """一条切片命中：``index_version`` 只用于假存储按配对过滤，不外泄给响应。"""

    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": "退款政策",
        "content": f"{chunk_id} 的正文",
        "score": score,
        "kb_id": kb_id,
        "index_version": version,
    }


class _ScopedVectorStore:
    """离线向量库：**像真实 SQL 一样**只返回落在 ``(kb, version)`` 配对里的行。

    它会记下每次收到的映射——这就是「版本 pin 到哪一次构建」在链路上留下的
    唯一可观察事实。
    """

    def __init__(self, rows: Sequence[dict]) -> None:
        self._rows = list(rows)
        self.scopes: list[dict[str, str]] = []

    async def similarity_search_scope(
        self, _db: Any, _query_vector: Any, *, tenant_id: str, index_versions: dict, top_k: int
    ) -> list[dict]:
        self.scopes.append(dict(index_versions))
        matched = [
            row
            for row in self._rows
            if index_versions.get(str(row["kb_id"])) == row["index_version"]
        ]
        # 版本列是本文件的观察工具，不是响应字段：归属索引与响应契约都不许因此多一个字。
        return [
            {key: value for key, value in row.items() if key != "index_version"}
            for row in matched[:top_k]
        ]

    async def similarity_search(self, *_args: Any, **_kwargs: Any):  # pragma: no cover
        raise AssertionError("检索走范围版，不许按库循环")


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _EmptyResult:
    def all(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return 0

    def scalars(self) -> "_EmptyResult":
        return self


class _FakeRequestDb:
    """请求会话替身：空结果判定那条计数读它（本文件里结果非空，走不到）。"""

    def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    def close(self) -> None:
        pass


@contextmanager
def _offline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kbs: dict[str, str | None],
    rows: Sequence[dict] | None = None,
) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端：知识库行由 ``kbs`` 决定，切片由 ``rows`` 决定。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    factory_mod = importlib.import_module("app.db.session_factory")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    store = _ScopedVectorStore(rows if rows is not None else [])
    trace_session = AsyncRecordingSession()

    async def _lookup(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        if kb_id not in kbs:
            return None
        return _kb(kb_id, kbs[kb_id])

    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: store)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _lookup)
    factory_mod.set_async_session_factory(lambda: trace_session)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        _FakeRequestDb()
    )
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {"client": client, "store": store, "trace_session": trace_session}
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_async_session_factory(None)


def _retrieve(client: TestClient, kb_ids: list[str]) -> dict:
    IDENTITY.use(mode="vector", top_k=2, include_parent=False)
    response = client.post(RETRIEVE_PATH, json={"kb_ids": kb_ids, "query": QUERY})
    assert response.status_code == 200
    return response.json()


# ──────────────────────────────────────────────── 纯函数：映射的形状与缺版本的守卫


def test_resolve_versions_pairs_each_kb_with_its_own_effective_version() -> None:
    """两个库各配自己的版本——不是「这次请求一个版本」，更不是两个平行数组。"""

    resolved = resolve_index_versions([_kb(KB_A, "v2"), _kb(KB_B, "v1")])

    assert dict(resolved) == {KB_A: "v2", KB_B: "v1"}
    assert isinstance(resolved, dict), "配对关系只能从映射里读出来"


def test_resolve_versions_keeps_the_request_order_of_the_knowledge_bases() -> None:
    """顺序跟着本次的行来：范围语句的配对顺序因此是确定的，便于逐字节比对。"""

    resolved = resolve_index_versions([_kb(KB_B, "v2"), _kb(KB_A, "v1")])

    assert list(resolved) == [KB_B, KB_A]


def test_a_kb_without_an_effective_version_is_refused_and_named() -> None:
    """回填守卫：空版本**不**退回 ``v1``、也不跳过——它把这次检索显式拦下并点名。

    退回默认值等于把「没回填」这个状态悄悄抹掉：库的生效版本说空，切片却写在 v1，
    检索会给出一次「本次没命中」的空结果（ADR-0025 决策三 / 决策七）。
    """

    with pytest.raises(IndexVersionUnresolved) as error:
        resolve_index_versions([_kb(KB_A, None), _kb(KB_B, "v1")])

    assert error.value.kb_ids == (KB_A,), "必须点名是哪些库没回填"
    assert error.value.msg == UNRESOLVED_INDEX_VERSION_MESSAGE
    assert error.value.code.name == "SERVER_ERROR", "服务端漏了回填，不是调用方的错"


def test_effective_index_version_treats_blank_as_missing() -> None:
    """空白不是版本名：``''`` 与 ``None`` 同样「没有生效版本」。"""

    assert effective_index_version(_kb(KB_A, None)) is None
    assert effective_index_version(_kb(KB_A, "  ")) is None
    assert effective_index_version(_kb(KB_A, "v2")) == "v2"


def test_the_scope_filter_pairs_each_kb_with_its_resolved_version() -> None:
    """解析出来的映射喂给范围条件后，仍是「每对内部 AND、对与对之间 OR」。"""

    resolved = resolve_index_versions([_kb(KB_A, "v2"), _kb(KB_B, "v1")])
    sql = str(
        build_scope_filter(dict(resolved)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "chunks.kb_id = 'kb-a' AND chunks.index_version = 'v2'" in sql
    assert "chunks.kb_id = 'kb-b' AND chunks.index_version = 'v1'" in sql
    assert " OR " in sql, "两对之间是 OR"
    assert " IN " not in sql.upper(), "两个独立 IN 会拼出「A 库配 B 库版本」这种不存在的组合"


# ────────────────────────────────────────────────── ASGI：检索 pin 在哪一次构建上


def test_retrieval_sends_each_kb_with_its_own_effective_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多库多版本：向量库收到的是配对映射，调用方无法指定版本。"""

    with _offline(
        monkeypatch,
        kbs={KB_A: "v2", KB_B: "v1"},
        rows=[_row(KB_A, "v2", "chunk-a"), _row(KB_B, "v1", "chunk-b")],
    ) as fake:
        body = _retrieve(fake["client"], [KB_A, KB_B])

    assert fake["store"].scopes == [{KB_A: "v2", KB_B: "v1"}]
    assert [chunk["kb_id"] for chunk in body["data"]["retrieved_chunks"]] == [KB_A, KB_B]
    assert all("index_version" not in chunk for chunk in body["data"]["retrieved_chunks"]), (
        "版本不进响应：要查「哪次构建」读归属索引，不扩响应契约"
    )


def test_switching_the_effective_version_moves_the_hits_to_the_new_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个库造两个版本各一条，切换生效版本后**只**命中新版本那条。

    这是本票的用户可见结果：重建产出 v2、切上线之后，旧切法的切片不再被召回——
    而不是「两套切法混排」。
    """

    kbs: dict[str, str | None] = {KB_A: "v1"}
    rows = [_row(KB_A, "v1", "chunk-old"), _row(KB_A, "v2", "chunk-new")]

    with _offline(monkeypatch, kbs=kbs, rows=rows) as fake:
        before = _retrieve(fake["client"], [KB_A])
        kbs[KB_A] = "v2"  # 激活：库上的生效版本列被单事务改掉
        after = _retrieve(fake["client"], [KB_A])

    assert [chunk["chunk_id"] for chunk in before["data"]["retrieved_chunks"]] == ["chunk-old"]
    assert [chunk["chunk_id"] for chunk in after["data"]["retrieved_chunks"]] == ["chunk-new"]
    assert fake["store"].scopes == [{KB_A: "v1"}, {KB_A: "v2"}]


def test_a_kb_that_was_never_backfilled_is_refused_before_any_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫在**召回之前**拦下：拿不到生效版本就不许发任何一条检索语句。

    如果让它继续，「版本 = NULL」会拼出一条恒不匹配的条件，于是这次调用变成一次
    「诚实的空结果」——运维看到的是「这个库没内容」，而真正的原因是漏了回填。
    """

    with _offline(monkeypatch, kbs={KB_A: None}, rows=[_row(KB_A, "v1", "chunk-old")]) as fake:
        body = _retrieve(fake["client"], [KB_A])

    assert body["code"] == SERVER_ERROR_CODE
    assert body["msg"] == UNRESOLVED_INDEX_VERSION_MESSAGE
    assert fake["store"].scopes == [], "被拦下的请求一条召回语句都不该发出"
    assert fake["trace_session"].added_of(RetrievalTrace) == [], "没有发生的检索不写归属索引"


def test_the_trace_row_records_which_build_was_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    """归属索引那一行要能回答「这次查的是哪次构建」：``{kb_id: version}`` 映射。"""

    with _offline(
        monkeypatch,
        kbs={KB_A: "v2", KB_B: "v1"},
        rows=[_row(KB_A, "v2", "chunk-a"), _row(KB_B, "v1", "chunk-b")],
    ) as fake:
        _retrieve(fake["client"], [KB_A, KB_B])
        rows = fake["trace_session"].added_of(RetrievalTrace)

    assert len(rows) == 1, "一次检索一行（与 trace_id 一一对应）"
    assert rows[0].index_versions == {KB_A: "v2", KB_B: "v1"}, (
        "存的是映射：两个并列数组会拼出「A 库配 B 库版本」那种不存在的组合"
    )
    assert rows[0].kb_ids == [KB_A, KB_B]


def test_the_request_cannot_choose_an_index_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """版本是服务端决定的：请求体里带 ``index_version`` 直接 20002，连召回都不进。"""

    with _offline(monkeypatch, kbs={KB_A: "v2"}, rows=[_row(KB_A, "v2", "chunk-a")]) as fake:
        IDENTITY.use(mode="vector", top_k=2, include_parent=False)
        response = fake["client"].post(
            RETRIEVE_PATH,
            json={"kb_ids": [KB_A], "query": QUERY, "index_version": "v1"},
        )
        body = response.json()

    assert body["code"] == VALIDATION_ERROR_CODE
    assert fake["store"].scopes == []


def test_the_default_version_is_still_what_a_backfilled_kb_gets() -> None:
    """回填写的是 ``v1``，与索引流水线一直写的那个常量同源（不是又抄一个字面量）。"""

    assert DEFAULT_INDEX_VERSION == "v1"
