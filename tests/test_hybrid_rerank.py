"""重排接入编排：融合之后、组装之前（ASGI 接缝）。

接缝仍是既有的那一个：测试客户端打 ``POST /api/v1/rag/retrieve``，数据库依赖用
``dependency_overrides`` 换掉，Provider 工厂用离线替身换掉（与
``tests/test_hybrid_retrieval.py`` 同一套做法，没有新造接缝）。断言的只有响应体与
替身收到的调用，**不发网络、不连 PostgreSQL、不读 .env**。

本文件守的契约（父 spec「降级矩阵与三种结局」、GLOSSARY「重排」）：

* 关闭重排 → 顺序与融合后**完全一致**，且根本不去装配大模型；
* 开启重排 → 顺序按重排分变化、``score`` 是重排分、融合分仍在（可解释性不丢）；
* 重排抛错 / 返回空 / 返回不合法结构 → 保留融合顺序、``degraded=true``、
  原因固定 ``rerank failed``，业务码仍是成功；
* **已经因通道故障降级时，重排再失败不覆盖先到的原因**：一因一码，先到的才是根因。
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
from app.providers.llm.base import LLMProvider
from app.providers.rerank.llm import LLMRerankProvider, RerankRanking, RerankScore
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)

# 两路各两条时的手算融合顺序（k=60）：c1 双路第 1；c2 与 c3 都是单路第 2（1/62），
# tie-breaker 按 chunk_id 兜底 → c2 在 c3 前。顺序与 test_hybrid_retrieval.py 一致。
FUSION_ORDER = ["c1", "c2", "c3"]


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

    def __init__(self, *, indexed_chunks: int = 0) -> None:
        self.indexed_chunks = indexed_chunks
        self.closed = False

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(self.indexed_chunks)

    def close(self) -> None:
        self.closed = True


class _FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """离线向量库替身：这个文件只查一个库，所以行都归属同一个配对。

    票 5 起混合检索走**范围版**（一次召回覆盖全部配对，行自带 ``kb_id``）；
    单库版的 ``similarity_search`` 留作反例守卫：一旦按库循环就会在这里炸。
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, index_versions, top_k):
        kb_id = next(iter(index_versions), None)
        return [{**row, "kb_id": row.get("kb_id", kb_id)} for row in self.rows]

    async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
        raise AssertionError("混合模式必须一次范围召回，不许按库循环")


class _FakeKeyword:
    def __init__(self, rows: list[dict] | None = None, *, available: bool = True) -> None:
        self.rows = rows or []
        self.available = available

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return [dict(row) for row in self.rows]


class _FakeLLM(LLMProvider):
    """替身大模型：按预置结果作答或抛错；``delay`` 用来测超时。"""

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


def _vector_row(chunk_id: str, *, score: float) -> dict:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": score,
    }


def _keyword_row(chunk_id: str, *, score: float) -> dict:
    return {**_vector_row(chunk_id, score=score), "kb_id": KB_A}


def _ranking(*pairs: tuple[str, float]) -> RerankRanking:
    return RerankRanking(ranking=[RerankScore(chunk_id=cid, score=score) for cid, score in pairs])


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm: LLMProvider | None = None,
    keyword: Any = None,
) -> Iterator[dict[str, Any]]:
    """两路健康的混合检索客户端；``llm`` 给了才允许装配大模型。"""

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    service = importlib.import_module("app.services.rag_service")
    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    # 两路各两条：向量 c1 > c2，关键词 c1 > c3。
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(
        service,
        "get_vector_store",
        lambda: _FakeVectorStore([_vector_row("c1", score=0.93), _vector_row("c2", score=0.71)]),
    )
    monkeypatch.setattr(
        service,
        "get_keyword_search_provider",
        lambda: (
            keyword
            if keyword is not None
            else _FakeKeyword([_keyword_row("c1", score=12.4), _keyword_row("c3", score=9.1)])
        ),
    )
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        kb_repo, "get_by_id_for_tenant", lambda db, kb_id, *, tenant_id: SimpleNamespace(tenant_id=tenant_id)
    )

    if llm is not None:
        # 生产工厂已不再返回大模型重排（#116），所以替身接在**既有**的工厂出口上：
        # ``app/services/retrieval/rerank.py`` 里的 ``get_rerank_provider``。这样这一组
        # 仍然只测编排语义（顺序 / 降级 / 回显），不测「今天工厂装的是哪种实现」。
        provider = LLMRerankProvider(
            llm=llm,
            max_candidates=settings.RERANK_MAX_CANDIDATES,
            chunk_max_chars=settings.RERANK_CHUNK_MAX_CHARS,
            timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
        )
        monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: provider)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[session_mod.get_db] = lambda: _FakeDb()
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {"client": client}
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, **body: Any) -> dict:
    payload = {"kb_ids": [KB_A], "query": "退款怎么走"}
    payload.update(IDENTITY.apply({"mode": "hybrid", **body}))
    response = client.post(RETRIEVE_PATH, json=payload)
    assert response.status_code == 200
    return response.json()


def _retrieval_metadata(body: dict) -> dict:
    return body["data"]["metadata"]["retrieval"]


def _chunk_ids(body: dict) -> list[str]:
    return [chunk["chunk_id"] for chunk in body["data"]["retrieved_chunks"]]


# ───────────────────────────────────────────────────────────────── 关闭


def test_disabled_rerank_keeps_fusion_order_and_never_builds_an_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认关闭：顺序等于融合顺序，分数仍是融合分，且根本不去装配大模型。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    def _forbidden(_tier: str = "fast") -> LLMProvider:  # pragma: no cover
        raise AssertionError("关闭重排时不许装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    with _offline_client(monkeypatch) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0
    assert _chunk_ids(body) == FUSION_ORDER
    assert [c["score"] for c in body["data"]["retrieved_chunks"]] == [
        pytest.approx(2 / 61),
        pytest.approx(1 / 62),
        pytest.approx(1 / 62),
    ]
    assert _retrieval_metadata(body)["degraded"] is False
    assert _retrieval_metadata(body)["degraded_reason"] is None


# ───────────────────────────────────────────────────────────────── 开启


def test_enabled_rerank_reorders_and_score_is_the_rerank_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启后顺序按重排分走；``score`` 换成重排分，融合分仍可读。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    llm = _FakeLLM(ranking=_ranking(("c3", 9.0), ("c2", 5.0), ("c1", 2.0)))

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"])

    chunks = body["data"]["retrieved_chunks"]
    assert [c["chunk_id"] for c in chunks] == ["c3", "c2", "c1"]
    assert [c["score"] for c in chunks] == [9.0, 5.0, 2.0]
    # 可解释性不因重排丢失：融合分、分列分数与名次都原样在。
    assert [c["rrf_score"] for c in chunks] == [
        pytest.approx(1 / 62),
        pytest.approx(1 / 62),
        pytest.approx(2 / 61),
    ]
    assert chunks[2]["vector_rank"] == 1 and chunks[2]["bm25_rank"] == 1
    assert chunks[0]["content"] == "content-c3"
    assert _retrieval_metadata(body)["degraded"] is False
    assert llm.calls, "重排真的调用过模型"


def test_enabled_rerank_caps_candidates_sent_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选封顶在整条链路上生效：只有前 N 条进模型，返回也就不可能更多。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", 2)
    llm = _FakeLLM(ranking=_ranking(("c1", 3.0), ("c2", 2.0)))

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"], top_k=5)

    prompt = llm.calls[0]
    assert "c1" in prompt and "c2" in prompt
    assert "c3" not in prompt, "第三条候选不该送进模型"
    assert len(body["data"]["retrieved_chunks"]) == 2


# ─────────────────────────────────────────── 失败即降级：三种失败长相


def test_rerank_error_keeps_fusion_order_and_marks_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重排抛错：业务码仍成功，顺序回到融合顺序，原因固定 ``rerank failed``。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    llm = _FakeLLM(error=RuntimeError("模型服务 502"))

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0, "重排失败绝不冒泡成整单失败"
    assert _chunk_ids(body) == FUSION_ORDER
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "rerank failed", "一因一码，不拼异常文本"


def test_rerank_empty_ranking_keeps_fusion_order_and_marks_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    llm = _FakeLLM(ranking=_ranking())

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0
    assert _chunk_ids(body) == FUSION_ORDER
    assert _retrieval_metadata(body)["degraded_reason"] == "rerank failed"


@pytest.mark.parametrize("payload", [{"ranking": "不是列表"}, {"oops": 1}, None])
def test_rerank_malformed_result_keeps_fusion_order_and_marks_degraded(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    llm = _FakeLLM(ranking=payload)

    with _offline_client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0
    assert _chunk_ids(body) == FUSION_ORDER
    assert _retrieval_metadata(body)["degraded_reason"] == "rerank failed"


def test_rerank_failure_does_not_override_an_earlier_channel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经因通道故障降级 → 重排再失败也不覆盖：先到的那个才是根因。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    class _BrokenKeyword(_FakeKeyword):
        async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
            raise RuntimeError("connection refused: elasticsearch")

    with _offline_client(
        monkeypatch, llm=_FakeLLM(error=RuntimeError("模型超时")), keyword=_BrokenKeyword()
    ) as fake:
        body = _post(fake["client"])

    assert body["code"] == 0
    metadata = _retrieval_metadata(body)
    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "keyword search failed", "先到的原因不许被覆盖"
    assert _chunk_ids(body) == ["c1", "c2"], "向量一路的顺序就是这次的融合顺序"
