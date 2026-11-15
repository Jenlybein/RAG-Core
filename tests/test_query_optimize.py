"""查询加工的编排（第 13 步）：恒等变换与「改写 → 同义词扩展」（模块接缝）。

两层处理的**顺序**是核心：改写补语义、同义词补字面。顺序反了，同义词表就匹配不到
「改写之后才出现」的词，等于白配。

本文件守的契约：

* ``direct_query``（`/rag/retrieve`）：三句全等于原句，一次模型都不调；
* ``optimize_for_rewrite``（`/rag/rewrite`）：``effective_query`` 是改写结果，
  ``search_query`` 是**在改写结果上**做同义词扩展的句子；
* 同义词开关关闭时 ``search_query`` 跟着 ``effective_query`` 走；
* 改写模型调用失败 → 抛 ``50003``，不回退原句、不产出结果。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.services.query.optimizer import (
    QueryProcessResult,
    direct_query,
    optimize_for_rewrite,
)
from app.services.query.rewrite import QueryRewrite
from app.services.query.synonyms import collect_synonym_groups


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


def _groups(*terms: str):
    """一张「命中 terms、追加 expand」的库级同义词表。"""

    return collect_synonym_groups(
        [{"synonyms": [{"terms": list(terms), "expand": ["返还货款"]}]}]
    )


# ─────────────────────────────────────────────────────────── direct


def test_direct_query_is_an_identity_transform() -> None:
    """`/rag/retrieve`：三句全等于原句，vector_queries 单元素。"""

    result = direct_query("退款怎么走")

    assert isinstance(result, QueryProcessResult)
    assert (result.raw_query, result.effective_query, result.search_query) == (
        "退款怎么走",
        "退款怎么走",
        "退款怎么走",
    )
    assert result.vector_queries == ("退款怎么走",)
    assert result.rewrite_applied is False
    assert result.duration_ms == 0.0


# ─────────────────────────────────────────────────────────── rewrite


def test_rewrite_feeds_the_effective_query_to_the_vector_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改写成功：向量一路是「原句 + 改写句」两句，顺序固定。"""

    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(optimize_for_rewrite(query="退钱怎么弄"))

    assert result.vector_queries == ("退钱怎么弄", "退款到账时限")
    assert result.effective_query == "退款到账时限"
    assert result.search_query == "退款到账时限"
    assert result.rewrite_applied is True


def test_synonyms_are_expanded_on_the_rewritten_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同义词在**改写结果上**扩展，不是在原句上。"""

    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", True)
    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(
        optimize_for_rewrite(query="退钱怎么弄", synonym_groups=_groups("退款"))
    )

    # 改写句里含「退款」→ 命中该组 → 追加 expand，所以 search_query 比 effective 长。
    assert "返还货款" in result.search_query
    assert result.search_query != result.effective_query


def test_identical_rewrite_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型原样返回时，vector_queries 去重成一句。"""

    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款怎么走")))

    result = asyncio.run(optimize_for_rewrite(query="退款怎么走"))

    assert result.vector_queries == ("退款怎么走",)


def test_a_failed_rewrite_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """改写失败 → 整单 50003，绝不回退原句继续检索。"""

    _install(monkeypatch, _FakeLLM(error=RuntimeError("模型服务 502")))

    with pytest.raises(AppError) as caught:
        asyncio.run(optimize_for_rewrite(query="退款怎么走", synonym_groups=_groups("退款")))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_optimize_for_rewrite_reports_a_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """耗时是查询加工那一段的真实读数（非 0 也由替身决定，这里只断言非负）。"""

    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    _install(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款到账时限")))

    result = asyncio.run(optimize_for_rewrite(query="退款怎么走"))

    assert result.duration_ms >= 0
