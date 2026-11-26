"""查询加工的纯函数缝：``direct_query`` / ``optimize_for_rewrite``（第 15 步口径）。

这一组用例钉的是**返回哪几句**，不是「检索用得好不好」：

* ``recall_queries`` = 两路召回共用的一组（原句 → 改写句，两路都逐句搜）；
* ``effective_query`` = 改写句，**只供归因**（重排用的是原句，见
  ``tests/test_query_optimization_retrieval.py`` 里那条重排用例）；
* 同义词扩展已下线（第 15 步），所以这里不再有 ``search_query`` 这一栏。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.services.query.optimizer import (
    QueryProcessResult,
    direct_query,
    optimize_for_rewrite,
)
from app.services.query.rewrite import QueryRewrite


class _FakeLLM(LLMProvider):
    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        if self._error is not None:
            raise self._error
        return self._result

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写只走结构化输出")


def _install(monkeypatch: pytest.MonkeyPatch, llm: LLMProvider) -> None:
    async def _factory(_role: str = "default") -> LLMProvider:
        return llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _factory)


# ─────────────────────────────────────────────────────────── direct


def test_direct_query_is_an_identity_transform() -> None:
    """`/rag/retrieve`：用句就是原句，单元素——两路都只查一次。"""

    result = direct_query("退款怎么走")

    assert isinstance(result, QueryProcessResult)
    assert result.raw_query == "退款怎么走"
    assert result.effective_query == "退款怎么走"
    assert result.recall_queries == ("退款怎么走",)
    assert result.rewrite_applied is False
    assert result.duration_ms == 0.0


# ─────────────────────────────────────────────────────────── rewrite


def test_rewrite_returns_the_original_and_the_rewritten_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改写成功：召回用句是「原句 + 改写句」两句，顺序固定（原句在前）。"""

    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(optimize_for_rewrite(query="退钱怎么弄"))

    assert result.recall_queries == ("退钱怎么弄", "退款到账时限")
    assert result.effective_query == "退款到账时限"
    assert result.rewrite_applied is True


def test_there_is_no_third_sentence_any_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """同义词扩展下线后，结果里不该再有第三句「扩展出来的」句子。

    这是一条结构性的守卫：``QueryProcessResult`` 现在只有一组召回用句，
    谁要是把同义词加回来，就必须先改这里——而不是悄悄多出一栏没人认领的字符串。
    """

    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(optimize_for_rewrite(query="退钱怎么弄"))

    assert not hasattr(result, "search_query")
    assert len(result.recall_queries) == 2


def test_identical_rewrite_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型原样返回时去重成一句：两路都只查一次（不是白跑两遍）。"""

    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款怎么走")))

    result = asyncio.run(optimize_for_rewrite(query="退款怎么走"))

    assert result.recall_queries == ("退款怎么走",)


def test_a_failed_rewrite_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """改写失败 → 整单 50003，绝不回退原句继续检索。"""

    _install(monkeypatch, _FakeLLM(error=RuntimeError("模型服务 502")))

    with pytest.raises(AppError) as caught:
        asyncio.run(optimize_for_rewrite(query="退款怎么走"))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_optimize_for_rewrite_reports_a_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """耗时是查询加工那一段的真实读数（非 0 也由替身决定，这里只断言非负）。"""

    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(optimize_for_rewrite(query="退款怎么走"))

    assert result.duration_ms >= 0
