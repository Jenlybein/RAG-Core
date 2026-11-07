"""检索请求契约的第三种形态：``mode`` 与 ``top_k``（ASGI 接缝）。

接缝是既有的：用测试客户端打 ``POST /api/v1/rag/retrieve``，数据库依赖用
dependency_overrides 换掉、三个 Provider 工厂用离线替身换掉，其余走真实应用、
真实 schema 校验与真实错误处理器。断言的只有响应体。

票 23 的边界（票 4 会扩展它，所以这里也守边界）：

* ``mode`` 只允许 ``vector`` / ``bm25``；``hybrid`` **现在不允许**——
  声明支持而行为不对，比不支持更糟；
* ``mode=vector`` 的检索行为与之前完全一致（同一个向量存储、同样的顺序与分数）；
* ``mode=bm25`` 只走关键词一路：关键词分有值，向量分与向量名次是 ``null``；
* 单模式不降级：关键词引擎不可用 / 调用失败都必须明确失败（50002），
  绝不偷偷退回向量。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.providers.keyword_search.noop import NoopKeywordSearchProvider

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"
KB_B = "kb-b"


# ─────────────────────────────────────────────────────────────────── 替身


class _FakeEmbedding:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
        self.calls.append({"tenant_id": tenant_id, "kb_id": kb_id, "top_k": top_k})
        score = 0.9 if kb_id == KB_A else 0.5
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "title": "title",
                "content": "content",
                "score": score,
            }
        ]


class _FakeKeywordSearch:
    """关键词引擎替身：``available=False`` 模拟「没配引擎」，``fail=True`` 模拟引擎挂了。"""

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
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
        return [
            {
                "chunk_id": "chunk-hit",
                "document_id": "doc-hit",
                "kb_id": KB_A,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 12.5,
            }
        ]

    async def add_chunks(self, chunks):  # pragma: no cover - 检索链路不写
        raise AssertionError("检索链路不应写索引")

    async def delete_by_document_id(self, document_id, *, tenant_id):  # pragma: no cover
        raise AssertionError("检索链路不应删索引")


@pytest.fixture
def offline_retrieval(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端 + 三个工厂替身；返回可断言的替身本体。"""

    main = importlib.import_module("app.main")
    session = importlib.import_module("app.db.session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = _FakeEmbedding()
    vector = _FakeVectorStore()
    keyword = _FakeKeywordSearch()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(
        kb_repo,
        "get_by_id",
        lambda db, kb_id: type("KB", (), {"tenant_id": TENANT_ID})(),
    )

    from app.core.auth import DevKeyContext, resolve_dev_key

    main.app.dependency_overrides[session.get_db] = lambda: object()
    main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield {"client": client, "embedding": embedding, "vector": vector, "keyword": keyword}
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, **body) -> dict:
    payload = {"kb_ids": [KB_A], "query": "E1024"}
    payload.update(body)
    response = client.post(RETRIEVE_PATH, json=payload)
    assert response.status_code == 200
    return response.json()


# ────────────────────────────────────────────── mode=vector（回归红线）


def test_vector_mode_is_still_the_default_and_unchanged(offline_retrieval) -> None:
    """不传 mode 就是 vector：逐库检索、合并排序、score 仍是向量分。"""

    body = _post(offline_retrieval["client"])

    assert body["code"] == 0
    assert [c["document_id"] for c in body["data"]["retrieved_chunks"]] == ["doc-kb-a"]
    assert offline_retrieval["embedding"].queries == ["E1024"]
    assert offline_retrieval["vector"].calls == [
        {"tenant_id": TENANT_ID, "kb_id": KB_A, "top_k": settings.TOP_K}
    ]


def test_vector_mode_fills_vector_scores_and_leaves_keyword_fields_null(
    offline_retrieval,
) -> None:
    """两种分数在同一份响应里可区分：没走的那一路是 null，不是 0。"""

    body = _post(offline_retrieval["client"], mode="vector")
    chunk = body["data"]["retrieved_chunks"][0]

    assert chunk["score"] == chunk["vector_score"] == 0.9
    assert chunk["vector_rank"] == 1
    assert chunk["bm25_score"] is None
    assert chunk["bm25_rank"] is None
    assert chunk["rrf_score"] is None
    assert chunk["kb_id"] == KB_A


# ────────────────────────────────────────────── mode=bm25（本票新通道）


def test_bm25_mode_uses_only_the_keyword_channel(offline_retrieval) -> None:
    """只走关键词一路：不向量化、不查向量库、结果里只有关键词分数。"""

    body = _post(offline_retrieval["client"], mode="bm25")

    assert offline_retrieval["embedding"].queries == [], "bm25 不做向量化"
    assert offline_retrieval["vector"].calls == [], "单模式绝不偷偷退回向量"
    assert offline_retrieval["keyword"].calls == [
        {
            "query": "E1024",
            "tenant_id": TENANT_ID,
            "index_versions": {KB_A: "v1"},
            "top_k": settings.TOP_K,
        }
    ]

    chunk = body["data"]["retrieved_chunks"][0]
    assert body["code"] == 0
    assert chunk["content"] == "错误码 E1024 表示证书过期。", "回给调用方的仍是切片原文"
    assert chunk["title"] == "错误码手册"
    assert chunk["kb_id"] == KB_A
    assert chunk["bm25_score"] == chunk["score"] == 12.5
    assert chunk["bm25_rank"] == 1
    assert chunk["vector_score"] is None
    assert chunk["vector_rank"] is None
    assert chunk["rrf_score"] is None


def test_bm25_mode_pairs_every_kb_with_its_index_version(offline_retrieval) -> None:
    """多库时传的是映射而不是两个平行数组：一个库一条配对。"""

    _post(offline_retrieval["client"], mode="bm25", kb_ids=[KB_A, KB_B])

    assert offline_retrieval["keyword"].calls[0]["index_versions"] == {
        KB_A: "v1",
        KB_B: "v1",
    }


def test_bm25_mode_fails_loudly_when_the_engine_is_not_configured(
    offline_retrieval, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配关键词引擎：明确失败（50002），绝不退回向量。"""

    service = importlib.import_module("app.services.rag_service")
    monkeypatch.setattr(service, "get_keyword_search_provider", NoopKeywordSearchProvider)

    body = _post(offline_retrieval["client"], mode="bm25")

    assert body["code"] == 50002
    assert body["data"] is None
    assert offline_retrieval["vector"].calls == [], "失败的 bm25 不许退回向量"
    assert offline_retrieval["embedding"].queries == []


def test_bm25_mode_maps_engine_errors_to_retrieval_failed(offline_retrieval) -> None:
    """引擎连不上：错误分类是 50002，不是 500 也不是空结果。"""

    offline_retrieval["keyword"].fail = True

    body = _post(offline_retrieval["client"], mode="bm25")

    assert body["code"] == 50002
    assert offline_retrieval["vector"].calls == []


# ─────────────────────────────────────────────────── 请求字段的边界


def test_hybrid_mode_is_not_accepted_yet(offline_retrieval) -> None:
    """票 4 才实现 hybrid：现在传它必须是校验失败，而不是「支持但行为不对」。"""

    body = _post(offline_retrieval["client"], mode="hybrid")

    assert body["code"] == 20002
    assert offline_retrieval["vector"].calls == []
    assert offline_retrieval["keyword"].calls == []


@pytest.mark.parametrize("top_k", [0, -1, 51, 1000])
def test_top_k_is_bounded(offline_retrieval, top_k: int) -> None:
    """top_k 有上下界：越界走既有的 20002 校验失败，不进检索链路。"""

    body = _post(offline_retrieval["client"], top_k=top_k)

    assert body["code"] == 20002
    assert offline_retrieval["vector"].calls == []


def test_top_k_from_the_request_reaches_the_channel(offline_retrieval) -> None:
    """显式 top_k 一路穿到通道，也回显在 metadata 里。"""

    body = _post(offline_retrieval["client"], top_k=2)

    assert offline_retrieval["vector"].calls[0]["top_k"] == 2
    assert body["data"]["metadata"]["top_k"] == 2
    assert body["data"]["metadata"]["mode"] == "vector"


def test_mode_default_comes_from_server_config(
    offline_retrieval, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求不带 mode 时取服务端配置：这是 MCP 入口（只传 query）的行为来源。"""

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "bm25")

    body = _post(offline_retrieval["client"])

    assert body["data"]["metadata"]["mode"] == "bm25"
    assert offline_retrieval["keyword"].calls, "缺省模式来自配置，不是硬编码的 vector"
    assert offline_retrieval["embedding"].queries == []
