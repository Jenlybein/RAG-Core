"""重排的**返回条数只由 ``top_k`` 决定**（回归：失败 / 部分排序时曾返回整个融合池）。

开启重排时管线送进重排的是**融合池**（``RERANK_MAX_CANDIDATES``，默认 20），不是
``top_k``。任何一条返回路径只要「原样返回入参 chunks」，调用方就会收到 20 条——
``top_k=5`` 的请求拿回 20 条，直接违反父 spec「返回条数由 top_k 决定」。

接缝与 ``tests/test_hybrid_rerank.py`` 相同：ASGI 边界打 ``POST /api/v1/rag/retrieve``，
数据库依赖与 Provider 工厂用离线替身换掉。不发网络、不连 PostgreSQL、不读 .env。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.providers.rerank.llm import LLMRerankProvider, RerankRanking, RerankScore
from tests.fake_db import AsyncSessionAdapter, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)

# 融合池宽度与最终返回条数：这两个数字**必须**不同，否则本文件守不住任何东西。
CANDIDATES = 20
TOP_K = 5


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
    def __init__(self, *, indexed_chunks: int = 0) -> None:
        self.indexed_chunks = indexed_chunks

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(self.indexed_chunks)

    def close(self) -> None:
        return None


class _FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _row(index: int, *, score: float) -> dict:
    return {
        "document_id": f"doc-{index}",
        "chunk_id": f"c{index:02d}",
        "title": f"title-{index}",
        "content": f"content-{index}",
        "kb_id": KB_A,
        "score": score,
    }


class _FakeVectorStore:
    """只走向量一路造出 20 条候选：融合池因此正好是 ``RERANK_MAX_CANDIDATES``。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, index_versions, top_k):
        return list(self.rows)


class _FakeKeyword:
    """关键词一路空结果：既不是故障、也不影响候选池宽度。"""

    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return []


class _FakeLLM(LLMProvider):
    def __init__(self, *, ranking: Any = None, error: Exception | None = None) -> None:
        self._ranking = ranking
        self._error = error
        self.calls: list[str] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        self.calls.append(user)
        if self._error is not None:
            raise self._error
        return self._ranking

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("重排只走结构化输出")


@contextmanager
def _offline_client(monkeypatch: pytest.MonkeyPatch, *, llm: LLMProvider) -> Iterator[TestClient]:
    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    rows = [_row(index, score=1.0 - index / 100) for index in range(CANDIDATES)]
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore(rows))
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: _FakeKeyword())
    monkeypatch.setattr(
        kb_repo, "get_by_id_for_tenant_async", async_visible_kb
    )
    # 生产工厂已不再返回大模型重排（#116），替身因此接在**既有**的工厂出口上。
    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    provider = LLMRerankProvider(
        llm=llm,
        max_candidates=settings.RERANK_MAX_CANDIDATES,
        chunk_max_chars=settings.RERANK_CHUNK_MAX_CHARS,
        timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: provider)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        _FakeDb()
    )
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()


def _retrieve(client: TestClient, *, top_k: int = TOP_K) -> dict:
    # 票 #77：``mode`` / ``top_k`` 改由密钥配置表达（custom 档），请求体只剩两个字段。
    IDENTITY.use(mode="hybrid", top_k=top_k)
    response = client.post(
        RETRIEVE_PATH,
        json={"kb_ids": [KB_A], "query": "退款怎么走"},
    )
    assert response.status_code == 200
    return response.json()


def test_rerank_failure_still_returns_exactly_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """重排失败 → 回到融合顺序，但**条数仍是 top_k**，不是融合池的宽度。"""

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", CANDIDATES)
    llm = _FakeLLM(error=RuntimeError("模型服务 502"))

    with _offline_client(monkeypatch, llm=llm) as client:
        body = _retrieve(client)

    assert body["code"] == 0, "重排失败不冒泡成整单失败"
    chunks = body["data"]["retrieved_chunks"]
    assert len(chunks) == TOP_K, f"top_k={TOP_K} 的请求必须恰好回 {TOP_K} 条"

    metadata = body["data"]["metadata"]["retrieval"]
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "rerank failed"
    # 候选池确实是 20 条：证明这条断言不是「本来就只有 5 条」蒙对的。
    assert metadata["rerank"]["candidates"] == CANDIDATES
    # 回的是融合顺序的前 top_k 条，不是随便哪几条。
    assert [chunk["chunk_id"] for chunk in chunks] == [f"c{index:02d}" for index in range(TOP_K)]


def test_partial_ranking_still_returns_exactly_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型只排了部分候选（漏掉的按 0 分退到后面）→ 仍然恰好 top_k 条。"""

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", CANDIDATES)
    # 只给 3 条打分，剩下 17 条模型压根没提。
    llm = _FakeLLM(
        ranking=RerankRanking(
            ranking=[
                RerankScore(chunk_id="c07", score=0.9),
                RerankScore(chunk_id="c03", score=0.6),
                RerankScore(chunk_id="c11", score=0.4),
            ]
        )
    )

    with _offline_client(monkeypatch, llm=llm) as client:
        body = _retrieve(client)

    chunks = body["data"]["retrieved_chunks"]
    assert len(chunks) == TOP_K, "重排成功也要截到 top_k"
    assert [chunk["chunk_id"] for chunk in chunks[:3]] == ["c07", "c03", "c11"]
    assert body["data"]["metadata"]["retrieval"]["degraded"] is False
    assert body["data"]["metadata"]["retrieval"]["rerank"]["candidates"] == CANDIDATES
