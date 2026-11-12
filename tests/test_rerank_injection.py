"""重排的注入缝与回显：编排入口收一个「重排工厂」可调用对象。

接缝有三层，但**只有一个东西在流动**：``rerank_factory``。它从 ``rag_service.retrieve``
（组合根）一路传到 ``rerank_stage``，缺省仍是 ``app.services.retrieval.rerank.get_rerank_provider``
（既有 monkeypatch 接缝，一行不变）。评测因此可以显式装配旧的大模型重排做对照，而生产
路径一行不用改。

本文件守的契约（票 #116）：

* 注入的工厂**真的**决定了这次用哪个实现（不是「收下参数然后丢掉」）；
* 缺省（不注入）仍走既有工厂出口——既有 monkeypatch 接缝保持不变；
* ``metadata.retrieval.rerank`` 多出 ``provider`` / ``model`` 两个键，且**纯增量**：
  ``applied`` / ``candidates`` / ``returned`` / ``duration_ms`` 一个不少；
* 没跑重排（关闭 / 没装 / 没候选）时这两个键是 ``None``——「没有实现可署名」，而不是
  编一个 ``"noop"`` 让读者以为排过一遍。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.retrieval.rerank import REASON_RERANK_FAILED, rerank_stage
from app.services.retrieval_options import RetrievalOptions


def _chunk(chunk_id: str) -> dict[str, Any]:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "kb_id": "kb-a",
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "vector_score": 0.9,
        "bm25_score": None,
        "vector_rank": 1,
        "bm25_rank": None,
        "rrf_score": 0.03,
        "score": 0.03,
    }


class _StubRerank:
    """替身重排：把候选倒过来，并如实署上自己的身份。"""

    def __init__(self, *, available: bool = True, error: Exception | None = None) -> None:
        self.available = available
        self._error = error
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def model_name(self) -> str | None:
        return "stub-model"

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(reversed(chunks))[:top_n]


# ───────────────────────────────────────────── 重排阶段：工厂决定实现


def test_rerank_stage_uses_the_injected_factory_and_echoes_its_identity() -> None:
    """注入的工厂被真的调用；回显里署的是**它**的名字与模型。"""

    provider = _StubRerank()

    outcome = asyncio.run(
        rerank_stage(
            query="q",
            chunks=[_chunk("c1"), _chunk("c2")],
            top_n=2,
            enabled=True,
            provider_factory=lambda: provider,
        )
    )

    assert provider.calls == 1
    assert [row["chunk_id"] for row in outcome.chunks] == ["c2", "c1"]
    metadata = outcome.as_metadata()
    assert metadata["applied"] is True
    assert metadata["provider"] == "stub"
    assert metadata["model"] == "stub-model"


def test_a_failed_rerank_still_names_the_provider_it_tried() -> None:
    """失败也署名：降级之后最该能回答的就是「刚才试的是哪种重排」。"""

    outcome = asyncio.run(
        rerank_stage(
            query="q",
            chunks=[_chunk("c1"), _chunk("c2")],
            top_n=1,
            enabled=True,
            provider_factory=lambda: _StubRerank(error=RuntimeError("502")),
        )
    )

    assert outcome.reason == REASON_RERANK_FAILED
    assert [row["chunk_id"] for row in outcome.chunks] == ["c1"], "失败保留融合顺序且恰好 top_n 条"
    assert outcome.as_metadata()["provider"] == "stub"
    assert outcome.as_metadata()["model"] == "stub-model"


def test_the_two_identity_keys_are_additive_on_every_path() -> None:
    """``provider`` / ``model`` 是纯增量键；没跑重排时是 ``None`` 而不是缺席。"""

    disabled = asyncio.run(
        rerank_stage(
            query="q",
            chunks=[_chunk("c1")],
            top_n=1,
            enabled=False,
            provider_factory=lambda: _StubRerank(),
        )
    )
    empty = asyncio.run(
        rerank_stage(
            query="q",
            chunks=[],
            top_n=1,
            enabled=True,
            provider_factory=lambda: _StubRerank(),
        )
    )
    unavailable = asyncio.run(
        rerank_stage(
            query="q",
            chunks=[_chunk("c1")],
            top_n=1,
            enabled=True,
            provider_factory=lambda: _StubRerank(available=False),
        )
    )

    for outcome in (disabled, empty, unavailable):
        metadata = outcome.as_metadata()
        assert set(metadata) == {
            "applied",
            "candidates",
            "returned",
            "duration_ms",
            "provider",
            "model",
        }
        assert metadata["provider"] is None
        assert metadata["model"] is None


def test_without_a_factory_the_existing_seam_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入时仍走既有工厂出口——既有 monkeypatch 接缝必须一条都不用改。"""

    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    provider = _StubRerank()
    monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: provider)

    outcome = asyncio.run(
        rerank_stage(query="q", chunks=[_chunk("c1"), _chunk("c2")], top_n=2, enabled=True)
    )

    assert provider.calls == 1
    assert outcome.as_metadata()["provider"] == "stub"


# ───────────────────────────────────────────── 编排层：把工厂往下传


def test_the_pipeline_forwards_the_rerank_factory_to_the_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hybrid_retrieve`` 不自己造 Provider：它把工厂原样交给重排阶段。"""

    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    captured: dict[str, Any] = {}

    async def _fake_stage(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            chunks=kwargs["chunks"][: kwargs["top_n"]],
            reason=None,
            duration_ms=None,
            as_metadata=lambda: {"applied": False},
        )

    async def _no_keyword(**kwargs: Any) -> list[dict]:
        return []

    async def _no_vector_branch(**kwargs: Any) -> list[dict]:
        return []

    class _InlinePool:
        """把池换成就地执行：这条用例只关心重排注入，不该真的起线程。"""

        async def run(self, factory: Any) -> Any:
            return await factory()

    monkeypatch.setattr(pipeline, "rerank_stage", _fake_stage)
    monkeypatch.setattr(pipeline, "_vector_branch_in_pool", _no_vector_branch)
    monkeypatch.setattr(pipeline, "get_retrieval_pool", lambda: _InlinePool())
    monkeypatch.setattr(pipeline, "_keyword_branch", _no_keyword)

    factory = lambda: _StubRerank()  # noqa: E731 - 工厂就是一个可调用对象

    asyncio.run(
        pipeline.hybrid_retrieve(
            tenant_id="t",
            kb_ids=["kb-a"],
            vector_queries=["q"],
            rerank_query="q",
            keyword_query="q",
            top_k=3,
            embedding_factory=lambda: None,
            vector_factory=lambda: None,
            keyword_factory=lambda: None,
            rerank_enabled=True,
            rerank_factory=factory,
        )
    )

    assert captured["provider_factory"] is factory


def test_the_retrieval_entry_forwards_the_rerank_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """组合根收下 ``rerank_factory`` 后必须真的交给编排层（不是收下就丢）。"""

    service = importlib.import_module("app.services.rag_service")
    captured: dict[str, Any] = {}

    async def _fake_hybrid(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(chunks=[], degraded=False, degraded_reason=None, rerank=None)

    async def _identity_query(**kwargs: Any) -> Any:
        return SimpleNamespace(
            raw_query=kwargs["query"],
            effective_query=kwargs["query"],
            search_query=kwargs["query"],
            vector_queries=(kwargs["query"],),
            rewrite_applied=False,
            rewrite_degraded=False,
            rewrite_reason=None,
            duration_ms=0.0,
        )

    monkeypatch.setattr(service, "hybrid_retrieve", _fake_hybrid)
    monkeypatch.setattr(service, "optimize_query", _identity_query)
    monkeypatch.setattr(
        service,
        "ensure_visible",
        lambda db, *, tenant_id, kb_id: SimpleNamespace(settings=None, name="kb", description=None),
    )
    monkeypatch.setattr(service, "_collapse_results", lambda *args, **kwargs: [])
    monkeypatch.setattr(service, "_empty_reason", lambda *args, **kwargs: "no_chunks_matched")
    monkeypatch.setattr(service, "record_retrieval_trace", lambda **kwargs: None)
    monkeypatch.setattr(service, "emit_retrieval_trace", lambda **kwargs: None)

    factory = lambda: _StubRerank()  # noqa: E731 - 同上
    asyncio.run(
        service.retrieve(
            None,  # 这条路径不需要真会话：可见性与折叠都被替身接住了
            tenant_id="tenant_demo",
            kb_ids=["kb-a"],
            query="退款怎么走",
            options=RetrievalOptions(
                mode="hybrid",
                top_k=5,
                fold_to_sections=True,
                expand_table_rows=True,
                rerank=True,
                rewrite=False,
            ),
            rerank_factory=factory,
        )
    )

    assert captured["rerank_factory"] is factory
