"""大模型 Provider：启动期配置校验 + 工厂选型 + LangChain 实现的错误契约。

全部离线。用 LangChain 自带的 Fake 聊天模型走结构化输出链路，用构造期不联网的
ChatOpenAI / ChatAnthropic / ChatGoogleGenerativeAI 验供应商装配——真正发请求的那条路
需要供应商 key，离线测试碰不到，它能证明的只有「参数装对了、档位选对了」。

**为什么 ``LLM_PROVIDER`` 要有启动期校验**：真踩过的坑。用户本机的 ``.env`` 里写的是
旧值 ``LLM_PROVIDER=openai_compatible``（一个历史名字，工厂根本不认）。修复前进程照常
起来，直到「第一次调用改写 / 重排」才炸——那时已经在请求链路上，表现为一次 500，
排查要从 API 日志倒推回一个环境变量。把校验放在 ``Settings`` 上，拼错的名字会让进程
起不来，错误信息里直接带着允许值。

「结构化输出非法」这条之所以值得单测，是因为框架在这里有个反直觉行为：模型一个 tool_call
都不返回时，``with_structured_output`` 不抛异常、而是**返回 None**。若 Provider 不自己兜住，
业务层会拿到 None 当成合法结果继续跑下去。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, settings
from app.core.exceptions import AppError, ErrorCode
from app.core.llm_vendors import LLM_VENDORS
from app.providers.llm import _VENDORS, get_llm_provider
from app.providers.llm.langchain_llm import LangChainLLMProvider
from app.providers.llm.noop import NoopLLMProvider

_VENDOR_KEYS = (
    "DEEPSEEK_API_KEY",
    "QWEN_API_KEY",
    "ARK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)


class _RewriteResult(BaseModel):
    """测试用的最小结构化输出：查询改写要产出的形状。"""

    query: str
    synonyms: list[str] = []


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """把所有模型 key 清空，让每个用例从「什么都没配」这一确定性状态出发。"""

    for name in _VENDOR_KEYS:
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "deepseek")
    return monkeypatch


class _ToolCallFakeChatModel(FakeListChatModel):
    """LangChain 自带 FakeListChatModel + 一层 bind_tools。

    自带 Fake 都不实现 bind_tools，而 BaseChatModel.with_structured_output 第一件事就是
    bind_tools(...) | 解析器。不补这一层，「结构化输出」这条路径根本走不到，测出来的
    就不是框架的真实行为。
    """

    tool_args: object = {}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        name = tools[0] if isinstance(tools[0], str) else tools[0].__name__
        message = AIMessage(
            content="",
            tool_calls=[{"name": name, "args": self.tool_args, "id": "call_1"}],
        )
        return self | RunnableLambda(lambda _input: message)


class _SilentFakeChatModel(FakeListChatModel):
    """一个 tool_call 都不返回的模型：框架在这种情况返回 None 而不是抛异常。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        return self


def _provider_with(fake_model: FakeListChatModel) -> LangChainLLMProvider:
    return LangChainLLMProvider(chat_model=fake_model, model_name="fake-model")


def test_factory_returns_noop_when_no_key_configured(isolated_settings: pytest.MonkeyPatch) -> None:
    """没配任何 key 的机器（离线测试 / CI）必须能拿到空实现而不是启动失败。"""

    assert isinstance(get_llm_provider(), NoopLLMProvider)
    assert isinstance(get_llm_provider("strong"), NoopLLMProvider)


# ─────────────────────────────────────────────────── 启动期校验（本票新增）


def test_unknown_llm_provider_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写错供应商名必须**启动就炸**，不能等到第一次改写 / 重排才 500。

    现场值：用户本机 ``.env`` 是 ``LLM_PROVIDER=openai_compatible``——一个历史上
    出现过、但工厂不支持的名字。修复前 ``Settings()`` 照常构造成功，进程照常起来，
    错误要到请求链路上才暴露；修复后它在读完配置的那一刻就失败。

    用环境变量而不是关键字参数构造：这才是「进程启动」的那条路径
    （``settings = Settings()`` 在 import 时读环境）。
    """

    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "LLM_PROVIDER" in message
    assert "openai_compatible" in message
    # 报错必须把允许值列出来，否则运维还得回代码里翻工厂支持哪几家。
    for vendor in LLM_VENDORS:
        assert vendor in message


@pytest.mark.parametrize("vendor", LLM_VENDORS)
def test_every_supported_vendor_passes_startup_validation(vendor: str) -> None:
    """支持列表里的每一个值都要能构造成功——校验器不能比工厂更严。"""

    assert Settings(_env_file=None, LLM_PROVIDER=vendor).LLM_PROVIDER == vendor


def test_llm_provider_is_normalized_before_validation() -> None:
    """大小写与空白不算拼错：``DeepSeek `` 与工厂的 ``deepseek`` 是同一家。"""

    assert Settings(_env_file=None, LLM_PROVIDER=" DeepSeek ").LLM_PROVIDER == "deepseek"


def test_vendor_names_have_exactly_one_source_of_truth() -> None:
    """名单只有一份：``app.core.llm_vendors``，config 与工厂都 import 它。

    这条用例是「别在 config.py 里复制一份厂商名」的牙齿：工厂的装配表按名字索引，
    一旦有人只在一边加了一家（或写错一个字母），这里当场红。
    """

    assert tuple(_VENDORS) == LLM_VENDORS


def test_default_llm_provider_is_a_supported_vendor() -> None:
    """默认值本身也走校验：``Settings(_env_file=None)`` 不许抛错。"""

    assert Settings(_env_file=None).LLM_PROVIDER in LLM_VENDORS


def test_factory_maps_fast_and_strong_tiers_to_their_models(
    isolated_settings: pytest.MonkeyPatch,
) -> None:
    """fast 与 strong 是两个模型名，选错档位在真实调用里只表现为「贵了 / 慢了」。"""

    isolated_settings.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    isolated_settings.setattr(settings, "LLM_MODEL", "deepseek-flash")
    isolated_settings.setattr(settings, "LLM_STRONG_MODEL", "deepseek-v4-pro")

    assert get_llm_provider().model_name == "deepseek-flash"
    assert get_llm_provider("strong").model_name == "deepseek-v4-pro"


@pytest.mark.parametrize(
    ("vendor", "key_setting", "expected_model_class"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "ChatOpenAI"),
        ("qwen", "QWEN_API_KEY", "ChatOpenAI"),
        ("ark", "ARK_API_KEY", "ChatOpenAI"),
        ("openai", "OPENAI_API_KEY", "ChatOpenAI"),
        ("anthropic", "ANTHROPIC_API_KEY", "ChatAnthropic"),
        ("gemini", "GEMINI_API_KEY", "ChatGoogleGenerativeAI"),
    ],
)
def test_each_vendor_is_reachable_with_environment_variables_only(
    isolated_settings: pytest.MonkeyPatch,
    vendor: str,
    key_setting: str,
    expected_model_class: str,
) -> None:
    """换供应商只改环境变量：没有一条分支需要动业务代码。"""

    isolated_settings.setattr(settings, "LLM_PROVIDER", vendor)
    isolated_settings.setattr(settings, key_setting, "sk-test")

    provider = get_llm_provider()

    assert isinstance(provider, LangChainLLMProvider)
    assert type(provider.chat_model).__name__ == expected_model_class


def test_fast_tier_never_enables_reasoning(isolated_settings: pytest.MonkeyPatch) -> None:
    """fast 档写死关思考模式：改写与重排要的是低延迟与稳定输出。

    这一条不能用「配置文件里别写 high」来保证——那是运维纪律，不是代码约束。
    """

    isolated_settings.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    isolated_settings.setattr(settings, "LLM_REASONING_EFFORT", "high")

    assert get_llm_provider("fast").chat_model.reasoning_effort is None
    assert get_llm_provider("strong").chat_model.reasoning_effort == "high"


def test_unknown_tier_is_rejected(isolated_settings: pytest.MonkeyPatch) -> None:
    """档位只有两档；写错要立刻炸，不能悄悄按 fast 跑。"""

    isolated_settings.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")

    with pytest.raises(ValueError):
        get_llm_provider("medium")


def test_chat_structured_returns_schema_instance() -> None:
    """结构化输出的正常路径：拿回来的是 schema 实例，不是一个裸 dict。"""

    fake = _ToolCallFakeChatModel(
        responses=["unused"], tool_args={"query": "退款", "synonyms": ["退钱"]}
    )

    result = asyncio.run(
        _provider_with(fake).chat_structured(
            _RewriteResult, system="你是查询改写器", user="背调要问啥"
        )
    )

    assert isinstance(result, _RewriteResult)
    assert result.query == "退款"
    assert result.synonyms == ["退钱"]


def test_chat_structured_raises_llm_failed_when_model_returns_nothing() -> None:
    """模型不返回 tool_call 时框架给 None——必须当成失败，不能当合法结果流出去。"""

    fake = _SilentFakeChatModel(responses=["我拒绝输出结构化数据"])

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider_with(fake).chat_structured(_RewriteResult, system="s", user="u"))

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_chat_structured_raises_llm_failed_on_invalid_arguments() -> None:
    """字段类型对不上时框架抛 pydantic 校验错；对调用方统一成 LLM_FAILED。"""

    fake = _ToolCallFakeChatModel(
        responses=["unused"], tool_args={"query": 123, "synonyms": "退钱"}
    )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider_with(fake).chat_structured(_RewriteResult, system="s", user="u"))

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_chat_json_returns_parsed_dict() -> None:
    """JSON 便捷入口：把 ```json 围栏剥掉、解析成 dict。"""

    fake = FakeListChatModel(responses=['```json\n{"query": "退款"}\n```'])

    result = asyncio.run(_provider_with(fake).chat_json(system="s", user="u"))

    assert result == {"query": "退款"}


def test_chat_json_raises_llm_failed_on_non_json() -> None:
    """不是 JSON 就报错，不返回空 dict——空 dict 会让上游静默走错分支。"""

    fake = FakeListChatModel(responses=["抱歉，我无法回答这个问题。"])

    with pytest.raises(AppError) as excinfo:
        asyncio.run(_provider_with(fake).chat_json(system="s", user="u"))

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_noop_provider_raises_llm_failed_for_both_methods() -> None:
    """空实现两个方法都要炸：能力不可用要明确，不能静默降级。"""

    noop = NoopLLMProvider()

    with pytest.raises(AppError) as first:
        asyncio.run(noop.chat_json(system="s", user="u"))
    assert first.value.code == ErrorCode.LLM_FAILED

    with pytest.raises(AppError) as second:
        asyncio.run(noop.chat_structured(_RewriteResult, system="s", user="u"))
    assert second.value.code == ErrorCode.LLM_FAILED


def test_building_provider_switches_framework_telemetry_off(
    isolated_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """框架的追踪客户端读的是环境变量，不是我们的 Settings。

    配置模板里写了 false 只是文档；真正拦住外发的是构造模型时把开关钉成 false。
    """

    isolated_settings.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    isolated_settings.setattr(settings, "LANGCHAIN_TRACING_V2", False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    get_llm_provider()

    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
