"""大模型能力出口：抽象 + 按「供应商 + 档位」装配的工厂。

换供应商只改环境变量（``LLM_PROVIDER`` 与该家的 key / 模型名），业务代码零改动；
一家都没配时返回空实现，离线环境照常启动（仓库的离线测试全都跑在这条路上）。

**为什么参数装配写在这里而不是散在调用点**：``init_chat_model`` 的 provider 名、
默认地址、key 落在哪个配置项，是一张「事实表」。它只该有一份，否则换一家供应商就
要改 N 处调用点，而这份表恰好就是「只改环境变量」这句话的实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.providers.llm.langchain_llm import (
    LangChainLLMProvider,
    apply_framework_telemetry_switches,
)
from app.providers.llm.noop import NoopLLMProvider


@dataclass(frozen=True)
class _Vendor:
    """一家供应商的装配知识：框架里的 provider 名、默认地址、key 落在哪个配置项。"""

    chat_provider: str
    default_base_url: str
    key_setting: str


# 前三家（DeepSeek / 千问 / 豆包 Ark）都讲 OpenAI 兼容协议，只是地址与 key 不同——
# 这正是「换一家只改环境变量」能成立的现实基础。千问没有固定地址：工作空间 ID 嵌在
# URL 里（每个租户一份），只能由运维填 LLM_BASE_URL，这里给空串表示「必须自己填」。
_VENDORS: dict[str, _Vendor] = {
    "deepseek": _Vendor("openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "qwen": _Vendor("openai", "", "QWEN_API_KEY"),
    "ark": _Vendor("openai", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"),
    "openai": _Vendor("openai", "", "OPENAI_API_KEY"),
    "anthropic": _Vendor("anthropic", "", "ANTHROPIC_API_KEY"),
    "gemini": _Vendor("google_genai", "", "GEMINI_API_KEY"),
}

# 两档：fast 用于查询改写与重排（要快、要稳），strong 留给 Research（要准）。
_TIERS = ("fast", "strong")

# 「关掉思考」在配置里的写法；其余取值原样透传成框架的 reasoning_effort。
_REASONING_OFF = ("", "off", "none", "false", "0")


def get_llm_provider(tier: str = "fast") -> LLMProvider:
    """按供应商与档位装配。

    缺任何一环（供应商未知、key 没配）都退回空实现或立刻报错，不做「半装配」——
    半装配的症状是发出去了但一直 401，比启动就缺能力难查得多。
    """

    if tier not in _TIERS:
        raise ValueError(f"未知档位 {tier!r}，只支持 {_TIERS}")

    vendor = _VENDORS.get(settings.LLM_PROVIDER.strip().lower())
    if vendor is None:
        raise ValueError(
            f"未知的大模型供应商 {settings.LLM_PROVIDER!r}，只支持 {tuple(_VENDORS)}"
        )

    api_key = getattr(settings, vendor.key_setting, "")
    if not api_key:
        # 没配 key = 这台机器上没有大模型能力。空实现让它明确不可用，而不是启动失败，
        # 也不是静默降级成「检索照常、改写悄悄不生效」。
        return NoopLLMProvider()

    apply_framework_telemetry_switches()

    model_name = settings.LLM_MODEL if tier == "fast" else settings.LLM_STRONG_MODEL
    options: dict[str, Any] = {
        "api_key": api_key,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
    }
    base_url = settings.LLM_BASE_URL or vendor.default_base_url
    if base_url:
        options["base_url"] = base_url

    # fast 档写死关思考：改写与重排要的是低延迟与稳定输出，这是代码约束而不是
    # 运维纪律（配置里写 high 也影响不到 fast）。strong 档才由配置决定。
    if tier == "strong":
        effort = settings.LLM_REASONING_EFFORT.strip().lower()
        if effort not in _REASONING_OFF:
            options["reasoning_effort"] = effort

    chat_model = init_chat_model(model_name, model_provider=vendor.chat_provider, **options)
    return LangChainLLMProvider(chat_model=chat_model, model_name=model_name)


__all__ = ["LLMProvider", "LangChainLLMProvider", "get_llm_provider"]
