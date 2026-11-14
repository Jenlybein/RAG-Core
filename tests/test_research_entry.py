"""票 #148 / #149：``POST /api/v1/rag/research``——门禁、子检索的接线、响应形状与身份。

这一层验的是**入口**，不是状态机（状态机在 ``test_research_state_machine.py`` /
``test_research_finalizer.py`` 里逐条钉住）：

* **请求体只有 ``kb_ids`` + ``query``**：多给字段一律 ``20002``，不静默忽略；
* **没有 Research 授权 → ``20013``**，且**一次子检索、一次模型都不发生**（不静默降级成普通
  检索）——那是 ADR-0026 §1 的明确拒绝；
* **子检索不复用完整检索服务**：改写在 ``/rag/retrieve`` 那条路上，Research 里没有它；
  配额记账与观测提交都是「每次调用一次」的事，因此都不在子检索里（ADR-0028）；
  归属索引相反——它**只在外层写一行**（票 #149 的追加要求），子检索一条都不写；
* **响应形状**：计划（要点 + 绑定要点的查询）、轮次只给计数、材料是交付单元（与
  ``/rag/retrieve`` 同一个值对象）、``answer`` 是引用块的渲染串、元数据带身份与三条
  不丢信息的字段；内部候选池一份都不出去；
* **任何模型阶段失败都不整单报错**：``code 0`` + 材料 + 一个 ``degraded``。
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.models.key_policy import PROFILE_SPEED, KeyPolicySnapshot
from app.models.plan import PLAN_PRESETS, PLAN_PRO, PlanSpec
from app.models.retrieval_trace import RetrievalTrace
from app.providers.llm.base import LLMProvider
from app.services.orchestration import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
)
from tests.fake_db import AsyncRecordingSession, AsyncSessionAdapter, EmptySession, async_visible_kb

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
    """规划 + 判断 + 生成三次结构化调用；按调用顺序交出预置输出。

    生产里规划 / 判断走 ``fast``、生成走 ``strong``；本文件的夹具把两档都指向同一个替身
    （``get_llm_provider`` 只认角色，替身不看角色），所以响应栈按**实际发生的顺序**排：
    规划 → 判断 → 生成。生成失败之后的兜底会再要一次，那一条由用例自己决定给不给。
    """

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if not self._responses:
            raise AssertionError(f"预置输出用完了：还有一次 {schema.__name__} 没有应答")
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


def _answer_output(*blocks: dict) -> dict:
    """生成的预置输出：默认一块引用第一条材料（本文件只在个别用例里关心块的内容）。"""

    return {
        "blocks": list(blocks)
        or [
            {
                "text": "3 个工作日内到账。",
                "kind": "supported",
                "primary": [2],
                "supporting": [],
            }
        ]
    }


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
    """一条 HTTP 入口 + 可改的身份 + 可观察的模型 / 候选 / 归属索引替身。"""

    def __init__(self) -> None:
        self.llm: LLMProvider = _ScriptedLLM(_plan_output(), _covered(), _answer_output())
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
        #: 归属索引那一行的落点（旁路短事务自己开的会话）。
        self.trace_session = AsyncRecordingSession()
        self.holder: dict[str, KeyContext] = {
            "context": KeyContext(tenant_id=TENANT_ID, policy=_policy(), plan=_plan())
        }
        self.client: TestClient | None = None

    def use(self, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
        self.holder["context"] = KeyContext(
            tenant_id=TENANT_ID, key_id="key-under-test", policy=policy, plan=plan
        )

    def traces(self) -> list[RetrievalTrace]:
        """这次调用写下的归属索引行（正常路径恰好一行）。"""

        return [row for row in self.trace_session.added if isinstance(row, RetrievalTrace)]

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
    factory_mod = importlib.import_module("app.db.session_factory")
    gate = importlib.import_module("app.services.research_gate")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    entry = _Entry()

    monkeypatch.setattr(gate, "get_embedding_provider", lambda: entry.embedding)
    monkeypatch.setattr(gate, "get_vector_store", lambda: entry.vector)
    monkeypatch.setattr(gate, "get_keyword_search_provider", _FakeKeyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)
    # 归属索引走**独立短事务**（自己开会话）：把进程级会话工厂指到这个用例自己的替身上，
    # 于是「写了几行、写了什么」是可断言的，而不会真的去连一次 PostgreSQL。
    factory_mod.set_async_session_factory(lambda: entry.trace_session)

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
        factory_mod.set_async_session_factory(None)


# ───────────────────────────────────────────────────────────── 响应形状


def test_the_response_has_the_plan_rounds_materials_and_metadata(entry: _Entry) -> None:
    """一次全覆盖的 Research：计划、材料、轮次、引用块与元数据各就各位。"""

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

    # 元数据：身份 + 计划名 + 耗时 + 模型调用次数 + 停止原因 + 三条不丢信息的字段。
    assert set(data["metadata"]) == {
        "plan_name",
        "duration_ms",
        "model_calls",
        "stop_reason",
        "trace_id",
        "evidence_state",
        "sub_search_degraded_reasons",
        "strong_model_fallback",
    }
    assert data["metadata"]["plan_name"] == "research_fixed"
    # 规划 / 判断 / 生成各一次（票 #149 起第 ⑤ 步会真的花掉第三次调用）。
    assert data["metadata"]["model_calls"] == 3
    assert data["metadata"]["stop_reason"] == "evidence_complete"
    assert isinstance(data["metadata"]["duration_ms"], int)
    assert data["metadata"]["evidence_state"] == COMPLETENESS_COMPLETE
    assert data["metadata"]["sub_search_degraded_reasons"] == []
    assert data["metadata"]["strong_model_fallback"] is None
    # 身份是 32 位十六进制，与归属索引那一行同一个值（下一次调用就换一根线）。
    assert len(data["metadata"]["trace_id"]) == 32
    int(data["metadata"]["trace_id"], 16)

    # 最终回答：``answer`` 是引用块的渲染串，块带着解析过的证据下标。
    assert data["answer"] == "3 个工作日内到账。（E2）"
    assert data["citations"] == [
        {"text": "3 个工作日内到账。", "evidence_ids": ["E2"], "status": "supported"}
    ]


def test_the_evidence_state_vocabulary_is_the_orchestration_one() -> None:
    """元数据里的三态与切片编排的 ``COMPLETENESS_*`` 是同一套词（不是第三份同义词表）。"""

    from app.schemas.research import ResearchMetadata

    annotation = ResearchMetadata.model_fields["evidence_state"].annotation
    assert set(get_args(annotation)) == {
        COMPLETENESS_COMPLETE,
        COMPLETENESS_PARTIAL,
        COMPLETENESS_MISSING,
    }


def test_the_materials_keep_the_retrieval_shape_and_are_verbatim(entry: _Entry) -> None:
    """材料与 ``/rag/retrieve`` 同一个值对象：正文逐字来自库，内部标识只用于回溯。"""

    data = entry.post()["data"]
    materials = data["materials"]

    assert [item["chunk_id"] for item in materials] == ["chunk-a", "chunk-b"]
    assert materials[0]["content"] == "超期退款需人工审核。"
    assert materials[1]["content"] == "3 个工作日内到账。"
    # 交付单元的字段集：节身份、章节路径、出处都在（与检索响应同一份模型）。
    assert {"section_id", "heading_path", "kb_id", "document_id", "score"} <= set(materials[0])
    # ``E2`` = ``materials[1]``：编号按最终材料顺序分配，调用方靠这个下标回指。
    assert data["citations"][0]["evidence_ids"] == ["E2"]
    assert data["materials"][1]["content"] == "3 个工作日内到账。"


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


# ───────────────────────── 子检索：不改进程、不记账、不发观测、不各写一行归属索引


def test_sub_queries_do_not_rewrite_or_account(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每条子查询各跑一次内核，**不碰**「每次调用只做一次」的那几件事（ADR-0028 §2）。

    证明方式有两层：

    1. **行为**：嵌入 provider 收到的句子逐字等于计划里的查询（一次一条），规划 / 判断 /
       生成各问一次——改写会改掉句子并多出一次模型调用；
    2. **结构**：把完整检索服务那一串入口全部换成「一碰就炸」的替身。Research 若偷懒复用
       ``rag_service.retrieve``，这条用例立刻红；反过来，它全绿就说明那些调用点一次都没
       经过。
    """

    import app.services.quota_gate as quota_gate
    import app.services.rag_service as rag_service
    import app.services.retrieval_gate as retrieval_gate
    from app.services.query import optimizer

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Research 的子检索不该经过这一步")

    monkeypatch.setattr(rag_service, "retrieve", _boom)
    monkeypatch.setattr(rag_service, "optimize_query", _boom)
    monkeypatch.setattr(rag_service, "record_retrieval_trace", _boom)
    monkeypatch.setattr(rag_service, "emit_retrieval_trace_off_loop", _boom)
    monkeypatch.setattr(retrieval_gate, "gated_retrieve", _boom)
    monkeypatch.setattr(quota_gate, "retrieval_quota", _boom)
    monkeypatch.setattr(optimizer, "optimize_query", _boom)

    data = entry.post()["data"]

    # 逐字、逐条、各一次：没有改写（也没有第二次嵌入），没有重复记账。
    assert entry.embedding.queries == ["退款多久到账", "退款需要哪些材料"]
    # 三个角色各一次——改写若在场，这里会多一次。
    assert entry.llm.calls == ["ResearchPlan", "FollowupPlan", "AnswerOut"]
    assert data["rounds"]["searches"] == 2
    # 归属索引仍然只写一行（子检索一条都不写）。
    assert len(entry.traces()) == 1


def test_the_sub_search_never_writes_an_attribution_row_by_source() -> None:
    """结构层：**状态机与内核**里一次都不出现写库调用，写行只在外层组合根那一次。

    与上面那条行为用例互补——行为用例证明「今天只写了一行」，这条证明「想多写也写不到」：
    归属索引的名字只允许出现在 ``research_gate``（每次调用只做一次），而 ``research.py``
    （会被调 N 次的那一层）里连它的名字都不该有。
    """

    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    state_machine = (root / "app" / "services" / "research.py").read_text(encoding="utf-8")
    for forbidden in (
        "rag_service",
        "optimize_query",
        "record_retrieval_trace",
        "emit_retrieval_trace",
    ):
        assert forbidden not in state_machine, f"research.py 不该出现 {forbidden}"

    # 组合根里恰好一处：一次 Research 一行，调用点只有一个。
    gate = (root / "app" / "services" / "research_gate.py").read_text(encoding="utf-8")
    assert gate.count("record_retrieval_trace(") == 1


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


# ───────────────────────────────── 身份与归属索引：一次 Research = 一行


def test_one_research_writes_exactly_one_attribution_row(entry: _Entry) -> None:
    """三条子查询、两次模型阶段，归属索引仍然**只有一行**，且那一行就是响应里的那根线。"""

    data = entry.post()["data"]
    rows = entry.traces()

    assert len(rows) == 1, "一次 Research = 一个 log_id / trace_id = 一行"
    row = rows[0]
    assert row.trace_id == data["metadata"]["trace_id"]
    assert row.tenant_id == TENANT_ID
    assert row.kb_ids == [KB]
    assert row.index_versions == {KB: "v1"}
    # ``chunk_ids`` 存**最终证据的节**：代表片 + 溯源片，去重保序。
    assert row.chunk_ids == ["chunk-a", "chunk-b"]
    # 条数是这套集成自己的参数（``.env`` 的 TOP_K），与子检索逐次用的是同一个值。
    assert row.top_k == 5
    assert row.degraded_reason is None
    assert row.empty_reason is None
    assert row.latency_ms == data["metadata"]["duration_ms"]


def test_a_degraded_sub_search_is_reported_once_in_metadata_and_on_the_row(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两条子查询各降级一次 → 只交回**一个**原因（一因一码、去重），且它也进那唯一一行。

    「关键词一路挂了、只是碰巧向量一路也命中了」这种调用，材料看起来完全正常；把它记下来
    是这张票的两条追加要求之一（另一条是身份）。它不许变成每算子查询各一条记录。
    """

    from app.services.retrieval.kernel import KernelOutcome

    gate = importlib.import_module("app.services.research_gate")

    async def _degraded_kernel(*args: Any, **kwargs: Any) -> KernelOutcome:
        # 一条子查询也命中这一节：两条子查询返回同一节，材料只交付一次。
        return KernelOutcome(
            chunks=[_row("a", "超期退款需人工审核。")], degraded_reason="keyword search failed"
        )

    monkeypatch.setattr(gate, "retrieve_kernel", _degraded_kernel)

    data = entry.post()["data"]

    assert data["metadata"]["sub_search_degraded_reasons"] == ["keyword search failed"]
    assert data["rounds"]["searches"] == 2, "两条子查询都跑了，只是原因去重"
    rows = entry.traces()
    assert len(rows) == 1
    assert rows[0].degraded_reason == "keyword search failed"


# ───────────────────────────────────────── 四条校验在入口上的出口：answer = null


def test_an_illegal_citation_leaves_the_answer_empty_but_keeps_the_materials(
    entry: _Entry,
) -> None:
    """下标越界 → ``answer = null``、``citations = []``，而材料与计划完整可读。

    ADR-0026 §3：「不过就 ``answer = null``，材料照给」。这不是失败——调用方仍然拿得到
    检索结果、查询计划、轮次计数与停止原因。
    """

    entry.llm = _ScriptedLLM(
        _plan_output(),
        _covered(),
        _answer_output(
            {"text": "3 个工作日内到账。", "kind": "supported", "primary": [9], "supporting": []}
        ),
    )

    data = entry.post()["data"]

    assert data["answer"] is None
    assert data["citations"] == []
    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-a", "chunk-b"]
    assert data["plan"]["points"] and data["plan"]["queries"]
    assert data["metadata"]["stop_reason"] == "evidence_complete"
    assert data["metadata"]["evidence_state"] == COMPLETENESS_MISSING


def test_a_missing_material_point_shows_up_as_an_insufficient_block(entry: _Entry) -> None:
    """第二个要点没有材料 → 那一块是 ``insufficient``（承认不足），完整度是 ``partial``。"""

    entry.vector.rows_for = {
        "退款多久到账": [_row("a", "超期退款需人工审核。")],
        "退款需要哪些材料": [],
    }
    entry.llm = _ScriptedLLM(
        _plan_output(),
        _covered(),
        _answer_output(
            {"text": "超期退款需人工审核。", "kind": "supported", "primary": [1], "supporting": []},
            {
                "text": "未找到所需材料的说明。",
                "kind": "insufficient",
                "primary": [],
                "supporting": [],
            },
        ),
    )

    data = entry.post()["data"]

    assert [item["status"] for item in data["citations"]] == ["supported", "insufficient"]
    assert data["citations"][1]["evidence_ids"] == [], "材料不足的块不许挂证据"
    assert data["answer"] is not None and "材料不足" in data["answer"]
    assert data["metadata"]["evidence_state"] == COMPLETENESS_PARTIAL


# ───────────────────────────────────────────── 降级出口：code 0


def test_a_failed_planner_returns_code_zero_with_one_plain_search(entry: _Entry) -> None:
    """规划失败 → 原问题跑一次普通检索，``code 0``，停止原因 ``degraded``。

    降级之后第 ⑤ 步仍然工作：这份材料是真的，所以它仍然可以被引用块用起来
    （``insufficient`` 与「有没有材料」是两件事）。
    """

    entry.llm = _ScriptedLLM(
        RuntimeError("模型服务 502"),
        _answer_output(
            {"text": "3 个工作日内到账。", "kind": "supported", "primary": [1], "supporting": []}
        ),
    )

    data = entry.post()["data"]

    assert entry.embedding.queries == [QUERY], "回退的是原问题，且只有一次"
    assert data["metadata"]["stop_reason"] == "degraded"
    # 规划那一次失败也算成本；生成对这份材料问了一次。
    assert data["metadata"]["model_calls"] == 2
    assert data["plan"] == {"points": [], "queries": []}
    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-fallback"]
    # 降级拿到的材料是真的，所以它仍然可以被引用块用起来（下标相对这份材料池）。
    assert data["answer"] == "3 个工作日内到账。（E1）"


def test_a_failed_decider_keeps_the_materials_and_marks_degraded(entry: _Entry) -> None:
    """判断失败 → 停止补证、标记降级；首轮材料完整交回。"""

    entry.llm = _ScriptedLLM(_plan_output(), RuntimeError("模型超时"), _answer_output())

    data = entry.post()["data"]

    assert data["metadata"]["stop_reason"] == "degraded"
    assert data["rounds"] == {"searches": 2, "followup": 0}
    assert [item["chunk_id"] for item in data["materials"]] == ["chunk-a", "chunk-b"]
