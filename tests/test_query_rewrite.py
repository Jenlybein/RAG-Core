"""查询改写：关闭不调用、成功替换、失败即降级（模块接缝，不起应用）。

接缝仍是既有的那一个：改写阶段通过 ``app.providers.llm.get_llm_provider`` 拿大模型
（与重排完全同一处替换点，惰性 import 所以 monkeypatch 生效），测试用它换替身。
断言的只有 ``RewriteOutcome`` 与替身收到的调用，**不发网络、不连数据库、不读 .env**。

本文件守的契约（父 spec「降级矩阵」：查询改写失败（含超时、空返回）→ 降级：用原句继续）：

* 关闭时**根本不装配大模型**（换成「一被调用就抛错」的桩也能通过）；
* 成功时 ``effective_query`` 换成模型给的那句，且用的是 **fast 模型角色**；
* 超时 / 抛错 / 空返回 / 空串 → 回退原句，原因固定 ``query rewrite failed``
  ——一因一码，绝不把异常文本拼进去；
* 提示词**只做改写、不做问答**，并带上知识库名称 / 描述（可选 ``rewrite_hint``）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.services.query.rewrite import (
    REASON_REWRITE_FAILED,
    QueryRewrite,
    rewrite_query,
)


# ─────────────────────────────────────────────────────────────── 替身


class _FakeLLM(LLMProvider):
    """替身大模型：按预置结果作答 / 抛错 / 慢到超时，并记账收到的提示词。"""

    def __init__(
        self,
        *,
        result: Any = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._result = result
        self._error = error
        self._delay = delay
        self.calls: list[dict[str, str]] = []

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        self.calls.append({"system": system, "user": user})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写只走结构化输出")


def _install_llm(monkeypatch: pytest.MonkeyPatch, llm: LLMProvider) -> list[str]:
    """替换大模型出口，并把「被要了哪个模型角色」记下来。

    角色（fast / strong）是**代码约束**而不是运维纪律：改写与重排必须是快档。
    这里把实参记下来，就是为了让「写成 strong」这件事当场失败。
    """

    roles: list[str] = []

    def _factory(role: str = "fast") -> LLMProvider:
        roles.append(role)
        return llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _factory)
    return roles


# ─────────────────────────────────────────────────────────────── 关闭


def test_disabled_rewrite_never_builds_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭：连大模型都不装配。证法是「换成会抛错的桩仍然通过」。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    def _forbidden(_role: str = "fast") -> LLMProvider:  # pragma: no cover
        raise AssertionError("关闭改写时不许装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint="人事制度库"))

    assert outcome.query == "背调要问啥", "关闭时 effective_query 就是原句"
    assert outcome.applied is False
    assert outcome.reason is None, "没开启不是降级"
    assert outcome.duration_ms is None, "没调用就没有耗时"


# ─────────────────────────────────────────────────────────────── 成功


def test_rewrite_replaces_the_query_using_the_fast_model_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    llm = _FakeLLM(result=QueryRewrite(query="  背景调查的标准问题清单  "))
    roles = _install_llm(monkeypatch, llm)

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint="人事制度库"))

    assert outcome.query == "背景调查的标准问题清单", "去掉模型带来的首尾空白"
    assert outcome.applied is True
    assert outcome.reason is None
    assert outcome.duration_ms is not None
    assert roles == ["fast"], "改写必须用 fast 模型角色，不是 strong"


def test_prompt_only_rewrites_and_carries_the_knowledge_base_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提示词只做改写、不做问答；用户原话与知识库名称 / 描述都要在。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    llm = _FakeLLM(result=QueryRewrite(query="背景调查问题清单"))
    _install_llm(monkeypatch, llm)

    asyncio.run(rewrite_query(query="背调要问啥", hint="人事制度库（入职与离职制度）"))

    sent = llm.calls[0]
    assert "背调要问啥" in sent["user"]
    assert "人事制度库" in sent["user"] and "入职与离职制度" in sent["user"]
    # 只做改写：系统提示词必须明确「不回答、不解释、不编造」，并给字数上限。
    for instruction in ("改写", "不回答", "不编造", "30"):
        assert instruction in sent["system"]


def test_rewrite_works_without_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """知识库名称 / 描述是**可选**的：没有它也要能改写，只是提示词更短。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    llm = _FakeLLM(result=QueryRewrite(query="退款流程"))
    _install_llm(monkeypatch, llm)

    outcome = asyncio.run(rewrite_query(query="退钱怎么弄", hint=None))

    assert outcome.query == "退款流程"
    assert "知识库" not in llm.calls[0]["user"]


# ─────────────────────────────────────────────────────── 失败即降级


def test_rewrite_timeout_falls_back_to_the_raw_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时：回退原句 + 固定原因，不抛出去。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(settings, "QUERY_REWRITE_TIMEOUT_MS", 10)
    llm = _FakeLLM(result=QueryRewrite(query="不该走到这里"), delay=1.0)
    _install_llm(monkeypatch, llm)

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert outcome.query == "背调要问啥"
    assert outcome.applied is False
    assert outcome.reason == REASON_REWRITE_FAILED


def test_rewrite_error_falls_back_to_the_raw_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    llm = _FakeLLM(error=RuntimeError("模型服务 502"))
    _install_llm(monkeypatch, llm)

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert outcome.query == "背调要问啥"
    assert outcome.reason == "query rewrite failed", "一因一码，不拼异常文本"
    assert "502" not in (outcome.reason or "")


@pytest.mark.parametrize(
    "returned",
    [
        QueryRewrite(query="   "),
        QueryRewrite(query=""),
        {"query": ""},
        None,
    ],
)
def test_rewrite_empty_result_falls_back_to_the_raw_query(
    monkeypatch: pytest.MonkeyPatch, returned: Any
) -> None:
    """空返回 / 空串同样是失败：绝不能把 ``effective_query`` 变成空串去检索。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    _install_llm(monkeypatch, _FakeLLM(result=returned))

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert outcome.query == "背调要问啥"
    assert outcome.applied is False
    assert outcome.reason == REASON_REWRITE_FAILED


def test_rewrite_malformed_result_falls_back_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构不合法（不是 schema、也不是带 ``query`` 的 dict）→ 降级，不冒泡。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    _install_llm(monkeypatch, _FakeLLM(result={"oops": 1}))

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert outcome.query == "背调要问啥"
    assert outcome.reason == REASON_REWRITE_FAILED


def test_rewrite_assembly_failure_falls_back_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配期抛错（例如 ``LLM_PROVIDER`` 写错）同样只降级：它是可选优化，不是检索前提。"""

    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    def _boom(_role: str = "fast") -> LLMProvider:
        raise ValueError("未知的大模型供应商 'typo'")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _boom)

    outcome = asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert outcome.query == "背调要问啥"
    assert outcome.applied is False
    assert outcome.reason == REASON_REWRITE_FAILED
