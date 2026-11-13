"""票 #147：编排结果接进检索响应——**只在开启时出现的一个顶层可选字段**。

两条边界在这里被钉住：

* **关闭时字段不出现**，一个字节都不多（逐字节基线另由
  ``tests/test_multi_query_union.py`` 的黄金哈希守着）；关闭时**一次模型都不调**——附加
  能力的代价只能付在真正打开它的那几把 Key 上。
* **开启时它挂在 ``data`` 顶层**，不在 ``metadata.retrieval`` 里：它是「这一次编排出了
  什么」，不是「这次检索遇到了什么」。

材料正文的最终来源也在端到端这条线上验一遍：材料里的 ``content`` 逐字等于同一份响应里
``retrieved_chunks`` 的正文——模型在中间没有任何一次机会改写它。
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY, DEV_KEY_PLAN, KeyContext
from app.core.config import settings
from app.core.profile_presets import (
    DEFAULT_PRESETS,
    ProfilePresetSpec,
    reset_profile_presets,
    set_profile_presets,
)
from app.models.key_policy import (
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
    KeyPolicySnapshot,
)
from app.models.plan import PLAN_PRESETS, PLAN_PRO, PLAN_STANDARD, PlanSpec
from app.providers.llm.base import LLMProvider
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb

TENANT_ID = "tenant_demo"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
KB = "kb-a"


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """候选来源：编排材料只能来自这里（也就是库里的原文）。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows

    async def similarity_search_scope(self, _db, _vector, *, tenant_id, index_versions, top_k):
        if self.rows is not None:
            return [{**row, "kb_id": row.get("kb_id", KB)} for row in self.rows]
        return [
            {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "kb_id": KB,
                "title": "退款政策",
                "content": "超期退款需人工审核。",
                "score": 0.9,
            },
            {
                "document_id": "doc-b",
                "chunk_id": "chunk-b",
                "kb_id": KB,
                "title": "退款政策",
                "content": "3 个工作日内到账。",
                "score": 0.8,
            },
        ]


class _FakeKeyword:
    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return []


class _ScriptedLLM(LLMProvider):
    """两次结构化调用：要点 → 角色。调了第三次就直接炸（本票只该问两次）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if len(self.calls) == 1:
            return {"points": ["退款时限"]}
        if len(self.calls) == 2:
            return {"points": [{"point": 1, "primary": [1], "supporting": [2]}]}
        raise AssertionError("编排只该问两次模型")

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("编排只走结构化输出")


class _BrokenLLM(LLMProvider):
    """每次调用都失败：用来验降级出口（响应照常返回 + 一条专码）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls += 1
        raise RuntimeError("模型服务 502")

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("编排只走结构化输出")


def _policy(*, orchestration_allowed: bool) -> KeyPolicySnapshot:
    return dataclasses.replace(
        DEV_KEY_POLICY,
        profile=PROFILE_SPEED,
        orchestration_allowed=orchestration_allowed,
    )


def _plan(code: str = PLAN_PRO, **overrides: Any) -> PlanSpec:
    return dataclasses.replace(PLAN_PRESETS[code], **overrides)


def _want_orchestration() -> None:
    """把 speed 这一档的「想要」打开（运营在面板上做的事）。"""

    spec = DEFAULT_PRESETS[PROFILE_SPEED]
    set_profile_presets(
        {
            PROFILE_SPEED: ProfilePresetSpec(
                mode=spec.mode,
                top_k=spec.top_k,
                rerank=spec.rerank,
                rewrite=spec.rewrite,
                orchestration=True,
            )
        }
    )


@pytest.fixture(autouse=True)
def _fresh_presets() -> Iterator[None]:
    reset_profile_presets()
    try:
        yield
    finally:
        reset_profile_presets()


class _Entry:
    """一条 HTTP 入口 + 可改的身份 + 可观察的模型 / 候选替身。"""

    def __init__(self, holder: dict[str, KeyContext]) -> None:
        self._holder = holder
        self.llm: LLMProvider = _ScriptedLLM()
        self.vector_rows: list[dict] | None = None
        self.client: TestClient

    def use(self, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
        self._holder["context"] = KeyContext(
            tenant_id=TENANT_ID, key_id="key-under-test", policy=policy, plan=plan
        )

    def post(self) -> dict:
        response = self.client.post(RETRIEVE_PATH, json={"kb_ids": [KB], "query": "退款多久到账"})
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entry]:
    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    holder: dict[str, KeyContext] = {
        "context": KeyContext(tenant_id=TENANT_ID, policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN)
    }
    entry = _Entry(holder)

    monkeypatch.setattr(service, "get_embedding_provider", _FakeEmbedding)
    # 工厂读的是**同一个** entry：用例换候选 / 换模型都只改它，不重新装配整套夹具。
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore(entry.vector_rows))
    monkeypatch.setattr(service, "get_keyword_search_provider", _FakeKeyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    async def _fake_llm(role: str = "fast") -> LLMProvider:
        return entry.llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _fake_llm)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        EmptySession()
    )
    main.app.dependency_overrides[get_current_key] = lambda: holder["context"]
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            entry.client = client
            yield entry
    finally:
        main.app.dependency_overrides.clear()


# ─────────────────────────────────────────── 开：顶层多一块


def test_the_block_appears_at_the_top_level_only_when_the_switch_is_on(
    entry: _Entry,
) -> None:
    """关闭：`data` 里没有这个键；开启：顶层出现 ``completeness`` / ``points`` / ``evidence``。"""

    _want_orchestration()

    entry.use(policy=_policy(orchestration_allowed=False), plan=_plan())
    off = entry.post()["data"]
    assert "orchestration" not in off

    entry.use(policy=_policy(orchestration_allowed=True), plan=_plan())
    on = entry.post()["data"]
    assert set(on["orchestration"]) == {"completeness", "points", "evidence"}
    assert on["orchestration"]["completeness"] == "complete"
    assert "orchestration" not in on["metadata"]["retrieval"], "它不是检索的元数据"


def test_the_material_text_is_the_delivered_chunk_content_verbatim(entry: _Entry) -> None:
    """材料正文逐字等于同一份响应里那条交付单元的正文（模型没有改写的机会）。"""

    _want_orchestration()
    entry.use(policy=_policy(orchestration_allowed=True), plan=_plan())

    data = entry.post()["data"]
    delivered = {chunk["chunk_id"]: chunk for chunk in data["retrieved_chunks"]}
    evidence = data["orchestration"]["evidence"]

    assert [item["evidence_id"] for item in evidence] == ["E1", "E2"]
    assert [item["role"] for item in evidence] == ["primary", "supporting"]
    for item in evidence:
        assert item["content"] == delivered[item["chunk_id"]]["content"]
        assert item["kb_id"] == delivered[item["chunk_id"]]["kb_id"]
    assert evidence[0]["content"] == "超期退款需人工审核。"
    assert evidence[1]["content"] == "3 个工作日内到账。"


def test_the_switch_does_not_change_the_retrieval_material_itself(entry: _Entry) -> None:
    """编排只组织材料，不改材料：两次响应的 ``retrieved_chunks`` 一个字都不差。"""

    _want_orchestration()

    entry.use(policy=_policy(orchestration_allowed=False), plan=_plan())
    off = entry.post()["data"]
    entry.use(policy=_policy(orchestration_allowed=True), plan=_plan())
    on = entry.post()["data"]

    assert on["retrieved_chunks"] == off["retrieved_chunks"]
    assert on["metadata"]["mode"] == off["metadata"]["mode"]
    assert on["metadata"]["retrieval"]["degraded"] is False


def test_orchestration_costs_two_model_calls_and_none_when_off(entry: _Entry) -> None:
    """开启 = 两次调用（要点 → 角色）；关闭 = 零次（附加能力的代价只付给开了它的档位）。"""

    _want_orchestration()

    entry.use(policy=_policy(orchestration_allowed=False), plan=_plan())
    entry.post()
    assert entry.llm.calls == [], "关闭时一次模型都不该调"

    entry.use(policy=_policy(orchestration_allowed=True), plan=_plan())
    entry.post()
    assert entry.llm.calls == ["PointList", "PointRoles"]


def test_a_plan_that_does_not_allow_orchestration_leaves_the_field_out(entry: _Entry) -> None:
    """套餐不给 → 门禁把 ``custom`` 档的「想要」裁掉（回显 ``clipped``），响应里也就不出现。

    命名档位越权是拒绝（``20013``，见 ``test_orchestration_switch``），``custom`` 档是
    裁剪。两条都是 #146 定下的反应；这里只验「裁掉之后响应里没有这一块、也一次不调模型」。
    """

    entry.use(
        policy=dataclasses.replace(
            DEV_KEY_POLICY,
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "vector"), ("orchestration", True)),
            orchestration_allowed=True,
        ),
        plan=_plan(PLAN_STANDARD, orchestration_allowed=False),
    )
    data = entry.post()["data"]

    assert "orchestration" not in data
    assert data["metadata"]["tenant_policy"]["clipped"] == ["orchestration"]
    assert entry.llm.calls == []


# ─────────────────────────────────────────── 降级与配额：端到端两个方向


def test_a_model_failure_keeps_the_result_and_marks_one_dedicated_reason(
    entry: _Entry,
) -> None:
    """模型失败：块不出现、``degraded_reason`` 记一条专码，检索结果**逐字照常**。

    这是「附加能力失败不等于本次调用失败」的可断言形态：同一个请求在模型好的时候与模型
    坏的时候，``retrieved_chunks`` 必须完全一样。
    """

    _want_orchestration()

    entry.use(policy=_policy(orchestration_allowed=True), plan=_plan())
    healthy = entry.post()["data"]

    entry.llm = _BrokenLLM()
    broken = entry.post()["data"]

    assert "orchestration" not in broken
    assert broken["metadata"]["retrieval"]["degraded"] is True
    assert broken["metadata"]["retrieval"]["degraded_reason"] == "orchestration failed"
    assert broken["retrieved_chunks"] == healthy["retrieved_chunks"]


def test_an_earlier_degraded_reason_wins_over_the_orchestration_one(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一因一码、先到的才是根因：改写先失败时，编排失败不覆盖它。

    编排排在折叠之后，是整条链上**最后**一步；它能做的只有「自己再添一条原因」，
    而那条原因只有在前面一件事都没坏时才有资格出现。
    """

    # 改写是「档位想要 ∧ 本机装了」的合取：这台机器上那条部署开关默认关着，得先打开，
    # 否则本次请求根本没有改写这一步可失败（那是另一个事实，不是这颗子弹）。
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    # quality 档：混合 + 重排 + 改写。改写排第一位，于是它先失败。
    set_profile_presets(
        {
            PROFILE_QUALITY: ProfilePresetSpec(
                mode=DEFAULT_PRESETS[PROFILE_QUALITY].mode,
                top_k=DEFAULT_PRESETS[PROFILE_QUALITY].top_k,
                rerank=DEFAULT_PRESETS[PROFILE_QUALITY].rerank,
                rewrite=DEFAULT_PRESETS[PROFILE_QUALITY].rewrite,
                orchestration=True,
            )
        }
    )
    entry.llm = _BrokenLLM()
    entry.use(
        policy=dataclasses.replace(
            DEV_KEY_POLICY,
            profile=PROFILE_QUALITY,
            orchestration_allowed=True,
            rerank_allowed=True,
            query_rewrite_allowed=True,
        ),
        plan=_plan(),
    )

    data = entry.post()["data"]

    assert data["metadata"]["retrieval"]["degraded"] is True
    assert data["metadata"]["retrieval"]["degraded_reason"] == "query rewrite failed"
    assert "orchestration" not in data


def test_the_pool_offered_to_the_model_is_capped_at_twenty(entry: _Entry) -> None:
    """25 条候选进检索，模型只看到 20 条：池子封顶是成本约束，也是「序号可数」的前提。"""

    _want_orchestration()
    entry.vector_rows = [
        {
            "document_id": f"doc-{index}",
            "chunk_id": f"chunk-{index}",
            "title": "退款政策",
            "content": f"第 {index} 条正文。",
            "score": 1.0 - index / 100,
        }
        for index in range(1, 26)
    ]
    # ``custom`` 档把返回条数写到 25：只有交付单元多于 20 条，「池子封顶」才看得见
    # （命名档位的 top_k 都是 5，凑不出这个场景）。
    entry.use(
        policy=dataclasses.replace(
            DEV_KEY_POLICY,
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "vector"), ("top_k", 25), ("orchestration", True)),
            orchestration_allowed=True,
        ),
        plan=_plan(),
    )

    data = entry.post()["data"]

    assert len(data["retrieved_chunks"]) == 25, "交付单元是 25 条（top_k 由这把 Key 定）"
    assert "共 20 条" in entry.llm.prompts[0]
    assert "25." not in entry.llm.prompts[0], "第 21 条及以后不进提示词"
    assert "20." in entry.llm.prompts[0]
