"""大模型能力出口：抽象 + 按「供应商 + 模型角色」装配的工厂。

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
from app.core.llm_vendors import LLM_VENDORS
from app.providers.langchain_runtime import apply_framework_telemetry_switches
from app.providers.llm.base import LLMProvider
from app.providers.llm.langchain_llm import LangChainLLMProvider
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
#
# **这张表的键必须与 ``app.core.llm_vendors.LLM_VENDORS`` 逐个相等**：那份名单是
# ``LLM_PROVIDER`` 启动期校验的依据，这张表是装配知识。名单在 config 侧（那里要校验），
# 装配知识在工厂侧（那是 LangChain 的参数），两边都不复制对方的职责；对应关系由
# ``tests/test_llm_provider.py::test_vendor_names_have_exactly_one_source_of_truth``
# 钉住——只在一处加一家，用例当场变红。
_VENDORS: dict[str, _Vendor] = {
    "deepseek": _Vendor("openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "qwen": _Vendor("openai", "", "QWEN_API_KEY"),
    "ark": _Vendor("openai", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"),
    "openai": _Vendor("openai", "", "OPENAI_API_KEY"),
    "anthropic": _Vendor("anthropic", "", "ANTHROPIC_API_KEY"),
    "gemini": _Vendor("google_genai", "", "GEMINI_API_KEY"),
}

# 名单与装配表的对应关系在 import 时就对一遍：两份名单漂移的症状是「启动通过了、
# 装配时 KeyError / 未知供应商」，那样错误发生在请求链路上，比这里贵得多。
if tuple(_VENDORS) != LLM_VENDORS:  # pragma: no cover - 名单不一致时才会命中
    raise RuntimeError(
        "厂商名单不一致：app.core.llm_vendors.LLM_VENDORS 与 app.providers.llm._VENDORS "
        f"必须逐个相等，当前分别是 {LLM_VENDORS} 与 {tuple(_VENDORS)}。"
    )

# 两种**模型角色**：fast 用于查询改写与重排（要快、要稳），strong 留给 Research（要准）。
#
# 为什么叫「角色」而不是「档位」（GLOSSARY 合规）：本仓库里已经有另外两个同形词，指的
# 都不是这件事——「套餐」是计费档（术语表里那个计费档的禁用词，本仓库不这么写），「档位」
# 是第 5 步的质量档位（speed / balanced / quality / custom）。这里说的是「同一家供应商下
# 哪个模型干什么活」，与计费、与质量档位都无关，所以三者必须用不同的词。
_MODEL_ROLES = ("fast", "strong")

# 「关掉思考」在配置里的写法；其余取值原样透传成框架的 reasoning_effort。
_REASONING_OFF = ("", "off", "none", "false", "0")


def get_llm_provider(role: str = "fast") -> LLMProvider:
    """按供应商与模型角色装配。

    缺任何一环（供应商未知、key 没配）都退回空实现或立刻报错，不做「半装配」——
    半装配的症状是发出去了但一直 401，比启动就缺能力难查得多。
    """

    if role not in _MODEL_ROLES:
        raise ValueError(f"未知模型角色 {role!r}，只支持 {_MODEL_ROLES}")

    # 遥测开关必须在**任何**客户端被构造之前落地，包括下面这条「没配 key、走空实现」
    # 的早退路径：审计要看的是「这个进程里的框架永远不外发」，而不是「有 key 时才
    # 不外发」。放在早退之前，是这个函数唯一不会漏掉的位置。
    apply_framework_telemetry_switches()

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

    model_name = settings.LLM_MODEL if role == "fast" else settings.LLM_STRONG_MODEL
    options: dict[str, Any] = {
        "api_key": api_key,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
    }
    base_url = settings.LLM_BASE_URL or vendor.default_base_url
    if base_url:
        options["base_url"] = base_url

    # fast 角色写死关思考：改写与重排要的是低延迟与稳定输出，这是代码约束而不是
    # 运维纪律（配置里写 high 也影响不到 fast）。strong 角色才由配置决定。
    if role == "strong":
        effort = settings.LLM_REASONING_EFFORT.strip().lower()
        if effort not in _REASONING_OFF:
            options["reasoning_effort"] = effort
    else:
        # 温度固定 0：改写与重排都是**确定性**步骤，而各家的默认温度是 1.0 量级。
        # 留默认值会让「同样的请求两次返回相同顺序」这条承诺直接失效，而 RRF 把
        # k=60 写死、融合加 tie-breaker 全是为了同一件事。strong 角色不设温度：
        # Research 需要一点发散，且那个角色的参数不该由检索链路替它决定。
        options["temperature"] = 0

    chat_model = init_chat_model(model_name, model_provider=vendor.chat_provider, **options)
    return LangChainLLMProvider(chat_model=chat_model, model_name=model_name)


__all__ = ["LLMProvider", "LangChainLLMProvider", "get_llm_provider"]
