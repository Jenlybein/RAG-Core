"""第 13 步 · 四条数据面端点的存在性与最小契约（ASGI 接缝）。

复用 ``test_multi_query_union`` 里那套离线替身（既有接缝，不新造）：三个 Provider 工厂
与知识库行都换成不发网络的替身，只验「四条端点各自存在、请求体同形、回包形状」。

* `/rag/retrieve`：零模型调用，恒等原句；
* `/rag/rewrite`：一次改写，向量两句并池；
* `/rag/decompose`：一次分解 + N 次内核，合并成一份带 ``matched_queries`` 的列表；
* `/rag/research`：既有端点，仍在。

真值是 spec 里拍过的形状，不从实现反推。
"""

from __future__ import annotations

import pytest

import tests.test_multi_query_union as u
from app.core.config import settings
from app.services.query.rewrite import QueryRewrite


class _PlanLLM(u._RewriteLLM):
    """分解端点的模型替身：按 schema 分辨「规划」与「改写」。"""

    def __init__(self, plan: dict) -> None:
        super().__init__(rewritten="")
        self._plan = plan
        self.prompts: list[str] = []

    async def chat_structured(self, schema, *, system: str, user: str):
        name = getattr(schema, "__name__", str(schema))
        self.prompts.append(user)
        if name == "ResearchPlan":
            return self._plan
        return QueryRewrite(query="").model_dump()


def _client(monkeypatch: pytest.MonkeyPatch, *, llm=None):
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    return u._offline_client(monkeypatch, llm=llm)


# ───────────────────────────────────────────────────────── 端点存在性


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/rag/retrieve",
        "/api/v1/rag/rewrite",
        "/api/v1/rag/decompose",
        "/api/v1/rag/research",
    ],
)
def test_all_four_endpoints_exist(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """四条端点都注册在 /api/v1/rag/ 下；请求体只有 kb_ids + query。"""

    with _client(monkeypatch) as fake:
        response = fake["client"].post(path, json={"kb_ids": [u.KB_A], "query": "退钱怎么弄"})

    # 不是 404（存在性）；业务结果由该端点自己的用例守。
    assert response.status_code == 200, path
    assert response.json()["code"] in (0, 20013, 50003, 20002), path


def test_retrieve_is_zero_model_call_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/rag/retrieve` 恒等原句：向量一路只有一句，不装配任何模型。"""

    async def _forbidden(_role: str = "default"):
        raise AssertionError("/rag/retrieve 不该装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    with _client(monkeypatch) as fake:
        body = u._response(fake["client"]).json()

    assert body["code"] == 0
    assert fake["embedding"].queries == ["退钱怎么弄"]


def test_rewrite_embeds_both_the_raw_and_the_rewritten_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/rag/rewrite` 一次改写，向量一路两句并池。"""

    llm = u._RewriteLLM(rewritten=u.REWRITTEN)
    with _client(monkeypatch, llm=llm) as fake:
        body = u._response(fake["client"], path=u.REWRITE_PATH).json()

    assert body["code"] == 0
    assert fake["embedding"].queries == [u.RAW_QUERY, u.REWRITTEN]
    assert body["data"]["metadata"]["retrieval"]["query_optimization"]["rewrite_applied"] is True


# ───────────────────────────────────────────────────────── decompose


def _plan() -> dict:
    return {
        "points": ["退款时限", "所需材料"],
        "queries": [
            {"point": 1, "query": "退款多久到账"},
            {"point": 2, "query": "退款需要哪些材料"},
        ],
    }


def test_decompose_merges_into_one_list_with_matched_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/rag/decompose`：N 次内核 → 一份列表，每条标出把它顶上来的子查询。"""

    llm = _PlanLLM(_plan())
    vector = u._FakeVectorStore(rows=[u._vector_row("c1", score=0.9)])
    keyword = u._FakeKeyword(rows=[u._keyword_row("c1")])

    # decompose 的子检索内核由 research_gate.build_kernel_search 装配，它从自己模块的
    # 名字空间取三个工厂——与 rag_service 的注入点是两处，都要换成同一份离线替身。
    import importlib

    research_gate = importlib.import_module("app.services.research_gate")
    monkeypatch.setattr(research_gate, "get_vector_store", lambda: vector)
    monkeypatch.setattr(research_gate, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(
        research_gate, "get_embedding_provider", lambda: u._RecordingEmbedding()
    )

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    with u._offline_client(
        monkeypatch, llm=llm, vector=vector, keyword=keyword
    ) as fake:
        body = fake["client"].post(
            "/api/v1/rag/decompose",
            json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY},
        ).json()

    assert body["code"] == 0, body
    chunks = body["data"]["retrieved_chunks"]
    # 两条子查询各保底 1 条，但这里两路命中同一节 → 去重成 1 条。
    assert len(chunks) == 1
    assert set(chunks[0]["matched_queries"]) == {
        "退款多久到账",
        "退款需要哪些材料",
    }
    meta = body["data"]["metadata"]["retrieval"]["decomposition"]
    assert meta["points"] == ["退款时限", "所需材料"]
    assert meta["queries"] == ["退款多久到账", "退款需要哪些材料"]


def test_a_failed_decomposition_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """分解模型失败 → 整单 50003，不退回原句直查。"""

    class _Boom(u._RewriteLLM):
        async def chat_structured(self, schema, *, system: str, user: str):
            raise RuntimeError("模型服务 502")

    with _client(monkeypatch, llm=_Boom()) as fake:
        body = fake["client"].post(
            "/api/v1/rag/decompose",
            json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY},
        ).json()

    assert body["code"] == 50003
