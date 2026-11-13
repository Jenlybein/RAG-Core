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
from app.core.profile_presets import (
    DEFAULT_PRESETS,
    ProfilePresetSpec,
    reset_profile_presets,
    set_profile_presets,
)
from app.models.key_policy import PROFILE_CUSTOM, PROFILE_SPEED, KeyPolicySnapshot
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
    """两条候选：编排材料只能来自这里（也就是库里的原文）。"""

    async def similarity_search_scope(self, _db, _vector, *, tenant_id, index_versions, top_k):
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

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        if len(self.calls) == 1:
            return {"points": ["退款时限"]}
        if len(self.calls) == 2:
            return {"points": [{"point": 1, "primary": [1], "supporting": [2]}]}
        raise AssertionError("编排只该问两次模型")

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
    """一条 HTTP 入口 + 可改的身份 + 可观察的模型替身。"""

    def __init__(self, holder: dict[str, KeyContext]) -> None:
        self._holder = holder
        self.llm = _ScriptedLLM()
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

    monkeypatch.setattr(service, "get_embedding_provider", _FakeEmbedding)
    monkeypatch.setattr(service, "get_vector_store", _FakeVectorStore)
    monkeypatch.setattr(service, "get_keyword_search_provider", _FakeKeyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    holder: dict[str, KeyContext] = {
        "context": KeyContext(tenant_id=TENANT_ID, policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN)
    }
    entry = _Entry(holder)

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
