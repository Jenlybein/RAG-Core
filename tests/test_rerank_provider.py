"""大模型重排 Provider：排序、分数、候选封顶、截断与失败语义（全部离线）。

接缝有两个，都是既有的：``RerankProvider.rerank``（既有抽象）与
``get_rerank_provider``（既有工厂出口）。断言只看「进去的候选、回来的顺序与分数、
提示词里到底有什么」，不看内部实现；替身大模型实现 ``LLMProvider``，**绝不发网络**。

本文件守的契约（父 spec「降级矩阵与三种结局」「模型接入层」、GLOSSARY「重排」）：

* 重排只改 ``score``，融合分与五列分列字段原样保留（可解释性不能因为重排丢掉）；
* 模型没提到的候选按 0 分参与排序，未知 ``chunk_id`` 直接忽略；
* 送进模型的候选不超过 ``RERANK_MAX_CANDIDATES``，单条内容截到 ``RERANK_CHUNK_MAX_CHARS``；
* 返回条数不超过配置的 ``RERANK_TOP_N``；
* 空返回 / 结构不合法 / 调用失败 / 超时都是**异常**——降级判定在上层（编排），
  Provider 不自己吞掉，否则上层就不知道「这次没重排」；
* 工厂：``RERANK_ENABLED=false`` 给空实现（默认路径一次模型都不调），
  ``true`` 才装大模型实现，并且固定用 fast 档。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings, settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.providers.rerank import get_rerank_provider
from app.providers.rerank.llm import LLMRerankProvider, RerankRanking, RerankScore
from app.providers.rerank.noop import NoopRerankProvider


# ─────────────────────────────────────────────────────────────── 替身


class _FakeLLM(LLMProvider):
    """替身大模型：记下提示词，按预置结果/异常作答，绝不发网络。"""

    def __init__(
        self,
        *,
        ranking: Any = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._ranking = ranking
        self._error = error
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        self.calls.append({"schema": schema, "system": system, "user": user})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._ranking

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("重排只走结构化输出")


def _chunk(chunk_id: str, *, content: str | None = None, rrf: float = 0.03) -> dict:
    """一条融合后的候选：带着五列字段与融合分，重排前后都该在。"""

    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "kb_id": "kb-a",
        "title": f"title-{chunk_id}",
        "content": content if content is not None else f"content-{chunk_id}",
        "vector_score": 0.9,
        "bm25_score": None,
        "vector_rank": 1,
        "bm25_rank": None,
        "rrf_score": rrf,
        "score": rrf,
    }


def _provider(llm: LLMProvider, **overrides: Any) -> LLMRerankProvider:
    options: dict[str, Any] = {
        "llm": llm,
        "max_candidates": 20,
        "chunk_max_chars": 1024,
        "timeout_seconds": 5.0,
    }
    options.update(overrides)
    return LLMRerankProvider(**options)


def _ranking(*pairs: tuple[str, float]) -> RerankRanking:
    return RerankRanking(ranking=[RerankScore(chunk_id=cid, score=score) for cid, score in pairs])


# ─────────────────────────────────────────────────── 排序与分数的语义


def test_ranking_follows_model_scores_and_score_becomes_rerank_score() -> None:
    """顺序按重排分走；``score`` 换成分数本身，融合分与分列字段原样保留。"""

    llm = _FakeLLM(ranking=_ranking(("c3", 9.0), ("c2", 5.0), ("c1", 2.0)))
    chunks = [_chunk("c1", rrf=0.03), _chunk("c2", rrf=0.02), _chunk("c3", rrf=0.01)]

    result = asyncio.run(_provider(llm).rerank(query="退款怎么走", chunks=chunks, top_n=3))

    assert [c["chunk_id"] for c in result] == ["c3", "c2", "c1"]
    assert [c["score"] for c in result] == [9.0, 5.0, 2.0]
    # 重排只改「最终排序分」，不改「这条怎么来的」。
    assert [c["rrf_score"] for c in result] == [0.01, 0.02, 0.03]
    assert result[0]["vector_score"] == 0.9
    assert result[0]["content"] == "content-c3", "回给调用方的仍是切片原文"
    assert result[0]["kb_id"] == "kb-a"


def test_unknown_chunk_ids_are_ignored_and_missing_candidates_score_zero() -> None:
    """模型编出一个不存在的 id 就忽略；它漏掉的候选按 0 分、保持融合顺序。"""

    llm = _FakeLLM(ranking=_ranking(("ghost", 10.0), ("c2", 7.0)))
    chunks = [_chunk("c1"), _chunk("c2"), _chunk("c3")]

    result = asyncio.run(_provider(llm).rerank(query="q", chunks=chunks, top_n=3))

    assert [c["chunk_id"] for c in result] == ["c2", "c1", "c3"]
    assert [c["score"] for c in result] == [7.0, 0.0, 0.0]


def test_caller_top_n_is_the_only_return_cap() -> None:
    """返回条数只由调用方的 ``top_n`` 决定——服务端配置不再把它悄悄压小。"""

    llm = _FakeLLM(ranking=_ranking(("c1", 1.0)))
    chunks = [_chunk(f"c{i}") for i in range(6)]

    result = asyncio.run(_provider(llm).rerank(query="q", chunks=chunks, top_n=6))

    assert len(result) == 6, "要 6 条就给 6 条（候选足够时）"
    assert result[0]["chunk_id"] == "c1", "打分最高的排第一"


# ──────────────────────────────────────────── 候选封顶与单条内容截断


def test_candidate_cap_and_content_truncation_reach_the_model() -> None:
    """送进模型的候选不超过上限，单条内容超过上限就截断——提示词是真被约束住的。"""

    llm = _FakeLLM(ranking=_ranking(("c0", 1.0)))
    chunks = [_chunk(f"c{i}", content="X" * 50) for i in range(25)]

    asyncio.run(
        _provider(llm, max_candidates=20, chunk_max_chars=10).rerank(
            query="q", chunks=chunks, top_n=5
        )
    )

    prompt = llm.calls[0]["user"]
    assert "c19" in prompt
    assert "c20" not in prompt, "第 21 条起不该送进模型"
    assert "X" * 10 in prompt
    assert "X" * 11 not in prompt, "单条内容必须截到 RERANK_CHUNK_MAX_CHARS"


def test_no_candidates_means_no_model_call() -> None:
    """空候选不浪费一次模型调用；返回空列表、不报错（空结果不是重排失败）。"""

    llm = _FakeLLM(ranking=_ranking())

    result = asyncio.run(_provider(llm).rerank(query="q", chunks=[], top_n=5))

    assert result == []
    assert llm.calls == []


# ────────────────────────────────────────────── 失败的四种长相都是异常


def test_empty_ranking_is_a_failure() -> None:
    """模型一条都没排（空返回）：当成失败交给上层降级，绝不返回空列表当结果。"""

    llm = _FakeLLM(ranking=_ranking())

    with pytest.raises(ValueError):
        asyncio.run(_provider(llm).rerank(query="q", chunks=[_chunk("c1")], top_n=5))


@pytest.mark.parametrize(
    "payload",
    [
        {"ranking": "不是列表"},
        {"oops": 1},
        ["c1", "c2"],
        None,
    ],
)
def test_malformed_model_output_is_rejected(payload: Any) -> None:
    """结构不合法（含 None）一律报错：不退回「按原顺序返回」这种静默降级。"""

    llm = _FakeLLM(ranking=payload)

    with pytest.raises(ValueError):
        asyncio.run(_provider(llm).rerank(query="q", chunks=[_chunk("c1")], top_n=5))


def test_llm_failure_propagates_to_the_caller() -> None:
    """模型调用失败原样上抛：降级由编排层负责，Provider 不该假装自己排过序。"""

    llm = _FakeLLM(error=AppError(ErrorCode.LLM_FAILED, "模型没有返回结构化结果"))

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider(llm).rerank(query="q", chunks=[_chunk("c1")], top_n=5))

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_slow_model_times_out() -> None:
    """超时是失败的一种：``RERANK_TIMEOUT_SECONDS`` 真的兜住了慢调用。"""

    llm = _FakeLLM(ranking=_ranking(("c1", 1.0)), delay=0.5)

    with pytest.raises(TimeoutError):
        asyncio.run(
            _provider(llm, timeout_seconds=0.01).rerank(query="q", chunks=[_chunk("c1")], top_n=5)
        )


# ────────────────────────────────────────────────────────── 工厂与开关


def test_rerank_settings_all_have_defaults() -> None:
    """离线构造配置对象不能因为缺默认值而炸（父 spec「配置与依赖」）。"""

    config = Settings(_env_file=None)

    assert config.RERANK_ENABLED is False, "默认关闭：默认路径不碰大模型"
    assert config.RERANK_MAX_CANDIDATES == 20
    assert config.RERANK_CHUNK_MAX_CHARS == 1024
    assert config.RERANK_TIMEOUT_SECONDS == 15.0


def test_factory_returns_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭时返回既有空实现：顺序不动、不发网络。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    assert isinstance(get_rerank_provider(), NoopRerankProvider)


def test_disabled_factory_never_touches_the_llm_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭时连大模型工厂都不该被调用——离线机器上这是「绝不发网络」的前提。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    def _forbidden(_tier: str = "fast") -> LLMProvider:  # pragma: no cover
        raise AssertionError("关闭重排时不许装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    assert isinstance(get_rerank_provider(), NoopRerankProvider)


def test_available_flag_distinguishes_off_from_reranked() -> None:
    """``available`` 是编排层区分「这一步没开」与「排了但顺序没变」的唯一信号。

    与 ``KeywordSearchProvider.available`` 同名同义：都是「这项能力在这台机器上
    存不存在」，不是「这次跑出了什么结果」。
    """

    assert NoopRerankProvider().available is False
    assert _provider(_FakeLLM(ranking=_ranking())).available is True


def test_enabled_without_any_model_key_degrades_instead_of_breaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开了重排但这台机器没配任何大模型 key：拿到空实现、重排失败，服务照常启动。

    父 spec 用户故事 22：没有 key 的机器（离线测试 / CI）必须能启动，能力明确
    不可用。这里是「明确不可用」的具体长相：``LLM_FAILED`` 原样上抛，由编排层
    降级成融合顺序——不是启动失败，也不是静默当作没配。
    """

    for name in (
        "DEEPSEEK_API_KEY",
        "QWEN_API_KEY",
        "ARK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    provider = get_rerank_provider()
    assert isinstance(provider, LLMRerankProvider)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(provider.rerank(query="q", chunks=[_chunk("c1")], top_n=5))

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_factory_wires_the_fast_tier_and_the_configured_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启后：用 fast 档、按配置封顶候选、按配置截断、按配置限制返回条数。

    这条测试同时证明「配置从 Settings 走到 Provider」是真的——不是写了一堆没人读的键。
    """

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", 2)
    monkeypatch.setattr(settings, "RERANK_CHUNK_MAX_CHARS", 4)
    monkeypatch.setattr(settings, "RERANK_TIMEOUT_SECONDS", 7.0)

    llm = _FakeLLM(ranking=_ranking(("c0", 1.0)))
    tiers: list[str] = []

    def _fake_get_llm_provider(tier: str = "fast") -> LLMProvider:
        tiers.append(tier)
        return llm

    # 惰性 import 的实现让这个替换点成立：工厂在调用时才去找这个函数。
    monkeypatch.setattr("app.providers.llm.get_llm_provider", _fake_get_llm_provider)

    provider = get_rerank_provider()
    assert isinstance(provider, LLMRerankProvider)
    assert tiers == ["fast"], "重排与查询改写共用快档"

    chunks = [_chunk(f"c{i}", content="X" * 50) for i in range(5)]
    result = asyncio.run(provider.rerank(query="q", chunks=chunks, top_n=5))

    prompt = llm.calls[0]["user"]
    assert "c1" in prompt and "c2" not in prompt, "候选封顶来自 RERANK_MAX_CANDIDATES"
    assert "X" * 4 in prompt and "X" * 5 not in prompt, "截断来自 RERANK_CHUNK_MAX_CHARS"
    assert len(result) == 2, "候选只有两条，返回就不可能更多"
