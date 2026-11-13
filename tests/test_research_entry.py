"""票 #148：``POST /api/v1/rag/research``——门禁、子检索的接线与响应形状。

这一层验的是**入口**，不是状态机（状态机在 ``test_research_state_machine.py`` 里逐条钉住）：

* **请求体只有 ``kb_ids`` + ``query``**：多给字段一律 ``20002``，不静默忽略；
* **没有 Research 授权 → ``20013``**，且**一次子检索、一次模型都不发生**（不静默降级成普通
  检索）——那是 ADR-0026 §1 的明确拒绝；
* **子检索不复用完整检索服务**：改写在 ``/rag/retrieve`` 那条路上，Research 里没有它；
  归属索引、配额记账、观测提交都是「每次调用一次」的事，因此都不在子检索里（ADR-0028）；
* **响应形状**：计划（要点 + 绑定要点的查询）、轮次只给计数、材料是交付单元（与
  ``/rag/retrieve`` 同一个值对象）、元数据带计划名 / 耗时 / 模型调用次数 / 停止原因；
  内部候选池一份都不出去；
* **任何模型阶段失败都不整单报错**：``code 0`` + 材料 + 一个 ``degraded``。
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.models.key_policy import PROFILE_SPEED, KeyPolicySnapshot
from app.models.plan import PLAN_PRESETS, PLAN_PRO, PlanSpec
from app.providers.llm.base import LLMProvider
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb

TENANT_ID = "tenant_demo"
RESEARCH_PATH = "/api/v1/rag/research"
KB = "kb-a"
QUERY = "退款多久到账，需要哪些材料"


# ─────────────────────────────────────────────────────────────── 离线替身


class _FakeEmbedding:
    """查询侧嵌入替身：**记下每一条被嵌入的句子**，并让向量自己带着那一句的身份。

    「子查询不触发改写」这条验收，在这里变成一个可数的东西：嵌入 provider 收到的句子
    必须逐字等于计划里的那几条查询——改写会把它变成另一个句子（或多出一条）。

    向量是「序号 → 句子」的钥匙（``vectors``），而不是固定的三元素常量：首轮两条子查询是
    **并行**发出的，向量库替身必须能分辨自己拿到的是哪一句的向量，否则两条子查询会读到
    同一批候选（那是夹具的 bug，不是被测行为）。
    """

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.vectors: dict[tuple[float, ...], str] = {}

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        # 同一个事件循环里这一小段没有 await：取号与登记因此是原子的。
        vector = (float(len(self.queries)), 0.0, 0.0)
        self.vectors[vector] = query
        return list(vector)


class _FakeVectorStore:
    """按「这一次拿到的是哪一句的向量」返回预置行：不同子查询命中不同的节。"""

    def __init__(self, embedding: _FakeEmbedding, rows_for: Mapping[str, Sequence[dict]]) -> None:
        self.embedding = embedding
        self.rows_for = dict(rows_for)
        self.scopes: list[dict[str, str]] = []

    async def similarity_search_scope(
        self, _db: Any, vector: Any, *, tenant_id: str, index_versions: Any, top_k: int
    ) -> list[dict]:
        self.scopes.append(dict(index_versions))
        query = self.embedding.vectors.get(tuple(vector), "")
        return [{**row, "kb_id": KB} for row in self.rows_for.get(query, ())][:top_k]


class _FakeKeyword:
    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return []


def _row(key: str, content: str) -> dict[str, Any]:
    return {
        "document_id": f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "title": "退款政策",
        "content": content,
        "score": 0.9,
        "section_id": f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


class _ScriptedLLM(LLMProvider):
    """规划 + 判断两次结构化调用；按调用顺序交出预置输出。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if not self._responses:
            raise AssertionError("Research 本票只该问两次模型")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _plan_output() -> dict:
    return {
        "points": ["退款时限", "所需材料"],
        "queries": [
            {"point": 1, "query": "退款多久到账"},
            {"point": 2, "query": "退款需要哪些材料"},
        ],
    }


def _covered() -> dict:
    return {"queries": []}


def _policy(*, research_allowed: bool = True) -> KeyPolicySnapshot:
    """这套集成的密钥配置：档位 ``speed``（走向量一路，替身只需一条通道）。"""

    return dataclasses.replace(
        DEV_KEY_POLICY,
        profile=PROFILE_SPEED,
        research_allowed=research_allowed,
    )


def _plan(*, research_allowed: bool = True) -> PlanSpec:
    return dataclasses.replace(PLAN_PRESETS[PLAN_PRO], research_allowed=research_allowed)


class _Entry:
    """一条 HTTP 入口 + 可改的身份 + 可观察的模型 / 候选替身。"""

    def __init__(self) -> None:
        self.llm: LLMProvider = _ScriptedLLM(_plan_output(), _covered())
        self.embedding = _FakeEmbedding()
        self.vector = _FakeVectorStore(
            self.embedding,
            {
                "退款多久到账": [_row("a", "超期退款需人工审核。")],
                "退款需要哪些材料": [_row("b", "3 个工作日内到账。")],
                # 降级出口：原问题那一次普通检索也要能命中东西（否则「材料照给」测不出来）。
                QUERY: [_row("fallback", "退款一般 3 个工作日内到账。")],
            },
        )
        self.holder: dict[str, KeyContext] = {
            "context": KeyContext(tenant_id=TENANT_ID, policy=_policy(), plan=_plan())
        }
        self.client: TestClient | None = None

    def use(self, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
        self.holder["context"] = KeyContext(
            tenant_id=TENANT_ID, key_id="key-under-test", policy=policy, plan=plan
        )

    def post(self, **extra: Any) -> dict:
        assert self.client is not None
        body: dict[str, Any] = {"kb_ids": [KB], "query": QUERY, **extra}
        response = self.client.post(RESEARCH_PATH, json=body)
        assert response.status_code == 200, "业务失败也是 HTTP 200（ADR-0007）"
        return response.json()


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entry]:
    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    gate = importlib.import_module("app.services.research_gate")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    entry = _Entry()

    monkeypatch.setattr(gate, "get_embedding_provider", lambda: entry.embedding)
    monkeypatch.setattr(gate, "get_vector_store", lambda: entry.vector)
    monkeypatch.setattr(gate, "get_keyword_search_provider", _FakeKeyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    async def _fake_llm(role: str = "fast") -> LLMProvider:
        return entry.llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _fake_llm)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        EmptySession()
    )
    main.app.dependency_overrides[get_current_key] = lambda: entry.holder["context"]
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            entry.client = client
            yield entry
    finally:
        main.app.dependency_overrides.clear()


# ───────────────────────────────────────────────────────────── 响应形状


def test_the_response_has_the_plan_rounds_materials_and_metadata(entry: _Entry) -> None:
    """一次全覆盖的 Research：计划是「要点 + 绑定要点的查询」，材料是交付单元，轮次只给计数。"""

    body = entry.post()
    assert body["code"] == 0

    data = body["data"]
    # 顶层键固定：没有内部候选池、没有第二份「原始回包」。
    assert set(data) == {
        "query",
        "kb_ids",
        "answer",
        "citations",
        "materials",
        "plan",
        "rounds",
        "metadata",
    }
    assert data["query"] == QUERY
    assert data["kb_ids"] == [KB]

    # 计划：每条查询绑定一个要点序号，要点编号 A1 起。
    assert [point["point_id"] for point in data["plan"]["points"]] == ["A1", "A2"]
    assert data["plan"]["queries"] == [
        {"point_id": "A1", "query": "退款多久到账", "round": 1},
        {"point_id": "A2", "query": "退款需要哪些材料", "round": 1},
    ]

    # 轮次只给计数（内容在 plan.queries 里，不重复一遍）。
    assert data["rounds"] == {"searches": 2, "followup": 0}

    # 元数据：计划名、耗时、模型调用次数、停止原因——四样都在。
    assert set(data["metadata"]) == {"plan_name", "duration_ms", "model_calls", "stop_reason"}
    assert data["metadata"]["plan_name"] == "research_fixed"
    assert data["metadata"]["model_calls"] == 2
    assert data["metadata"]["stop_reason"] == "evidence_complete"
    assert isinstance(data["metadata"]["duration_ms"], int)

    # 最终回答位：本票恒空（#149 落地）。
    assert data["answer"] is None
    assert data["citations"] == []


def test_the_materials_keep_the_retrieval_shape_and_are_verbatim(entry: _Entry) -> None:
    """材料与 ``/rag/retrieve`` 同一个值对象：正文逐字来自库，内部标识只用于回溯。"""

    data = entry.post()["data"]
    materials = data["materials"]

    assert [item["chunk_id"] for item in materials] == ["chunk-a", "chunk-b"]
    assert materials[0]["content"] == "超期退款需人工审核。"
    assert materials[1]["content"] == "3 个工作日内到账。"
    # 交付单元的字段集：节身份、章节路径、出处都在（与检索响应同一份模型）。
    assert {"section_id", "heading_path", "kb_id", "document_id", "score"} <= set(materials[0])


def test_the_request_body_takes_nothing_but_kb_ids_and_query(entry: _Entry) -> None:
    """多给一个字段（例如 ``rounds``）→ ``20002``，不是被静默忽略。"""

    body = entry.post(rounds=2)

    assert body["code"] == 20002
    assert entry.llm.calls == [], "参数校验失败先于任何一次模型调用"


# ───────────────────────────────────────────────────────────── 授权：拒绝而不是降级


def test_research_without_the_plan_capability_is_rejected(entry: _Entry) -> None:
    """套餐不给 → ``20013``；一次子检索、一次模型都没有发生（不静默降级成普通检索）。"""

    entry.use(policy=_policy(research_allowed=True), plan=_plan(research_allowed=False))

    body = entry.post()

    assert body["code"] == 20013
    assert body["data"] is None
    assert entry.embedding.queries == []
    assert entry.llm.calls == []


def test_research_without_the_key_capability_is_rejected(entry: _Entry) -> None:
    """密钥配置不给 → 同样 ``20013``（两层都要，ADR-0018）。"""

    entry.use(policy=_policy(research_allowed=False), plan=_plan(research_allowed=True))

    body = entry.post()

    assert body["code"] == 20013
    assert entry.embedding.queries == []


def test_a_kb_outside_the_whitelist_is_rejected_before_any_search(entry: _Entry) -> None:
    """白名单先于一切：越权的库 ``20008``，不烧一次嵌入。"""

    entry.use(
        policy=dataclasses.replace(
            _policy(), allowed_kb_ids=("kb-other",), unrestricted_scope=False
        ),
        plan=_plan(),
    )

    body = entry.post()

    assert body["code"] == 20008
    assert entry.embedding.queries == []


# ─────────────────────────────────────────── 子检索：不改进程、不写索引、不记账、不发观测


def test_sub_queries_do_not_rewrite_write_attribution_or_account(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每条子查询各跑一次内核，**不碰**「每次调用只做一次」的那五件事（ADR-0028 §2）。

    证明方式有两层：

    1. **行为**：嵌入 provider 收到的句子逐字等于计划里的查询（一次一条），模型只被问两次
       ——改写会改掉句子并多出一次模型调用；
    2. **结构**：把完整检索服务那一串入口全部换成「一碰就炸」的替身。Research 若偷懒复用
       ``rag_service.retrieve``，这条用例立刻红；反过来，它全绿就说明那些调用点一次都没
       经过。
    """

    import app.services.quota_gate as quota_gate
    import app.services.rag_service as rag_service
    import app.services.retrieval_gate as retrieval_gate
    import app.services.retrieval_trace as retrieval_trace
    from app.services.query import optimizer

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Research 的子检索不该经过这一步")

    monkeypatch.setattr(rag_service, "retrieve", _boom)
    monkeypatch.setattr(rag_service, "optimize_query", _boom)
    monkeypatch.setattr(rag_service, "record_retrieval_trace", _boom)
    monkeypatch.setattr(rag_service, "emit_retrieval_trace_off_loop", _boom)
    monkeypatch.setattr(retrieval_gate, "gated_retrieve", _boom)
    monkeypatch.setattr(retrieval_trace, "record_retrieval_trace", _boom)
    monkeypatch.setattr(quota_gate, "retrieval_quota", _boom)
    monkeypatch.setattr(optimizer, "optimize_query", _boom)

    data = entry.post()["data"]

    # 逐字、逐条、各一次：没有改写（也没有第二次嵌入），没有重复记账。
    assert entry.embedding.queries == ["退款多久到账", "退款需要哪些材料"]
    # 模型只有规划 + 判断两次——改写若在场，这里会多一次。
    assert entry.llm.calls == ["ResearchPlan", "FollowupPlan"]
    assert data["rounds"]["searches"] == 2


def test_the_gate_reuses_one_section_once_across_sub_queries(entry: _Entry) -> None:
    """两条子查询命中同一节 → 材料只交付一次（材料是并集，不是每轮回包）。"""

    same = _row("a", "超期退款需人工审核。")
    entry.vector.rows_for = {
        "退款多久到账": [same],
        "退款需要哪些材料": [dict(same)],
    }

    data = entry.post()["data"]

    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-a"]
    assert data["rounds"]["searches"] == 2, "两条查询都真的跑了，只是材料去重"


# ───────────────────────────────────────────────────────────── 降级出口：code 0


def test_a_failed_planner_returns_code_zero_with_one_plain_search(entry: _Entry) -> None:
    """规划失败 → 原问题跑一次普通检索，``code 0``，停止原因 ``degraded``。"""

    entry.llm = _ScriptedLLM(RuntimeError("模型服务 502"))

    data = entry.post()["data"]

    assert entry.embedding.queries == [QUERY], "回退的是原问题，且只有一次"
    assert data["metadata"]["stop_reason"] == "degraded"
    assert data["metadata"]["model_calls"] == 1
    assert data["plan"] == {"points": [], "queries": []}
    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-fallback"]


def test_a_failed_decider_keeps_the_materials_and_marks_degraded(entry: _Entry) -> None:
    """判断失败 → 停止补证、标记降级；首轮材料完整交回。"""

    entry.llm = _ScriptedLLM(_plan_output(), RuntimeError("模型超时"))

    data = entry.post()["data"]

    assert data["metadata"]["stop_reason"] == "degraded"
    assert data["rounds"] == {"searches": 2, "followup": 0}
    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-a", "chunk-b"]
