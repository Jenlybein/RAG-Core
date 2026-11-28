"""查询优化接入检索编排：两路各拿自己的查询（ASGI 接缝）。

接缝仍是既有的那一个：测试客户端打 ``POST /api/v1/rag/retrieve``，数据库依赖用
``dependency_overrides`` 换掉，Provider 工厂用离线替身换掉（与
``tests/test_hybrid_retrieval.py`` / ``tests/test_hybrid_rerank.py`` 同一套做法，没有
新造接缝）。断言的只有响应体与替身收到的调用，**不发网络、不连 PostgreSQL、不读 .env**。

本文件守的契约（父 spec「查询优化」「降级矩阵」、GLOSSARY「检索链路」）：

* 混合模式：**两路拿同一组句子**（原句 + 改写句，第 15 步起对称）——两个替身各自记账，
  证明它们收到的是同一对句子；
* 单模式也一致：``mode=vector`` 与 ``mode=bm25`` 都逐句搜这一组（否则会出现「开了改写但
  vector 模式只搜一句」这种意外）；
* **重排拿原句**：改写只负责召回，不参与排序判据；
* 改写失败：整单 ``50003``，不是 ``code=0`` 的降级结果；
* **两个开关都关闭**时：两路都拿原句、一次模型都不调、响应与票 4 逐字段一致。

第 15 步删掉了「词表来自知识库 settings」那一节（同义词扩展已下线）：
``kb_settings`` 这个接缝还留着，但库里存着旧的 ``synonyms`` 也不会再被读取。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.providers.llm.base import LLMProvider
from app.providers.rerank.llm import LLMRerankProvider
from app.services.query.rewrite import QueryRewrite
from tests.fake_db import AsyncSessionAdapter
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
REWRITE_PATH = "/api/v1/rag/rewrite"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"
KB_B = "kb-b"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)

REASON_REWRITE_FAILED = "query rewrite failed"


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
    """只回答「这些库里共有多少条可检索切片」——空结果原因那一次查询要用。"""

    def __init__(self, *, indexed_chunks: int = 1) -> None:
        self.indexed_chunks = indexed_chunks

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(self.indexed_chunks)

    def close(self) -> None:
        pass


class _RecordingEmbedding:
    """向量一路的入口：``embed_query`` 收到的句子就是向量一路真正用的查询。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, kb_ids, top_k):
        kb_id = next(iter(kb_ids), None)
        return [{**row, "kb_id": row.get("kb_id", kb_id)} for row in self.rows]

    async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
        raise AssertionError("多库检索必须一次范围召回，不许按库循环")


class _RecordingKeyword:
    """关键词一路的入口：``keyword_search`` 收到的句子就是关键词一路真正用的查询。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.available = True
        self.queries: list[str] = []

    async def keyword_search(self, query, *, tenant_id, kb_ids, top_k):
        self.queries.append(query)
        return [dict(row) for row in self.rows]


class _SchemaAwareLLM(LLMProvider):
    """一个替身同时服务改写与重排：按 schema 分辨这次是哪一个阶段。

    重排也拿 ``effective_query`` 是本票的契约之一，而它只能从提示词里看出来——所以这里
    把两次调用的 user 提示词都记下来，各自断言。
    """

    def __init__(self, *, rewritten: str | None = None, error: Exception | None = None) -> None:
        self._rewritten = rewritten
        self._error = error
        self.rewrite_prompts: list[str] = []
        self.rerank_prompts: list[str] = []
        self.schemas: list[str] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        name = getattr(schema, "__name__", str(schema))
        self.schemas.append(name)
        if name == "QueryRewrite":
            self.rewrite_prompts.append(user)
            if self._error is not None:
                raise self._error
            return QueryRewrite(query=self._rewritten or "")
        # 重排：返回一个空排序（够用即可——本文件不关心排出来的顺序）。
        from app.providers.rerank.llm import RerankRanking

        self.rerank_prompts.append(user)
        return RerankRanking(ranking=[])

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写与重排都只走结构化输出")


def _vector_row(chunk_id: str) -> dict:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": 0.9,
    }


def _keyword_row(chunk_id: str) -> dict:
    return {**_vector_row(chunk_id), "score": 9.0, "kb_id": KB_A}


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kb_settings: dict[str, dict] | None = None,
    llm: LLMProvider | None = None,
    keyword: Any = None,
) -> Iterator[dict[str, Any]]:
    """离线客户端：``kb_settings`` 是「kb_id → 那一行的 settings（含同义词表）」。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = _RecordingEmbedding()
    keyword = (
        keyword
        if keyword is not None
        else _RecordingKeyword([_keyword_row("c1"), _keyword_row("c2")])
    )
    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore([_vector_row("c1")]))
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)

    tables = kb_settings or {}

    async def _kb_for_tenant(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        rows = tables.get(kb_id)
        return SimpleNamespace(
            id=kb_id,
            tenant_id=tenant_id,
            settings=rows or {},
            name=f"知识库-{kb_id}",
            description="制度与流程",
            # 真行上有的两列（票 #141）：检索范围从库行里读生效版本。
            active_index_version=DEFAULT_INDEX_VERSION,
        )

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _kb_for_tenant)

    if llm is not None:
        # 改写与重排都是惰性 import 的，所以替换点仍在「大模型能力出口」上。
        async def _llm_factory(role: str = "fast") -> LLMProvider:
            return llm

        monkeypatch.setattr("app.providers.llm.get_llm_provider", _llm_factory)
        # #116 起生产工厂装的是专用端点，不再返回大模型重排；要测「重排看到哪一句」就得
        # 显式装配旧实现，并且接在**既有**的工厂出口上（同一个替身大模型）。
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
            yield {"client": client, "embedding": embedding, "keyword": keyword}
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, *, path: str = RETRIEVE_PATH, **body: Any) -> dict:
    payload = {"kb_ids": [KB_A], "query": "退钱怎么弄"}
    # 第 13 步：重排由配置直存参数决定，用 RERANK_ENABLED 同步表达「配置想要重排」。
    rerank = bool(getattr(settings, "RERANK_ENABLED", False))
    payload.update(IDENTITY.apply({"mode": "hybrid", "rerank": rerank, **body}))
    response = client.post(path, json=payload)
    assert response.status_code == 200
    return response.json()


def _retrieval_metadata(body: dict) -> dict:
    return body["data"]["metadata"]["retrieval"]


def _chunk_ids(body: dict) -> list[str]:
    return [chunk["chunk_id"] for chunk in body["data"]["retrieved_chunks"]]


# ─────────────────────────────────────────────── 混合模式：两路各拿自己的


def test_hybrid_mode_feeds_both_channels_the_same_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**两路拿同一组句子**：向量与关键词都搜「原句 + 改写句」（第 15 步）。

    第 15 步之前这里是「向量两句、关键词第三句（同义词扩展后的改写句）」，
    所以旧断言里那句 ``!=``「两路拿不同的查询是设计」现在正好反过来。
    """

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    llm = _SchemaAwareLLM(rewritten="退款的处理流程")

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"], path=REWRITE_PATH)

    assert body["code"] == 0
    # 改写开启是「原句 + 改写句都搜」而不是替换；顺序固定为 raw → rewritten。
    assert fake["embedding"].queries == ["退钱怎么弄", "退款的处理流程"]
    assert fake["keyword"].queries == ["退钱怎么弄", "退款的处理流程"], "关键词一路也搜这两句"
    assert fake["embedding"].queries == fake["keyword"].queries, "两路对用句的看法必须一致"
    assert _retrieval_metadata(body)["degraded"] is False
    # 响应里的 query 仍是**原话**：调用方看到的是自己问的那句。
    assert body["data"]["query"] == "退钱怎么弄"
    # 实际检索用句也留档——没有它，「这题为什么变差」无法归因。
    optimization = _retrieval_metadata(body)["query_optimization"]
    assert optimization["rewrite_applied"] is True
    assert optimization["effective_query"] == "退款的处理流程"
    assert optimization["recall_queries"] == ["退钱怎么弄", "退款的处理流程"]


def test_rerank_uses_the_raw_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """重排与改写无关：它看到的是**原句**（改写只负责召回）。

    这是第 15 步有意改掉的口径：改写是为了「换一种说法去捞切片」，不是「替用户重新
    表述问题」。让重排按改写句排序，等于让一次字面改写去影响语义排序的判据。
    """

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")

    llm = _SchemaAwareLLM(rewritten="退款的处理流程")

    with _offline_client(monkeypatch, llm=llm) as fake:
        _post(fake["client"], path=REWRITE_PATH)

    assert llm.schemas[:2] == ["QueryRewrite", "RerankRanking"], "先改写、后重排"
    assert llm.rerank_prompts, "重排真的调用了模型"
    assert "退钱怎么弄" in llm.rerank_prompts[0], "重排看到的是原句"
    assert "退款的处理流程" not in llm.rerank_prompts[0], "改写句只去召回，不进重排"


# ─────────────────────────────────── 第 13 步：改写模型失败即整单 50003


def test_a_failed_rewrite_is_a_50003_and_touches_no_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/rag/rewrite` 模型失败 → 50003，不降级、不返回切片、不烧一次召回。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    llm = _SchemaAwareLLM(error=TimeoutError("模型超时"))

    with _offline_client(
        monkeypatch,
        llm=llm,
    ) as fake:
        body = _post(fake["client"], path=REWRITE_PATH)

    assert body["code"] == 50003, "模型失败不再包成 code 0 的降级结果"
    assert body["data"] is None
    assert fake["embedding"].queries == [], "被拒的请求不该白烧一次嵌入"


def test_a_failed_rewrite_happens_before_any_channel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型失败发生在召回之前：后面的通道故障根本没有机会发生。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    class _BrokenKeyword(_RecordingKeyword):
        async def keyword_search(self, query, *, tenant_id, kb_ids, top_k):
            raise RuntimeError("connection refused: elasticsearch")

    llm = _SchemaAwareLLM(error=RuntimeError("模型服务 502"))

    with _offline_client(monkeypatch, llm=llm, keyword=_BrokenKeyword()) as fake:
        body = _post(fake["client"], path=REWRITE_PATH)

    assert body["code"] == 50003
    assert body["data"] is None


# ─────────────────────────────────────────────── 单模式：各自生效


def test_vector_mode_searches_both_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mode=vector`` 同样逐句搜这两句：单模式不是「只搜改写句」的例外。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    llm = _SchemaAwareLLM(rewritten="退款的处理流程")

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"], path=REWRITE_PATH, mode="vector")

    assert body["code"] == 0
    # #31：单模式同样吃并集，顺序固定；改写句仍在其中（effective_query 语义保留）。
    assert fake["embedding"].queries == ["退钱怎么弄", "退款的处理流程"]
    assert fake["keyword"].queries == [], "单模式不碰另一路"


def test_bm25_mode_searches_both_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mode=bm25`` 逐句搜这两句：字面那一路的并集在单模式下也要成立。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    llm = _SchemaAwareLLM(rewritten="退款的处理流程")

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"], path=REWRITE_PATH, mode="bm25")

    assert body["code"] == 0
    assert fake["keyword"].queries == ["退钱怎么弄", "退款的处理流程"]
    assert fake["embedding"].queries == [], "单模式不碰另一路"


# ─────────────────────────────────────────── 两个开关都关闭：与票 4 一致


def test_both_switches_off_keeps_the_raw_query_on_both_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归红线：两路都拿原句、一次模型都不调、``degraded=false``。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    async def _forbidden(_role: str = "fast") -> LLMProvider:  # pragma: no cover
        raise AssertionError("两个开关都关时不许装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    # 库里就算还留着旧的 synonyms 配置也不该扩展：这一路已经不存在了。
    with _offline_client(monkeypatch) as fake:
        body = _post(fake["client"])

    assert fake["embedding"].queries == ["退钱怎么弄"]
    assert fake["keyword"].queries == ["退钱怎么弄"]
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is False
    assert metadata["degraded_reason"] is None
