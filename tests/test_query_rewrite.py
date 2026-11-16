"""查询改写（第 13 步）：`/rag/rewrite` 的那一次模型调用（模块接缝，不起应用）。

接缝仍是既有的那一个：改写阶段通过 ``app.providers.llm.get_llm_provider`` 拿大模型
（惰性 import 所以 monkeypatch 生效），测试用它换替身。断言的只有 ``RewriteOutcome``
与替身收到的调用，**不发网络、不连数据库、不读 .env**。

本文件守的契约：

* 改写**总是**装配一次大模型（端点决定要不要加工，不再有开关）；用的是单一
  ``default`` 模型角色；
* 成功时 ``query`` 换成模型给的那句，且 ``duration_ms`` 有值；
* 超时 / 抛错 / 空返回 / 空串 / 结构不合法 → 抛 ``AppError(50003)``，**绝不**静默
  回退成原句（客户重试即可）；
* 提示词**只做改写、不做问答**，并带上知识库名称 / 描述（可选 ``hint``）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.services.query.rewrite import QueryRewrite, rewrite_query


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
    """替换大模型出口，并把「被要了哪个模型角色」记下来。"""

    roles: list[str] = []

    async def _factory(role: str = "default") -> LLMProvider:
        roles.append(role)
        return llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _factory)
    return roles


# ─────────────────────────────────────────────────────────── 正常路径


def test_rewrite_uses_the_single_default_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """改写装配一次大模型，角色是唯一的 ``default``。"""

    roles = _install_llm(monkeypatch, _FakeLLM(result=QueryRewrite(query="退款多久到账")))

    outcome = asyncio.run(rewrite_query(query="退钱怎么弄", hint="人事制度库"))

    assert outcome.query == "退款多久到账"
    assert outcome.duration_ms >= 0
    assert roles == ["default"]


def test_rewrite_prompt_only_asks_for_a_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """提示词只描述改写，不给答题角色；可选 hint 带上知识库名称 / 描述。"""

    llm = _FakeLLM(result=QueryRewrite(query="退款到账时限"))
    _install_llm(monkeypatch, llm)

    asyncio.run(rewrite_query(query="退钱怎么弄", hint="人事制度库（入职与离职制度）"))

    system, user = llm.calls[0]["system"], llm.calls[0]["user"]
    assert "不回答" in system and "不编造" in system
    assert "退钱怎么弄" in user
    assert "人事制度库（入职与离职制度）" in user


# ───────────────────────────────────────── 模型调用失败：整单 50003


@pytest.mark.parametrize(
    "llm",
    [
        _FakeLLM(error=RuntimeError("模型服务 502")),
        _FakeLLM(result=QueryRewrite(query="  ")),
        _FakeLLM(result=QueryRewrite(query="")),
        _FakeLLM(result={"query": ""}),
        _FakeLLM(result=None),
        _FakeLLM(result={"oops": 1}),
    ],
    ids=["exception", "blank", "empty", "dict_empty", "none", "malformed"],
)
def test_a_model_call_failure_is_a_50003(monkeypatch: pytest.MonkeyPatch, llm: _FakeLLM) -> None:
    """超时 / 抛错 / 空返回 / 空串 / 结构不合法 → 统一 50003，绝不回退原句。"""

    _install_llm(monkeypatch, llm)

    with pytest.raises(AppError) as caught:
        asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_timeout_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时也是模型调用失败，不降级。"""

    _install_llm(
        monkeypatch,
        _FakeLLM(result=QueryRewrite(query="不该走到这里"), delay=1.0),
    )

    with pytest.raises(AppError) as caught:
        asyncio.run(rewrite_query(query="背调要问啥", hint=None, timeout_ms=10))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_assembly_failure_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配期抛错（供应商拼错）也是模型调用失败，不降级。"""

    async def _boom(_role: str = "default") -> LLMProvider:
        raise ValueError("未知的大模型供应商 'typo'")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _boom)

    with pytest.raises(AppError) as caught:
        asyncio.run(rewrite_query(query="背调要问啥", hint=None))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED
