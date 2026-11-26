"""重排额度用尽时的降级：三条链路共用一处实现，而且真的接进了链路。

重排是**可选优化**，额度用尽时正确行为是「跳过它 + 如实记原因」，不是拒绝整单
（ADR-0032、用户故事 15）。本文件守两件事：

* :func:`app.services.rerank_gate.degrade_rerank_if_exhausted` 的三个分支各自成立，
  尤其是「没开重排时不去碰账本」——白读一次 Redis 是纯浪费；
* 它**真的**接进了 ``/retrieve`` 与 ``/decompose``：响应里的 ``degraded_reason`` 与
  ``tenant_policy.rerank`` 都必须跟着变。只测函数不测接线，等于允许调用点悄悄退回
  老写法（那正是这个函数存在的理由）。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import tests.test_multi_query_union as u
from app.core.config import settings
from app.services import rerank_gate
from app.services.retrieval.rerank import REASON_RERANK_QUOTA_EXHAUSTED
from app.services.retrieval_options import RetrievalOptions
from tests.support.policy import policy
from tests.test_four_retrieval_endpoints import _PlanLLM, _plan


def _options(*, rerank: bool) -> RetrievalOptions:
    return RetrievalOptions(
        mode="hybrid",
        top_k=5,
        fold_to_sections=False,
        expand_table_rows=True,
        max_unit_chars=2000,
        rerank=rerank,
    )


class _Ledger:
    """账本替身：只回答「还有没有余量」，并数自己被问了几次。"""

    def __init__(self, *, capacity: bool) -> None:
        self._capacity = capacity
        self.probes = 0

    async def has_capacity(self) -> bool:
        self.probes += 1
        return self._capacity


def _key() -> Any:
    return u.IDENTITY.context()


class _UnavailableRerank:
    """「装了但没开」的重排替身：不发网络，``applied`` 恒为 False。"""

    available = False
    provider_name = "unavailable"
    model_name = None

    async def rerank(self, **_kwargs: Any) -> Any:  # pragma: no cover - 不会被调用
        raise AssertionError("没装重排时不该真的去排")


# ─────────────────────────────────────────── 函数本身：三个分支


@pytest.mark.anyio
async def test_rerank_off_never_touches_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """没开重排就不该连计数器：这次调用本来就不会排，读一次 Redis 是纯浪费。"""

    def _boom(**_kwargs: Any) -> Any:
        raise AssertionError("没开重排时不该构造账本")

    monkeypatch.setattr(rerank_gate, "rerank_quota", _boom)

    options = _options(rerank=False)

    assert await rerank_gate.degrade_rerank_if_exhausted(options, _key()) is options


@pytest.mark.anyio
async def test_capacity_left_keeps_the_options_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """还有余量：原样返回（连对象都不换），调用方拿到的就是它传进去的那一份。"""

    ledger = _Ledger(capacity=True)
    monkeypatch.setattr(rerank_gate, "rerank_quota", lambda **_kwargs: ledger)

    options = _options(rerank=True)
    result = await rerank_gate.degrade_rerank_if_exhausted(options, _key())

    assert result is options
    assert result.rerank_skipped_reason is None
    assert ledger.probes == 1


@pytest.mark.anyio
async def test_exhausted_degrades_and_records_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额度用尽：关掉重排 + 写上原因。原因这一栏少了，就是看不见的降级。"""

    monkeypatch.setattr(rerank_gate, "rerank_quota", lambda **_kwargs: _Ledger(capacity=False))

    result = await rerank_gate.degrade_rerank_if_exhausted(_options(rerank=True), _key())

    assert result.rerank is False
    assert result.rerank_skipped_reason == REASON_RERANK_QUOTA_EXHAUSTED


# ─────────────────────────────────────────── 接线：端点真的降级


def _rerank_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """让这台机器「装着重排」：能力位 = 开关 ∧ 端点配齐（#116）。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")


def _wants_rerank() -> None:
    """一套「要重排」的配置：参数说 true，位也开着，否则配置自己就不成立。"""

    u.IDENTITY.use_policy(
        policy(
            allowed_kb_ids=(u.KB_A,),
            rerank_allowed=True,
            custom_options=(("mode", "hybrid"), ("top_k", 5), ("rerank", True)),
        )
    )


def _degraded(body: dict[str, Any]) -> dict[str, Any]:
    return body["data"]["metadata"]["retrieval"]


@pytest.mark.parametrize("path", [u.RETRIEVE_PATH, "/api/v1/rag/decompose"])
def test_an_exhausted_rerank_book_degrades_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """额度用尽时两条链路都**照常返回**，只是把重排跳过并写明原因。"""

    _rerank_on(monkeypatch)
    _wants_rerank()
    monkeypatch.setattr(rerank_gate, "rerank_quota", lambda **_kwargs: _Ledger(capacity=False))

    # 分解端点的子检索内核从 research_gate 自己的名字空间取三个工厂，要换成同一份离线替身。
    vector = u._FakeVectorStore(rows=[u._vector_row("c1", score=0.9)])
    keyword = u._FakeKeyword(rows=[u._keyword_row("c1")])
    research_gate = importlib.import_module("app.services.research_gate")
    monkeypatch.setattr(research_gate, "get_vector_store", lambda: vector)
    monkeypatch.setattr(research_gate, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(research_gate, "get_embedding_provider", lambda: u._RecordingEmbedding())

    llm = _PlanLLM(_plan()) if "decompose" in path else None
    with u._offline_client(monkeypatch, llm=llm, vector=vector, keyword=keyword) as fake:
        response = fake["client"].post(path, json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0, body
    assert _degraded(body)["degraded_reason"] == REASON_RERANK_QUOTA_EXHAUSTED
    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is False


def test_a_healthy_rerank_book_does_not_report_the_quota_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额度够用时**不会**报「额度用尽」：原因不能恒填，否则它就不再是信息。

    这里换掉 Provider 工厂只为不发网络：本用例问的是「额度够时那条原因不出现」，
    不是「重排本身跑得怎么样」——后者是 ``test_rerank_injection`` 的射程。
    """

    _rerank_on(monkeypatch)
    _wants_rerank()
    monkeypatch.setattr(rerank_gate, "rerank_quota", lambda **_kwargs: _Ledger(capacity=True))

    rerank_module = importlib.import_module("app.services.retrieval.rerank")

    async def _unavailable() -> Any:
        return _UnavailableRerank()

    monkeypatch.setattr(rerank_module, "_default_rerank_provider", _unavailable)

    with u._offline_client(monkeypatch) as fake:
        response = fake["client"].post(
            u.RETRIEVE_PATH, json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY}
        )

    body = response.json()
    assert body["code"] == 0, body
    assert _degraded(body)["degraded_reason"] is None
    # 配置要重排、额度也够，所以「这次会排」这一栏必须是 True——没有被额度折掉。
    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is True
