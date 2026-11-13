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

import asyncio
import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.result_collapse import OVERFETCH_FACTOR
from app.core.exceptions import AppError, ErrorCode
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.services.retrieval_options import RetrievalOptions
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"
KB_B = "kb-b"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」，
# 所以下面四十来个调用点保持原样。
IDENTITY = RetrievalIdentity(TENANT_ID)


# ─────────────────────────────────────────────────────────────────── 替身


class _FakeEmbedding:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """向量库替身：单库原语（``similarity_search``）与范围版都实现。

    票 5 起 ``mode=vector`` 也走范围版（一次召回覆盖全部配对）。单库时范围里只有
    一个配对，与旧的逐库调用等价，所以这里把每次配对记成 ``{tenant_id, kb_id,
    top_k}``——回归断言「单库只查一次、用这个库、这个深度」的语义一个字没变。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.scope_calls: list[dict[str, Any]] = []

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

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, index_versions, top_k):
        self.scope_calls.append(
            {"tenant_id": tenant_id, "index_versions": dict(index_versions), "top_k": top_k}
        )
        rows: list[dict[str, Any]] = []
        for kb_id in index_versions:
            self.calls.append({"tenant_id": tenant_id, "kb_id": kb_id, "top_k": top_k})
            score = 0.9 if kb_id == KB_A else 0.5
            rows.append(
                {
                    "document_id": f"doc-{kb_id}",
                    "chunk_id": f"chunk-{kb_id}",
                    "kb_id": kb_id,
                    "title": "title",
                    "content": "content",
                    "score": score,
                }
            )
        return rows


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
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = _FakeEmbedding()
    vector = _FakeVectorStore()
    keyword = _FakeKeywordSearch()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    # 可见性走异步仓储（第 11 步票 #127）：替身因此也得是 ``async def``。
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    from app.api.deps import get_current_key

    # 检索链路走异步依赖（第 11 步票 #127）：同步那条留给身份判定与其他路由。
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: (
        AsyncSessionAdapter(EmptySession())
    )

    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app) as client:
            yield {"client": client, "embedding": embedding, "vector": vector, "keyword": keyword}
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, **body) -> dict:
    payload = {"kb_ids": [KB_A], "query": "E1024"}
    payload.update(IDENTITY.apply(body))
    response = client.post(RETRIEVE_PATH, json=payload)
    assert response.status_code == 200
    return response.json()


# ────────────────────────────────────────────── mode=vector（回归红线）


def test_vector_mode_is_unchanged(offline_retrieval) -> None:
    """``mode=vector`` 与之前完全一致：单库一次召回、score 仍是向量分。

    票 5 起召回换成范围版（一次覆盖全部「库 + 版本」配对）：单库时范围里只有一个
    配对，SQL 语义与逐库版相同，所以这条回归红线（同一个库、同一个深度、同样的
    分数口径）继续成立。

    注意：票 4 起**不传 mode 的缺省值已是 hybrid**（父 spec：缺省值取服务端配置），
    所以这里必须显式传 ``vector``，否则测的就不是这条回归红线了。
    缺省值来自配置这一点由下面 ``test_mode_default_comes_from_server_config`` 守。
    """

    body = _post(offline_retrieval["client"], mode="vector")

    assert body["code"] == 0
    assert [c["document_id"] for c in body["data"]["retrieved_chunks"]] == ["doc-kb-a"]
    assert offline_retrieval["embedding"].queries == ["E1024"]
    assert offline_retrieval["vector"].calls == [
        {"tenant_id": TENANT_ID, "kb_id": KB_A, "top_k": settings.TOP_K * OVERFETCH_FACTOR}
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


def test_vector_mode_covers_all_kbs_in_one_scope_call(offline_retrieval) -> None:
    """``mode=vector`` 的多库也是一次全局召回：一个范围调用覆盖两个配对。

    如果只有 hybrid 是全局召回、vector 仍分库召回，「假均衡」就只是换了个 mode
    藏起来——同一个 API 里不许有两套多库语义。
    """

    _post(offline_retrieval["client"], mode="vector", kb_ids=[KB_A, KB_B])

    assert offline_retrieval["vector"].scope_calls == [
        {
            "tenant_id": TENANT_ID,
            "index_versions": {KB_A: "v1", KB_B: "v1"},
            "top_k": settings.TOP_K * OVERFETCH_FACTOR,
        }
    ]
    assert offline_retrieval["embedding"].queries == ["E1024"], "多库也只嵌入一次"


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
            "top_k": settings.TOP_K * OVERFETCH_FACTOR,
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


def test_hybrid_mode_is_accepted_and_fuses_both_channels(offline_retrieval) -> None:
    """票 4 起 ``hybrid`` 真的可用：两路都走，结果融合，降级标记为假。

    深度覆盖（四种降级组合、对称降级、空结果原因、真并行）在
    ``tests/test_hybrid_retrieval.py``；这里只守住「允许值放开了，行为也真的在」。
    """

    body = _post(offline_retrieval["client"], mode="hybrid")

    assert body["code"] == 0
    assert offline_retrieval["embedding"].queries == ["E1024"]
    assert offline_retrieval["vector"].calls, "混合模式必须走向量一路"
    assert offline_retrieval["keyword"].calls, "混合模式必须走关键词一路"
    assert {c["chunk_id"] for c in body["data"]["retrieved_chunks"]} == {
        "chunk-kb-a",
        "chunk-hit",
    }
    retrieval = body["data"]["metadata"]["retrieval"]
    assert retrieval["mode"] == "hybrid"
    assert retrieval["degraded"] is False


def test_metadata_omits_optimization_block_when_rewrite_is_off(offline_retrieval) -> None:
    """改写关闭时**不加** ``query_optimization``（#31）。

    关闭时查询优化是恒等变换（用句就是原话），加这一栏不增加任何可归因信息，
    只会让「关闭改写时响应逐字节不变」那条回归红线失效。这条用例钉的是「不加」，
    「加了且内容正确」由 ``tests/test_query_optimization_retrieval.py`` 钉。
    """

    body = _post(offline_retrieval["client"])

    assert "query_optimization" not in body["data"]["metadata"]["retrieval"]


@pytest.mark.parametrize("top_k", [0, -1, 51, 1000])
def test_top_k_is_no_longer_a_request_field(offline_retrieval, top_k: int) -> None:
    """``top_k`` 已不在请求体里：传任何值都是 ``20002``，不进检索链路。

    旧用例守的是「``top_k`` 的上下界」——那个字段现在是密钥配置的一部分
    （``app/services/retrieval_options.py`` 的 ``MIN_TOP_K`` / ``MAX_TOP_K``），
    越界值按缺键处理、回落到服务端缺省。请求这一侧只剩一件事可测：它不该再被接受。
    所以这里**绕过请求助手直接发**——助手的作用恰恰是把这四个旧字段翻译成配置，
    用它来测「字段不存在」是自相矛盾的。
    """

    response = offline_retrieval["client"].post(
        RETRIEVE_PATH, json={"kb_ids": [KB_A], "query": "E1024", "top_k": top_k}
    )

    assert response.status_code == 200
    assert response.json()["code"] == 20002
    assert offline_retrieval["vector"].calls == []


def test_request_top_k_is_overfetched_before_collapsing(offline_retrieval) -> None:
    """显式 top_k 一路穿到通道，也回显在 metadata 里。"""

    body = _post(offline_retrieval["client"], mode="vector", top_k=2)

    # 折叠会把同一父节的命中并成一条，所以召回要多取 OVERFETCH_FACTOR 倍；
    # 对外承诺的 top_k 不变（下一行）。
    assert offline_retrieval["vector"].calls[0]["top_k"] == 2 * OVERFETCH_FACTOR
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


# ───────────────────────────────────────── mode 校验与可见性预校验的先后顺序


class _ExplodingSession:
    """只要被访问就炸的会话替身：用来证明「这段逻辑一次都没碰数据库」。"""

    def get(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 访问即失败
        raise AssertionError("顺序错了：mode 校验之后、可见性校验之前不应该访问数据库")

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 访问即失败
        # 可见性守卫现在是 `select(...).where(id, tenant_id)` 这一次查询，走的是 execute。
        raise AssertionError("顺序错了：mode 校验之后、可见性校验之前不应该访问数据库")


def test_unknown_mode_wins_over_invisible_kb() -> None:
    """未知 ``mode`` + 不可见 ``kb_id``：先撞参数校验（20002），不是可见性（20008）。

    **为什么断言这个顺序**（票 6 把 mode 校验提到了最前面）：

    * 这条组合**只能在服务层测**。HTTP 入口的请求体用 ``Literal`` 守着 ``mode``，
      未知值在 schema 层就被 20002 挡掉了，根本到不了 ``retrieve``；能带着未知 mode
      进来的只有 MCP 入口与内部调用，它们直接调 ``retrieve``。所以这条用例不走
      ``TestClient``，而是对着服务函数本身断言。
    * 顺序的两条理由：**(1)** 被拒的请求不该白烧一次改写——校验放在可见性预校验与
      查询优化之前，一次参数拼错不会先查一次库、再调一次模型；**(2)** 参数错误的表现
      不该取决于「这个库恰好可不可见」——反过来（先校验可见性）会让同一个拼写错误
      有时报 20002、有时报 20008，排障时最容易被这两条来回误导。

    ``_ExplodingSession`` 是这条断言的牙齿：若有人把可见性预校验提到前面，
    ``ensure_visible`` 会先 ``db.execute(...)``（按 id + 租户取库那一次查询），用例当场炸在
    ``AssertionError`` 上，而不是悄悄换一个业务码继续通过。
    """

    service = importlib.import_module("app.services.rag_service")

    with pytest.raises(AppError) as excinfo:
        asyncio.run(
            service.retrieve(
                _ExplodingSession(),
                tenant_id=TENANT_ID,
                kb_ids=["kb-a"],
                query="E1024",
                # 未知取值：正常入口（HTTP / MCP）都进不来（请求体里没有这个字段、
                # 配置里的值也验过词表），只有内部调用能造出这样的 options。
                options=RetrievalOptions(
                    mode="bing",
                    top_k=settings.TOP_K,
                    fold_to_sections=True,
                    expand_table_rows=True,
                    rerank=False,
                    rewrite=False,
                ),
            )
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
