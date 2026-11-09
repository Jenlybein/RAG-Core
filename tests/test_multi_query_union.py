"""#31：改写开启时向量一路「原句 + 改写句」都搜，合成一个池再进既有 RRF。

两个接缝，都是仓库里既有的：

* 模块接缝 ``optimize_query``：断言 ``QueryProcessResult.vector_queries`` 的开关 / 失败 /
  去重语义，以及 ``effective_query`` / ``search_query`` 原样不动；
* ASGI 接缝 ``POST /api/v1/rag/retrieve``：数据库依赖用 ``dependency_overrides`` 换掉，
  三个 Provider 工厂用离线替身换掉（与 ``tests/test_hybrid_retrieval.py`` 同一套做法）。
  全部替身不发网络、不连 PG / ES。

守的契约：

* 改写成功：嵌入收到**两句**（查询侧没有批量接口，故两次 ``embed_query`` 并发发出），
  向量库被查**两次**，关键词仍只有一次且拿 ``search_query``；
* 并集收益：原句命中 A、改写句命中 B，A 与 B 都进向量池；
* 去重取优：同一 ``(kb_id, chunk_id)`` 只出现一次，保留名次更好的那份内容、
  分数取更高者，池按分数降序 + 确定性 tie-breaker 排好再截断；
* 改写失败：只剩原句一路，``degraded`` / ``degraded_reason`` 语义不变；
* ``mode=vector`` 单模式同样搜两句；``mode=bm25`` 不受影响；
* **回归红线**：``QUERY_REWRITE_ENABLED=false`` 时 hybrid / vector / bm25 三种 mode
  的响应全文 SHA-256 与 ``f1ddb46`` 上实跑取到的基线逐一相等。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.services.query.optimizer import QueryProcessResult, optimize_query
from app.services.query.rewrite import QueryRewrite
from app.services.query.synonyms import collect_synonym_groups

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"

RAW_QUERY = "退钱怎么弄"
REWRITTEN = "退款的处理流程"

# 关闭改写时三种 mode 的响应全文 SHA-256。取值方式见 ``_BASELINE_SOURCE``：
# 在 f1ddb46（本票开工前、生产代码未改）上，用本文件同一个 ``_response_hashes``
# 实跑采集，不是手写的期望字符串。
_BASELINE_SOURCE = "f1ddb46 (feat/step4-t31-multi-query 开工点, 生产代码未改)"
_BASELINE_SHA256: dict[str, str] = {
    "hybrid": "78fb011de37e97b37257f19c48c3b2d176bdf45f842b96d961c2cb8d09c144c8",
    "vector": "8bbc63b426b4fff1529261388951abd11f6d9960f00a82e924608a8479f8a180",
    "bm25": "5c08c56cd144374803f7c53a22cba85a3be7f8736ee3baf1fb15eade7fc95259",
}


# ─────────────────────────────────────────────────────────────── 替身


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def all(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter(())


class _FakeDb:
    """只回答「库里有多少条可检索切片」；检索两侧都是替身，不真连 PostgreSQL。"""

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(1)

    def close(self) -> None:
        pass


class _RecordingEmbedding:
    """查询侧替身：每次 ``embed_query`` 记下句子，并给该次调用一个可辨认的向量标记。

    ``concurrent_probe=True`` 时插入一次 ``await``，让 ``max_in_flight`` 能证明
    两次嵌入是并发发出而不是一前一后串行（查询侧没有批量接口，并发是为了不付两次 RTT）。
    """

    def __init__(self, *, concurrent_probe: bool = False) -> None:
        self.queries: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._concurrent_probe = concurrent_probe

    async def embed_query(self, query: str) -> list[float]:
        marker = float(len(self.queries) + 1)
        self.queries.append(query)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self._concurrent_probe:
            await asyncio.sleep(0.01)
        self.in_flight -= 1
        return [marker, 0.2, 0.3]


class _FakeVectorStore:
    """按向量标记返回不同候选池，用来证明两句各自都真的查了向量库。

    标记 ``1.0`` = 第一句（原句），``2.0`` = 第二句（改写句）。没配 ``pools`` 时
    两次都返回同一份 ``rows``，供不关心候选来源的用例使用。
    """

    def __init__(
        self,
        pools: dict[float, list[dict]] | None = None,
        *,
        rows: list[dict] | None = None,
    ) -> None:
        self.pools = pools or {}
        self.rows = rows or []
        self.seen: list[float] = []
        self.calls: list[dict[str, Any]] = []

    async def similarity_search_scope(
        self, db, query_vector, *, tenant_id, index_versions, top_k
    ) -> list[dict]:
        marker = float(query_vector[0])
        self.seen.append(marker)
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "index_versions": dict(index_versions),
                "top_k": top_k,
            }
        )
        kb_id = next(iter(index_versions), None)
        rows = self.pools.get(marker, self.rows)
        return [{**row, "kb_id": row.get("kb_id", kb_id)} for row in rows]

    async def similarity_search(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise AssertionError("必须走一次范围召回，不许按库循环")


class _FakeKeyword:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.available = True
        self.queries: list[str] = []

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k) -> list[dict]:
        self.queries.append(query)
        return [dict(row) for row in self.rows]


class _RewriteLLM(LLMProvider):
    """改写阶段的模型替身；重排默认关闭，本文件不会走到别的 schema。"""

    def __init__(self, *, rewritten: str | None = None, error: Exception | None = None) -> None:
        self._rewritten = rewritten
        self._error = error

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        if self._error is not None:
            raise self._error
        return QueryRewrite(query=self._rewritten or "")

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写只走结构化输出")


def _vector_row(
    chunk_id: str, *, score: float, content: str | None = None, kb_id: str | None = None
) -> dict:
    row: dict[str, Any] = {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": content if content is not None else f"content-{chunk_id}",
        "score": score,
    }
    if kb_id is not None:
        row["kb_id"] = kb_id
    return row


def _keyword_row(chunk_id: str, *, score: float = 9.0) -> dict:
    return {**_vector_row(chunk_id, score=score), "kb_id": KB_A}


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: Any = None,
    vector: Any = None,
    keyword: Any = None,
    llm: LLMProvider | None = None,
    kb_settings: dict[str, dict] | None = None,
) -> Iterator[dict[str, Any]]:
    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    service = importlib.import_module("app.services.rag_service")
    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = embedding or _RecordingEmbedding()
    vector = vector or _FakeVectorStore(rows=[_vector_row("c1", score=0.9)])
    keyword = keyword if keyword is not None else _FakeKeyword(rows=[_keyword_row("c1")])

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _FakeDb())

    tables = kb_settings or {}

    def _get_by_id(_db: Any, kb_id: str) -> Any:
        return SimpleNamespace(
            tenant_id=TENANT_ID,
            settings=tables.get(kb_id) or {},
            name="",
            description="",
        )

    monkeypatch.setattr(kb_repo, "get_by_id", _get_by_id)
    if llm is not None:
        monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda role="fast": llm)

    from app.core.auth import DevKeyContext, resolve_dev_key

    main.app.dependency_overrides[session_mod.get_db] = lambda: _FakeDb()
    main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {
                "client": client,
                "embedding": embedding,
                "vector": vector,
                "keyword": keyword,
            }
    finally:
        main.app.dependency_overrides.clear()


def _response(client: TestClient, **body: Any) -> Any:
    payload: dict[str, Any] = {
        "kb_ids": [KB_A],
        "query": RAW_QUERY,
        "mode": "hybrid",
        "top_k": 5,
    }
    payload.update(body)
    return client.post(RETRIEVE_PATH, json=payload)


def _chunk_ids(body: dict) -> list[str]:
    return [chunk["chunk_id"] for chunk in body["data"]["retrieved_chunks"]]


def _metadata(body: dict) -> dict:
    return body["data"]["metadata"]["retrieval"]


def _response_hashes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """同一份 ASGI 请求，三种 mode 各自的响应全文 SHA-256。

    这个函数同时是基线采集脚本的入口：在 ``f1ddb46`` 上直接调它取到的字典，
    就是上面 ``_BASELINE_SHA256`` 的来源，因此测试与基线共用完全相同的替身与请求。
    """

    # 显式三个开关全关：黄金哈希守的是「默认路径」，不能被别的用例改过的 settings 影响。
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    hashes: dict[str, str] = {}
    for mode in ("hybrid", "vector", "bm25"):
        with _offline_client(monkeypatch) as fake:
            response = _response(fake["client"], mode=mode)
        assert response.status_code == 200
        hashes[mode] = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
    return hashes


def _switches(monkeypatch: pytest.MonkeyPatch, *, rewrite: bool, synonyms: bool = False) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", rewrite)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", synonyms)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)


def _install_llm(monkeypatch: pytest.MonkeyPatch, llm: LLMProvider) -> None:
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda role="fast": llm)


# ───────────────────────────────── 接缝一：optimize_query 产出两句话


def test_rewrite_off_yields_exactly_the_raw_vector_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭改写：向量一路只有原句，三个查询字段仍是恒等变换。"""

    _switches(monkeypatch, rewrite=False, synonyms=False)

    result = asyncio.run(optimize_query(query=RAW_QUERY))

    assert isinstance(result, QueryProcessResult)
    assert result.vector_queries == (RAW_QUERY,)
    assert result.raw_query == result.effective_query == result.search_query == RAW_QUERY
    assert result.rewrite_applied is False
    assert result.rewrite_degraded is False


def test_rewrite_success_yields_raw_then_effective_in_a_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启且成功：``(原句, 改写句)`` 顺序固定、两句都非空；effective 仍给重排。"""

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=REWRITTEN))

    result = asyncio.run(optimize_query(query=RAW_QUERY))

    assert result.vector_queries == (RAW_QUERY, REWRITTEN)
    assert all(query.strip() for query in result.vector_queries)
    assert result.effective_query == REWRITTEN
    assert result.search_query == REWRITTEN
    assert result.rewrite_applied is True


def test_rewrite_same_as_raw_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """两句相同时只留一句：不白付第二次嵌入，也不让同一句占两个名次。"""

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=RAW_QUERY))

    result = asyncio.run(optimize_query(query=RAW_QUERY))

    assert result.vector_queries == (RAW_QUERY,)
    assert result.effective_query == RAW_QUERY


def test_synonym_expansion_still_only_touches_the_keyword_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search_query`` 完全不变：同义词只追加给关键词一路，不污染向量查询池。"""

    _switches(monkeypatch, rewrite=True, synonyms=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=REWRITTEN))
    groups = collect_synonym_groups([{"synonyms": [{"terms": ["退款"], "expand": ["退款流程"]}]}])

    result = asyncio.run(optimize_query(query=RAW_QUERY, synonym_groups=groups))

    assert result.vector_queries == (RAW_QUERY, REWRITTEN)
    assert result.search_query == "退款的处理流程 退款流程"


def test_rewrite_failure_yields_only_the_raw_query_and_keeps_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改写失败：只剩原句一路（等同于关闭），降级事实一字不改。"""

    _switches(monkeypatch, rewrite=True, synonyms=True)
    _install_llm(monkeypatch, _RewriteLLM(error=TimeoutError("模型超时")))

    result = asyncio.run(
        optimize_query(
            query=RAW_QUERY,
            synonym_groups=collect_synonym_groups(
                [{"synonyms": [{"terms": ["退钱"], "expand": ["退款"]}]}]
            ),
        )
    )

    assert result.vector_queries == (RAW_QUERY,)
    assert result.effective_query == RAW_QUERY
    assert result.search_query == "退钱怎么弄 退款"
    assert result.rewrite_applied is False
    assert result.rewrite_degraded is True
    assert result.rewrite_reason == "query rewrite failed"


# ───────────────────────────────── 接缝二：ASGI 上的并集检索


def test_rewrite_on_embeds_and_searches_both_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启成功：嵌入收到两句、向量库查两次；关键词仍只一次且拿扩展后的句子。"""

    _switches(monkeypatch, rewrite=True, synonyms=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )
    keyword = _FakeKeyword(rows=[])

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=keyword,
        llm=llm,
        kb_settings={KB_A: {"synonyms": [{"terms": ["退款"], "expand": ["退款流程"]}]}},
    ) as fake:
        body = _response(fake["client"], include_parent=False).json()

    assert body["code"] == 0
    # 查询侧 EmbeddingProvider 没有批量接口：两句 = 两次 embed_query（并发发出）。
    assert fake["embedding"].queries == [RAW_QUERY, REWRITTEN]
    assert fake["vector"].seen == [1.0, 2.0], "两句各做一次范围召回"
    assert len(fake["vector"].calls) == 2
    assert fake["keyword"].queries == ["退款的处理流程 退款流程"], "关键词一路完全不变"
    assert _metadata(body)["degraded"] is False


def test_union_keeps_hits_that_only_one_of_the_two_queries_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并集收益的替身证据：原句只命中 A、改写句只命中 B，A 与 B 都进向量池。

    这是 q13（改写赢）与 q16（原句赢）能同时吃到而不必二选一的最小模型，
    不需要真模型、真库，也不需要跑评测数字。
    """

    _switches(monkeypatch, rewrite=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch, vector=vector, keyword=_FakeKeyword(rows=[]), llm=llm
    ) as fake:
        body = _response(fake["client"], include_parent=False).json()

    chunks = body["data"]["retrieved_chunks"]
    assert set(_chunk_ids(body)) == {"A", "B"}, "两边的命中都进了向量池"
    assert all(chunk["vector_rank"] is not None for chunk in chunks)
    assert all(chunk["bm25_rank"] is None for chunk in chunks), "关键词一路没喂结果"


def test_duplicate_hit_is_deduplicated_taking_better_rank_and_higher_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 chunk 两句都命中：只出现一次，保留名次更好的那份内容、分数取更高者。

    X 在原句里是第 1 名（0.40），在改写句里是第 2 名（0.90）。合并后应当是
    「第 1 名那份的 content」+「0.90 这个更高分」，而不是二选一整条覆盖。
    """

    _switches(monkeypatch, rewrite=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("X", score=0.40, content="from-raw")],
            2.0: [
                _vector_row("Z", score=0.80, content="from-rewrite-z"),
                _vector_row("X", score=0.90, content="from-rewrite-x"),
            ],
        }
    )

    with _offline_client(
        monkeypatch, vector=vector, keyword=_FakeKeyword(rows=[]), llm=llm
    ) as fake:
        body = _response(fake["client"], include_parent=False).json()

    chunks = body["data"]["retrieved_chunks"]
    assert len(chunks) == 2, "同一 (kb_id, chunk_id) 只留一条"
    matches = [chunk for chunk in chunks if chunk["chunk_id"] == "X"]
    assert len(matches) == 1
    assert matches[0]["content"] == "from-raw", "同名取 rank 更小的那一次"
    assert matches[0]["vector_score"] == 0.90, "分数取更高者"
    assert _chunk_ids(body) == ["X", "Z"], "池按分数降序，名次可复现"


def test_multi_query_embeddings_are_issued_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询侧没有批量接口，两次 ``embed_query`` 必须并发发出（约 1 次 RTT）。"""

    _switches(monkeypatch, rewrite=True)
    embedding = _RecordingEmbedding(concurrent_probe=True)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        embedding=embedding,
        vector=vector,
        keyword=_FakeKeyword(rows=[]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        _response(fake["client"], include_parent=False)

    assert embedding.queries == [RAW_QUERY, REWRITTEN]
    assert embedding.max_in_flight == 2, "两次嵌入同时在飞，而不是串行"


def test_rewrite_failure_uses_only_one_vector_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改写失败：嵌入与向量检索都只剩一次，降级语义不变。"""

    _switches(monkeypatch, rewrite=True)
    vector = _FakeVectorStore(rows=[_vector_row("c1", score=0.9)])

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=_FakeKeyword(rows=[]),
        llm=_RewriteLLM(error=RuntimeError("模型服务 502")),
    ) as fake:
        body = _response(fake["client"], include_parent=False).json()

    assert fake["embedding"].queries == [RAW_QUERY]
    assert fake["vector"].seen == [1.0]
    assert len(fake["vector"].calls) == 1
    assert _metadata(body)["degraded"] is True
    assert _metadata(body)["degraded_reason"] == "query rewrite failed"


def test_vector_single_mode_also_searches_both_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode=vector`` 单模式同样吃并集；``bm25`` 完全不碰嵌入。"""

    _switches(monkeypatch, rewrite=True)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=_FakeKeyword(rows=[_keyword_row("k1")]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        vector_body = _response(fake["client"], mode="vector", include_parent=False).json()

    assert fake["embedding"].queries == [RAW_QUERY, REWRITTEN]
    assert fake["vector"].seen == [1.0, 2.0]
    assert set(_chunk_ids(vector_body)) == {"A", "B"}
    assert fake["keyword"].queries == [], "单模式不碰另一路"


def test_one_failing_embedding_degrades_the_whole_vector_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两句里有一句嵌入失败：整路降级，绝不拿另一句的半份结果冒充完整池。"""

    _switches(monkeypatch, rewrite=True)

    class _OneFails(_RecordingEmbedding):
        async def embed_query(self, query: str) -> list[float]:
            if query == REWRITTEN:
                raise RuntimeError("embedding endpoint refused the connection")
            return await super().embed_query(query)

    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        embedding=_OneFails(),
        vector=vector,
        keyword=_FakeKeyword(rows=[_keyword_row("k1")]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        body = _response(fake["client"], include_parent=False).json()

    assert body["code"] == 0
    assert _metadata(body)["degraded"] is True
    assert _metadata(body)["degraded_reason"] == "vector search failed"
    assert _chunk_ids(body) == ["k1"], "只剩关键词一路，向量半份结果不参与融合"


def test_single_query_keeps_each_caller_historical_tie_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭 / 失败时同分多行：单模式稳定排序、混合分支按 kb_id / chunk_id 兜底。

    两条调用方历史上排序口径不同，黄金哈希的替身没有同分行，覆盖不到这一维；
    把它们钉住，才敢说「关闭时行为不变」不只是「关闭时那份特定响应不变」。
    """

    _switches(monkeypatch, rewrite=False)
    # 两条同分行：库返回顺序是 Z → A；稳定排序应保留它，tiebreak 应换成 A → Z。
    same_score_rows = [
        _vector_row("Z", score=0.5, kb_id=KB_A),
        _vector_row("A", score=0.5, kb_id=KB_A),
    ]

    with _offline_client(
        monkeypatch,
        vector=_FakeVectorStore(rows=same_score_rows),
        keyword=_FakeKeyword(rows=[]),
    ) as fake:
        single = _response(fake["client"], mode="vector", include_parent=False).json()
        hybrid = _response(fake["client"], mode="hybrid", include_parent=False).json()

    assert _chunk_ids(single) == ["Z", "A"], "单模式：稳定排序，同分保留库返回顺序"
    assert _chunk_ids(hybrid) == ["A", "Z"], "混合向量分支：同分按 kb_id / chunk_id 兜底"


# ───────────────────────────────── 回归红线：关闭时逐字节一致


def test_rewrite_off_response_is_byte_identical_to_pre_change_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``QUERY_REWRITE_ENABLED=false``：三种 mode 的响应全文与 f1ddb46 逐字节相同。

    基线常量取于 ``_BASELINE_SOURCE``，用本文件同一个 ``_response_hashes`` 在未改
    生产代码的提交上实跑采集。这条守的是本票最容易出的那类 bug：并集分支顺手让
    **关闭时**也多搜一句，让所有默认用户白付一次嵌入费用。
    """

    assert "PENDING" not in _BASELINE_SHA256.values(), "基线必须来自实跑，不能留占位"
    assert _response_hashes(monkeypatch) == _BASELINE_SHA256
