"""混合检索：两路并行 → RRF 融合 → 降级 / 空结果（ASGI 接缝）。

接缝仍是既有的那一个：测试客户端打 ``POST /api/v1/rag/retrieve``，数据库依赖用
``dependency_overrides`` 换掉，Provider 工厂用离线替身换掉（与
``tests/test_retrieval_mode.py`` 完全同一套做法，没有新造接缝），其余走真实应用、
真实 schema 校验与真实错误处理器。断言的只有响应体与替身收到的调用。

本文件守的契约（父 spec「融合契约」「降级矩阵与三种结局」「响应契约」）：

* 两路都健康 → 融合，``degraded=false``；
* 仅一路失败 → 用另一路，``code`` 仍为 0，``degraded=true`` + ``degraded_reason``；
* 两路都失败 → ``RETRIEVAL_FAILED``（50002）；
* 嵌入失败发生在向量分支内部 → 对称降级成「仅关键词」，绝不冒泡成整单失败；
* 单模式（vector / bm25）不降级：挂了就失败；
* 空结果区分 ``no_indexed_chunks`` 与 ``no_chunks_matched``，HTTP 200、``code=0``；
* 混合模式下每条切片五列分数 / 名次真的有值，``score`` 就是融合分；
* 相同请求两次返回完全相同的顺序；
* 融合常数 ``k`` 不进请求体；
* **多库（票 5）**：两条通道各查**一次**、嵌入只算一次，配对映射一次覆盖全部库；
  两个库的切片都进结果且各自带 ``kb_id``；某库没命中不是失败；可见性每库只校验
  一次，且先于召回——被拒的请求连嵌入都不算。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import SUPPORTED_RETRIEVAL_MODES, Settings, settings
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.services.retrieval_options import RetrievalMode
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"
KB_B = "kb-b"

# 票 #77 起质量参数由密钥配置提供：本模块的请求助手把旧写法（``mode=`` / ``top_k=``
# 等放进请求体）翻译成「造一个 custom 档的假身份」，调用点一个字都不用改。
IDENTITY = RetrievalIdentity(TENANT_ID)

# 手算的两条参考值（k=60）：两路都第 1 名 = 2/61；单路第 2 名 = 1/62。
BOTH_FIRST = 0.0327868852459016
SINGLE_SECOND = 0.0161290322580645


# ─────────────────────────────────────────────────────────────── 替身


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def all(self) -> list[Any]:
        # 拼父块时会查一次同节切片；离线替身没有数据可给，返回空即可。
        return []

    def __iter__(self) -> Any:
        return iter(())


class _FakeDb:
    """数据库替身：只回答「这个租户的库内共有多少条可检索切片」。

    空结果原因必须查一次数据库才许下结论（父 spec：不许猜），所以这里只实现
    那一次 ``count`` 需要的 ``execute(...).scalar_one()``。向量、关键词两路都是
    替身，不会真的碰它。
    """

    def __init__(self, *, indexed_chunks: int = 0) -> None:
        self.indexed_chunks = indexed_chunks
        self.closed = False
        self.executed = 0

    def execute(self, _statement: Any) -> _FakeResult:
        self.executed += 1
        return _FakeResult(self.indexed_chunks)

    def close(self) -> None:
        self.closed = True


class _FakeEmbedding:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("embedding endpoint refused the connection")
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """按库返回切片；``fail=True`` 模拟向量库/数据库这一路挂了。

    实现的是**范围版**（票 5）：一次调用覆盖全部「库 + 版本」配对。单库版的
    ``similarity_search`` 在这里故意直接报错——混合模式一旦按库循环召回，
    这个测试文件会同时以「按库分名额」和「多次查询」两种方式失败。
    """

    def __init__(self, rows: dict[str, list[dict]] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or {}
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.db_seen: Any = None

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, index_versions, top_k):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "index_versions": dict(index_versions),
                "top_k": top_k,
            }
        )
        self.db_seen = db
        if self.fail:
            raise RuntimeError("psycopg: connection refused")
        # 一次范围召回：结果按配对顺序拼接，每行自带 kb_id（跨库结果要能归属）。
        return [
            {**row, "kb_id": kb_id} for kb_id in index_versions for row in self.rows.get(kb_id, [])
        ]

    async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
        raise AssertionError("混合模式必须一次范围召回，不许按库循环")


class _FakeKeyword:
    """关键词引擎替身：``available=False`` 模拟没配引擎，``fail=True`` 模拟引擎挂了。"""

    def __init__(
        self, rows: list[dict] | None = None, *, available: bool = True, fail: bool = False
    ):
        self.rows = rows or []
        self.available = available
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        self.calls.append(
            {
                "query": query,
                "tenant_id": tenant_id,
                "index_versions": index_versions,
                "top_k": top_k,
            }
        )
        if self.fail:
            raise RuntimeError("connection refused: elasticsearch")
        return list(self.rows)

    async def add_chunks(self, chunks):  # pragma: no cover - 检索链路不写
        raise AssertionError("检索链路不应写索引")

    async def delete_by_document_id(self, document_id, *, tenant_id):  # pragma: no cover
        raise AssertionError("检索链路不应删索引")


def _vector_row(chunk_id: str, *, kb_id: str = KB_A, score: float) -> dict:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": score,
    }


def _keyword_row(chunk_id: str, *, kb_id: str = KB_A, score: float) -> dict:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "kb_id": kb_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": score,
    }


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: Any = None,
    vector: Any = None,
    keyword: Any = None,
    indexed_chunks: int = 0,
    request_db: _FakeDb | None = None,
    work_db: _FakeDb | None = None,
) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端 + 替身；返回替身本体供断言。

    ``request_db`` 是 FastAPI 依赖注入的那个会话（查空结果原因用），
    ``work_db`` 是向量分支在**自己的线程**里拿到的会话——两者刻意分开，
    这样「向量分支持有自己的 SessionLocal」才是可观察的事实。
    """

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    service = importlib.import_module("app.services.rag_service")
    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = embedding or _FakeEmbedding()
    vector = vector or _FakeVectorStore()
    keyword = keyword if keyword is not None else _FakeKeyword()
    request_db = request_db or _FakeDb(indexed_chunks=indexed_chunks)
    work_db = work_db or _FakeDb()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    # 向量分支必须持有自己的会话；测试里把它接到替身上，绝不真的连 PostgreSQL。
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: work_db)
    monkeypatch.setattr(
        kb_repo,
        "get_by_id_for_tenant",
        lambda db, kb_id, *, tenant_id: SimpleNamespace(tenant_id=tenant_id),
    )

    from app.api.deps import get_current_key

    main.app.dependency_overrides[session_mod.get_db] = lambda: request_db
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {
                "client": client,
                "embedding": embedding,
                "vector": vector,
                "keyword": keyword,
                "db": request_db,
                "work_db": work_db,
            }
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, **body) -> dict:
    payload = {"kb_ids": [KB_A], "query": "E1024"}
    payload.update(IDENTITY.apply(body))
    response = client.post(RETRIEVE_PATH, json=payload)
    assert response.status_code == 200
    return response.json()


def _retrieval_metadata(body: dict) -> dict:
    return body["data"]["metadata"]["retrieval"]


# ───────────────────────────────────────────── 模式词汇与默认值（票 4 第 1 条）


def test_mode_vocabulary_is_in_sync_everywhere() -> None:
    """配置里的词汇表、已解析参数里的 ``Literal``、默认值三处必须一致。"""

    assert (
        set(SUPPORTED_RETRIEVAL_MODES)
        == set(RetrievalMode.__args__)
        == {"vector", "bm25", "hybrid"}
    )
    assert Settings(_env_file=None).RETRIEVAL_MODE == "hybrid"


def test_hybrid_is_the_server_default_and_calls_both_channels(monkeypatch) -> None:
    """不传 mode → hybrid（MCP 入口只传 query，行为随服务端默认值变化）。"""

    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    keyword = _FakeKeyword([_keyword_row("c1", score=12.5)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0
    assert body["data"]["metadata"]["mode"] == "hybrid"
    assert _retrieval_metadata(body)["mode"] == "hybrid"
    assert fake["embedding"].queries == ["E1024"], "混合模式用原句向量化"
    # 每路召回的是**召回深度**而不是 top_k：召回宽、返回窄。两路各找 20 条再融合，
    # 最终仍只返回 top_k 条——池子只有 top_k 时融合几乎无事可做。
    recall_depth = max(settings.TOP_K, settings.HYBRID_RECALL_TOP_K)
    assert recall_depth > settings.TOP_K, "默认配置下召回深度应大于返回条数"
    assert fake["vector"].calls == [
        {"tenant_id": TENANT_ID, "index_versions": {KB_A: "v1"}, "top_k": recall_depth}
    ]
    assert fake["keyword"].calls == [
        {
            "query": "E1024",
            "tenant_id": TENANT_ID,
            "index_versions": {KB_A: "v1"},
            "top_k": recall_depth,
        }
    ]


def test_rrf_k_is_not_a_request_field(monkeypatch) -> None:
    """``k`` 写死，不做成请求参数：传了就是校验失败，不进检索链路。"""

    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    with _offline_client(monkeypatch, vector=vector) as fake:
        body = _post(fake["client"], rrf_k=10)

    assert body["code"] == 20002
    assert fake["vector"].calls == []


# ───────────────────────────────────────── 融合结果的字段与顺序（响应契约）


def test_hybrid_fills_every_split_field_and_uses_fusion_score(monkeypatch) -> None:
    """两路名次都在、两路分数都在、``score`` 就是融合分。"""

    vector = _FakeVectorStore(
        {KB_A: [_vector_row("c1", score=0.93), _vector_row("c2", score=0.71)]}
    )
    keyword = _FakeKeyword([_keyword_row("c1", score=12.4), _keyword_row("c3", score=9.1)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    chunks = body["data"]["retrieved_chunks"]
    assert [c["chunk_id"] for c in chunks] == ["c1", "c2", "c3"]

    first, second, third = chunks
    assert (first["vector_rank"], first["bm25_rank"]) == (1, 1)
    assert (first["vector_score"], first["bm25_score"]) == (0.93, 12.4)
    assert first["rrf_score"] == pytest.approx(BOTH_FIRST)
    assert first["score"] == first["rrf_score"], "混合模式下 score 的语义是融合分"
    assert first["kb_id"] == KB_A
    assert first["content"] == "content-c1", "回给调用方的仍是切片原文"

    # 只命中一路的：另一路是 null 而不是 0。
    assert (second["vector_rank"], second["bm25_rank"]) == (2, None)
    assert (second["vector_score"], second["bm25_score"]) == (0.71, None)
    assert second["rrf_score"] == pytest.approx(SINGLE_SECOND)
    assert (third["vector_rank"], third["bm25_rank"]) == (None, 2)
    assert (third["vector_score"], third["bm25_score"]) == (None, 9.1)
    assert third["rrf_score"] == pytest.approx(SINGLE_SECOND)


def test_repeated_requests_return_the_same_order(monkeypatch) -> None:
    """同样请求两次，顺序完全一致——含两条同分（都是 1/62）的情况。"""

    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9), _vector_row("c2", score=0.8)]})
    keyword = _FakeKeyword([_keyword_row("c1", score=12.5), _keyword_row("c3", score=9.0)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        first = _post(fake["client"], mode="hybrid")
        second = _post(fake["client"], mode="hybrid")

    assert first["data"]["retrieved_chunks"] == second["data"]["retrieved_chunks"]


# ─────────────────────────────── 多库：一次全局召回 + 一次融合（票 5）


def test_two_knowledge_bases_cost_one_vector_query_and_one_embedding(monkeypatch) -> None:
    """两个库的代价：向量查询 1 次、关键词查询 1 次、嵌入 1 次。

    这是「一次全局召回」最直接的证据。分库召回再合并会随库数量线性增长，
    而嵌入本来就和库数量无关——把两者都钉在 1，才排除「按库循环」。
    """

    vector = _FakeVectorStore(
        {KB_A: [_vector_row("a1", score=0.93)], KB_B: [_vector_row("b1", score=0.71)]}
    )
    keyword = _FakeKeyword([_keyword_row("a1", kb_id=KB_A, score=12.0)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid", kb_ids=[KB_A, KB_B])

    assert body["code"] == 0
    assert len(fake["vector"].calls) == 1, "两个库也只查一次向量"
    assert fake["vector"].calls[0]["index_versions"] == {KB_A: "v1", KB_B: "v1"}, (
        "一次调用覆盖全部「库 + 版本」配对"
    )
    assert fake["embedding"].queries == ["E1024"], "嵌入只算一次，不随库数量增加"
    assert len(fake["keyword"].calls) == 1
    assert fake["keyword"].calls[0]["index_versions"] == {KB_A: "v1", KB_B: "v1"}


def test_two_knowledge_bases_both_contribute_chunks_with_their_own_kb_id(monkeypatch) -> None:
    """跨库端到端：两个库都有切片进结果，每条都带 ``kb_id``。"""

    vector = _FakeVectorStore(
        {
            KB_A: [_vector_row("a1", score=0.93), _vector_row("a2", score=0.66)],
            KB_B: [_vector_row("b1", score=0.71)],
        }
    )
    keyword = _FakeKeyword([_keyword_row("b1", kb_id=KB_B, score=9.9)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid", kb_ids=[KB_A, KB_B])

    chunks = body["data"]["retrieved_chunks"]
    assert {chunk["kb_id"] for chunk in chunks} == {KB_A, KB_B}
    assert all(chunk["kb_id"] for chunk in chunks), "跨库结果必须能归属到某个库"
    assert body["data"]["kb_ids"] == [KB_A, KB_B]
    # 两个库的切片都在同一个融合序列里竞争名次，而不是各排各的。
    assert [chunk["rrf_score"] for chunk in chunks] == sorted(
        (chunk["rrf_score"] for chunk in chunks), reverse=True
    )


def test_a_kb_without_hits_is_not_a_failure(monkeypatch) -> None:
    """「某库没有切片进入结果」是正常排序结果，绝不记成该库失败。"""

    # KB_B 一条都没命中：这是「排序结果」，不是「这个库出事了」。
    vector = _FakeVectorStore({KB_A: [_vector_row("a1", score=0.9)]})
    keyword = _FakeKeyword([_keyword_row("a1", kb_id=KB_A, score=12.0)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid", kb_ids=[KB_A, KB_B])

    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is False, "某库没结果不是降级"
    assert [chunk["kb_id"] for chunk in body["data"]["retrieved_chunks"]] == [KB_A]
    assert "failed_kb_ids" not in json.dumps(body), "不许按库记失败：那是按结果条数推断故障"


def test_visibility_is_checked_once_per_kb_before_any_recall(monkeypatch) -> None:
    """可见性：N 个库 = N 次查询（之前两条分支各一遍 = 2N 次）。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    seen: list[str] = []

    def _visible(db, kb_id, *, tenant_id):
        seen.append(kb_id)
        return SimpleNamespace(tenant_id=tenant_id)

    with _offline_client(monkeypatch) as fake:
        monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _visible)
        body = _post(fake["client"], mode="hybrid", kb_ids=[KB_A, KB_B])

    assert body["code"] == 0
    assert seen == [KB_A, KB_B], "两个库各校验一次，向量与关键词两条分支都不再重复"


def test_invisible_kb_is_rejected_before_anything_is_recalled(monkeypatch) -> None:
    """不可见的库：拒绝先于召回——两条通道都没被调用，嵌入也没算。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    vector = _FakeVectorStore({KB_A: [_vector_row("a1", score=0.9)]})
    with _offline_client(monkeypatch, vector=vector) as fake:
        monkeypatch.setattr(
            kb_repo, "get_by_id_for_tenant", lambda db, kb_id, *, tenant_id: None
        )
        body = _post(fake["client"], mode="hybrid", kb_ids=[KB_A, KB_B])

    assert body["code"] == 20008
    assert body["data"] is None
    assert fake["vector"].calls == [], "拒绝发生在召回之前"
    assert fake["keyword"].calls == [], "两条通道都不该被调用"
    assert fake["embedding"].queries == [], "被拒的请求不该白烧一次嵌入调用"


# ───────────────────────────────────────────── 降级矩阵（四种组合）


def test_both_channels_healthy_is_not_degraded(monkeypatch) -> None:
    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    keyword = _FakeKeyword([_keyword_row("c2", score=12.5)])
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    assert _retrieval_metadata(body) == {
        "mode": "hybrid",
        "degraded": False,
        "degraded_reason": None,
        "empty_reason": None,
        # 重排默认关闭：``applied=false``，``duration_ms=null`` 表示「没有发生」，
        # 而 candidates/returned 反映进入这一步的候选数——两者都在，才分得清
        # 「没开」与「开了但一条没变」。
        "rerank": {
            "applied": False,
            "candidates": 2,
            "returned": 2,
            "duration_ms": None,
            # #116 起的两个**纯增量**身份键：这一步没跑，所以没有实现可署名（None）。
            "provider": None,
            "model": None,
        },
    }
    assert len(body["data"]["retrieved_chunks"]) == 2


def test_keyword_failure_degrades_to_vectors(monkeypatch) -> None:
    """关键词一路挂了：业务码仍为成功，结果来自向量，并如实标记降级。"""

    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    keyword = _FakeKeyword(fail=True)
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "keyword search failed"
    chunks = body["data"]["retrieved_chunks"]
    assert [c["chunk_id"] for c in chunks] == ["c1"]
    assert chunks[0]["bm25_rank"] is None
    assert chunks[0]["vector_rank"] == 1


def test_keyword_engine_not_configured_degrades_only_in_hybrid(monkeypatch) -> None:
    """没配引擎：混合模式降级成仅向量；单模式仍然硬失败（不降级）。"""

    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    with _offline_client(monkeypatch, vector=vector, keyword=NoopKeywordSearchProvider()) as fake:
        hybrid = _post(fake["client"], mode="hybrid")
        single = _post(fake["client"], mode="bm25")

    assert hybrid["code"] == 0
    assert _retrieval_metadata(hybrid)["degraded_reason"] == "keyword search failed"
    assert single["code"] == 50002, "单模式不降级"


def test_vector_failure_degrades_to_keywords(monkeypatch) -> None:
    """对称降级：向量一路挂了，关键词一路的结果照样返回并标记降级。"""

    keyword = _FakeKeyword([_keyword_row("c1", score=12.5)])
    vector = _FakeVectorStore(fail=True)
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "vector search failed"
    chunks = body["data"]["retrieved_chunks"]
    assert [c["chunk_id"] for c in chunks] == ["c1"]
    assert chunks[0]["vector_rank"] is None
    assert chunks[0]["bm25_rank"] == 1


# ────────────────────────────────────────── 对称降级：嵌入失败不冒泡


def test_embedding_failure_degrades_to_keywords_instead_of_failing(monkeypatch) -> None:
    """嵌入失败发生在向量分支内部，必须被该分支捕获并走「仅关键词」。

    这是本票最容易漏掉的一条：原作只兜了「关键词挂了」一半。
    """

    embedding = _FakeEmbedding(fail=True)
    keyword = _FakeKeyword([_keyword_row("c1", score=12.5)])
    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    with _offline_client(monkeypatch, embedding=embedding, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0, "嵌入失败绝不许冒泡成整单失败"
    assert fake["embedding"].queries == ["E1024"], "向量分支确实尝试过"
    assert fake["vector"].calls == [], "向量化失败后不该再查向量库"
    assert _retrieval_metadata(body)["degraded"] is True
    assert _retrieval_metadata(body)["degraded_reason"] == "vector search failed"
    assert [c["chunk_id"] for c in body["data"]["retrieved_chunks"]] == ["c1"]


# ─────────────────────────────────────────── 两路都挂 / 单模式不降级


def test_both_channels_failing_is_retrieval_failed(monkeypatch) -> None:
    """两路都不可用：明确的检索失败，而不是「诚实的空结果」。"""

    embedding = _FakeEmbedding(fail=True)
    keyword = _FakeKeyword(fail=True)
    with _offline_client(monkeypatch, embedding=embedding, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 50002
    assert body["data"] is None


def test_single_vector_mode_still_fails_loudly_when_embedding_breaks(monkeypatch) -> None:
    """单模式不降级：``mode=vector`` 下嵌入挂了照旧失败，绝不换成关键词。"""

    embedding = _FakeEmbedding(fail=True)
    keyword = _FakeKeyword([_keyword_row("c1", score=12.5)])
    # 这条用例要断言 500，走不了那个「必须 200」的请求助手，于是直接发；模式仍由密钥
    # 配置表达（票 #77 起请求体里没有它了）。
    IDENTITY.use(mode="vector")
    with _offline_client(monkeypatch, embedding=embedding, keyword=keyword) as fake:
        response = fake["client"].post(
            RETRIEVE_PATH, json={"kb_ids": [KB_A], "query": "E1024"}
        )

    assert response.status_code == 500
    assert response.json()["code"] == 50000
    assert fake["keyword"].calls == [], "单模式绝不偷偷退回另一路"


def test_visibility_rejection_is_not_degraded(monkeypatch) -> None:
    """知识库不可见属于「召回前拒绝」，不参与降级：照旧 20008。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    with _offline_client(monkeypatch) as fake:
        # 在客户端就绪之后再换仓储，确保覆盖掉 fixture 里「库可见」的默认替身。
        monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", lambda db, kb_id, *, tenant_id: None)
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 20008
    assert body["data"] is None


# ───────────────────────────────────────────────── 空结果的两种原因


def test_empty_result_with_no_indexed_chunks(monkeypatch) -> None:
    """库内没有可检索内容：成功 + ``no_indexed_chunks``。"""

    with _offline_client(monkeypatch, indexed_chunks=0) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    assert body["data"]["retrieved_chunks"] == []
    assert _retrieval_metadata(body)["empty_reason"] == "no_indexed_chunks"
    assert _retrieval_metadata(body)["degraded"] is False
    assert fake["db"].executed >= 1, "空结果原因必须查一次数据库，不许猜"


def test_empty_result_with_content_but_no_match(monkeypatch) -> None:
    """库内有内容但这次没命中：成功 + ``no_chunks_matched``。"""

    with _offline_client(monkeypatch, indexed_chunks=7) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    assert body["data"]["retrieved_chunks"] == []
    assert _retrieval_metadata(body)["empty_reason"] == "no_chunks_matched"


def test_empty_result_keeps_degradation_flag(monkeypatch) -> None:
    """降级 + 空可以同时成立：结果为空且一路挂着，两个事实都要如实说。"""

    keyword = _FakeKeyword(fail=True)
    with _offline_client(monkeypatch, keyword=keyword, indexed_chunks=2) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "keyword search failed"
    assert metadata["empty_reason"] == "no_chunks_matched"


def test_non_empty_result_has_no_empty_reason(monkeypatch) -> None:
    vector = _FakeVectorStore({KB_A: [_vector_row("c1", score=0.9)]})
    with _offline_client(monkeypatch, vector=vector) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert _retrieval_metadata(body)["empty_reason"] is None


# ───────────────────────────────────────────── 真并行与独立会话


def test_vector_branch_runs_in_a_worker_thread_with_its_own_session(monkeypatch) -> None:
    """向量分支必须跑在独立线程、持有自己的 ``SessionLocal``，否则事件循环被卡住。

    证据是「关键词协程在向量分支阻塞期间仍在推进」：向量替身用 ``time.sleep``
    同步阻塞 250ms（模拟 psycopg），关键词替身每 10ms 记一次 tick。若向量分支
    跑在事件循环上，关键词协程根本得不到执行，tick 会是 0。
    """

    ticks: list[int] = []
    vector_started = threading.Event()
    vector_finished = threading.Event()

    class _BlockingVectorStore(_FakeVectorStore):
        async def similarity_search_scope(
            self, db, query_vector, *, tenant_id, index_versions, top_k
        ):
            self.db_seen = db
            self.calls.append(
                {"tenant_id": tenant_id, "index_versions": dict(index_versions), "top_k": top_k}
            )
            vector_started.set()
            time.sleep(0.25)  # 同步阻塞：这正是「并行」必须给它的代价
            vector_finished.set()
            return [{**_vector_row("c1", score=0.9), "kb_id": KB_A}]

    class _TickingKeyword(_FakeKeyword):
        async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
            # 加一个轮数上限：万一向量分支没起来，测试要失败而不是挂死。
            for _ in range(200):
                if vector_finished.is_set():
                    break
                ticks.append(1)
                await asyncio.sleep(0.01)
            return [_keyword_row("c2", score=12.5)]

    vector = _BlockingVectorStore()
    keyword = _TickingKeyword()
    with _offline_client(monkeypatch, vector=vector, keyword=keyword) as fake:
        body = _post(fake["client"], mode="hybrid")

    assert body["code"] == 0
    assert vector_started.is_set(), "向量分支真的跑了"
    assert len(ticks) >= 5, "事件循环在向量分支阻塞期间必须仍在转（否则不是真并行）"
    assert vector.db_seen is fake["work_db"], "向量分支用的是自己的会话，不是请求那个"
    assert vector.db_seen is not fake["db"]
    assert fake["work_db"].closed is True, "线程里的会话用完必须关掉"
    assert fake["db"].closed is False, "请求会话由 FastAPI 依赖负责关闭，检索层不越权"
