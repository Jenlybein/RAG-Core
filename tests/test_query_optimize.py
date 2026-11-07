"""查询优化的编排：先改写、再同义词扩展（模块接缝）。

两层处理的**顺序**是这张票的核心：改写补语义、同义词补字面。顺序反了，同义词表就
匹配不到「改写之后才出现」的词，等于白配。

本文件守的契约（父 spec「代码结构与模块边界」：查询优化拆成独立模块，同义词扩展是
纯函数；「查询优化 → 召回」）：

* 两个开关都关 → ``raw / effective / search`` 是同一句，且一次模型都不调；
* 只开改写 → ``search_query`` 跟着 ``effective_query`` 走（同义词表在但开关关 = 不扩展）；
* 只开同义词 → ``effective_query`` 是原句，``search_query`` 是扩展后的句子；
* 两个都开 → ``effective_query`` 是改写结果，``search_query`` 是**在改写结果上**扩展的；
* 改写失败 → ``effective_query`` 回退原句，同义词**仍然作用在原句上**，不整单失败。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.services.query.optimizer import QueryProcessResult, optimize_query
from app.services.query.rewrite import QueryRewrite
from app.services.query.synonyms import collect_synonym_groups


class _FakeLLM(LLMProvider):
    """替身大模型：改写阶段唯一会用到的方法就是 ``chat_structured``。"""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.system_prompts: list[str] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        self.system_prompts.append(system)
        if self._error is not None:
            raise self._error
        return self._result

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写只走结构化输出")


def _install_llm(monkeypatch: pytest.MonkeyPatch, llm: LLMProvider) -> None:
    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda role="fast": llm)


# 「退款」这一组：中文精确子串；expand 里故意放两个词，验证空格分隔的追加形状。
_SYNONYMS = ({"terms": ["退款", "退钱"], "expand": ["退款流程", "售后"]},)


def _switches(monkeypatch: pytest.MonkeyPatch, *, rewrite: bool, synonyms: bool) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", rewrite)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", synonyms)


def _groups(table: list[dict[str, list[str]]]) -> tuple:
    """把「库里存的那种 JSON 形状」变成纯函数要的词表。"""

    return collect_synonym_groups([{"synonyms": table}])


# ───────────────────────────────────────────────────────── 两个都关


def test_both_switches_off_keeps_all_three_queries_equal_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归红线：两个开关关掉时这一步是**恒等变换**。"""

    _switches(monkeypatch, rewrite=False, synonyms=False)
    monkeypatch.setattr(
        "app.providers.llm.get_llm_provider",
        lambda role="fast": (_ for _ in ()).throw(AssertionError("关掉时不许装配大模型")),
    )

    result = asyncio.run(optimize_query(query="退款怎么走", synonym_groups=_groups(_SYNONYMS)))

    assert isinstance(result, QueryProcessResult)
    assert result.raw_query == result.effective_query == result.search_query == "退款怎么走"
    assert result.rewrite_applied is False
    assert result.rewrite_degraded is False
    assert result.rewrite_reason is None


# ───────────────────────────────────────────────────────── 单开一层


def test_rewrite_only_makes_search_query_follow_effective_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只开改写：关键词一路也拿改写后的句子，同义词表在但开关关 = 不扩展。"""

    _switches(monkeypatch, rewrite=True, synonyms=False)
    _install_llm(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款的处理流程")))

    result = asyncio.run(optimize_query(query="退钱怎么弄", synonym_groups=_groups(_SYNONYMS)))

    assert result.effective_query == "退款的处理流程"
    assert result.search_query == "退款的处理流程", "同义词关着，即便句子里有「退款」也不扩展"
    assert result.rewrite_applied is True


def test_synonyms_only_expand_the_raw_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _switches(monkeypatch, rewrite=False, synonyms=True)
    monkeypatch.setattr(
        "app.providers.llm.get_llm_provider",
        lambda role="fast": (_ for _ in ()).throw(AssertionError("同义词不需要大模型")),
    )

    result = asyncio.run(optimize_query(query="退钱怎么弄", synonym_groups=_groups(_SYNONYMS)))

    assert result.effective_query == "退钱怎么弄"
    assert result.search_query == "退钱怎么弄 退款流程 售后"


# ───────────────────────────────────────────────────────── 两层都开


def test_synonym_expansion_runs_on_the_rewritten_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """顺序钉死：同义词匹配的是 ``effective_query``，不是用户原话。

    原话「退钱怎么弄」不含「退款」以外的命中词，改写结果「退款的处理流程」含「退款」；
    如果实现先扩展再改写（或者用原话去扩展），这条用例就会拿到没扩展的 ``search_query``。
    """

    _switches(monkeypatch, rewrite=True, synonyms=True)
    # 原话里没有「退款」两字，只有「退钱」——同义词表两种都配了，所以这里再收紧一次：
    # 让词表只认「退款」，就能证明扩展发生在改写**之后**。
    groups = _groups([{"terms": ["退款"], "expand": ["退款流程"]}])
    _install_llm(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款的办理流程")))

    result = asyncio.run(optimize_query(query="退钱怎么弄", synonym_groups=groups))

    assert result.effective_query == "退款的办理流程"
    assert result.search_query == "退款的办理流程 退款流程"


def test_rewrite_failure_falls_back_then_synonyms_still_expand_the_raw_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改写失败：``effective_query`` 回退原句，同义词照常扩展原句，且标记降级。"""

    _switches(monkeypatch, rewrite=True, synonyms=True)
    _install_llm(monkeypatch, _FakeLLM(error=RuntimeError("timeout")))

    result = asyncio.run(optimize_query(query="退款怎么走", synonym_groups=_groups(_SYNONYMS)))

    assert result.effective_query == "退款怎么走"
    assert result.search_query == "退款怎么走 退款流程 售后"
    assert result.rewrite_applied is False
    assert result.rewrite_degraded is True
    assert result.rewrite_reason == "query rewrite failed"


def test_optimize_query_reports_a_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """耗时是这一步的可观测事实：关掉时也要有（几乎为 0），不能是 ``None``。"""

    _switches(monkeypatch, rewrite=False, synonyms=True)

    result = asyncio.run(optimize_query(query="退款怎么走", synonym_groups=_groups(_SYNONYMS)))

    assert result.duration_ms is not None
    assert result.duration_ms >= 0
