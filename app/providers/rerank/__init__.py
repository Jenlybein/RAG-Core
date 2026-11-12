"""重排能力出口：抽象 + 按配置选择的工厂。

## 语义（第 10 步票 3 起，**破坏性变更**）

``RERANK_ENABLED`` 现在说的是「**用专用端点**重排」，不再是「让通用大模型逐条打分」：

* ``RERANK_ENABLED=false`` → 空实现（默认路径，一次远程调用都没有）；
* ``true`` 且端点已配（地址 + 密钥都有）→ ``Qwen3RerankProvider``（``qwen3.7-text-rerank``）；
* ``true`` 但端点没配 → **空实现**（明确不可用），**不再回落** ``LLMRerankProvider``。

为什么不再回落：回落会把「这台机器没配重排端点」变成「它正在用另一种方式重排」，而
两种实现的延迟差一个数量级（10.9s vs 单个位数秒）、读数不可比，回显却都写着
``rerank: true``。明确不可用至少能让调用方看见 ``applied=false``。

旧实现 ``LLMRerankProvider`` **类保留**，并由 :func:`build_llm_rerank_provider` 显式装配：
评测要拿同一份题集跑「旧的重排」做对照（``--rerank-provider llm``），生产工厂不再返回它。

## 为什么用开关而不是两套分支

同一份代码在不同机器上表现为「有没有重排能力」，业务编排不需要知道今天是哪一种——
它只知道「调重排，失败就降级」。
"""

from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.providers.rerank.base import RerankProvider
from app.providers.rerank.noop import NoopRerankProvider

__all__ = [
    "PROVIDER_LLM",
    "PROVIDER_NONE",
    "PROVIDER_QWEN3",
    "RerankProvider",
    "NoopRerankProvider",
    "build_llm_rerank_provider",
    "configured_rerank_provider_name",
    "get_rerank_provider",
    "qwen_rerank_configured",
    "rerank_capability",
    "reset_rerank_provider_cache",
]

#: 三种实现的名字。它们同时是响应元数据 ``rerank.provider`` 与评测环境快照
#: ``rerank_provider`` 的取值——两处必须是同一份词表，否则「这一轮用的哪种重排」
#: 只能靠人肉对照。
PROVIDER_QWEN3 = "qwen3"
PROVIDER_LLM = "llm"
PROVIDER_NONE = "none"


def _rerank_api_key() -> str:
    """端点密钥：自己的优先，留空回落 ``MODEL_API_KEY``（同一把百炼 key 通常两处都通）。"""

    return settings.RERANK_API_KEY.strip() or settings.MODEL_API_KEY.strip()


def qwen_rerank_configured() -> bool:
    """这台机器上「专用重排端点」配没配齐：地址与密钥缺一不可。

    缺一个就等于没配：只有一个地址的重排每次都会 401，那比「明确不可用」难查得多。
    """

    return bool(settings.RERANK_BASE_URL.strip()) and bool(_rerank_api_key())


def rerank_capability() -> bool:
    """这台机器「真的能重排」吗：开关开着 **且** 端点配齐。

    这是**能力**信号，不是**意图**信号。档位说「要重排」只表达意图；回显里的
    ``tenant_policy.rerank`` 必须回答「这次真的会排吗」——端点没配齐时它必须是
    ``False``，否则面板说「开了」、日志里一次模型都没调（票 116 要消灭的正是这种假话）。
    """

    return settings.RERANK_ENABLED and qwen_rerank_configured()


#: 已装配过的 Provider，按「实现 + 端点指纹 + 超时 + 候选上限 + 截断长度」做键。
#:
#: 为什么必须缓存（一次实测）：改造前每次检索都现装一个 Provider（开启时还连带一个
#: 大模型），每一题都重做一次 TLS 握手。缓存里**只有 Provider**：模型与 HTTP 客户端按
#: 调用现取——客户端绑在事件循环上，缓存它等于把第一个 loop 的池子交给第二个 loop。
_PROVIDER_CACHE: dict[tuple[Any, ...], RerankProvider] = {}

#: 空实现的缓存键：与开启时的指纹键共用一张表，但永远不会相撞。
_NOOP_KEY: tuple[Any, ...] = ("noop",)


def reset_rerank_provider_cache() -> None:
    """清掉缓存。**只给测试用**：进程级缓存不该在用例之间串味。"""

    _PROVIDER_CACHE.clear()


def get_rerank_provider() -> RerankProvider:
    """生产工厂：关闭 / 端点没配都返回空实现，配齐才装专用端点。

    按配置指纹缓存：实现、地址、key、模型、超时、候选上限、截断长度——改任何一个都
    拿不到旧实例（那几种「面板上改了、实际没变」的故障里最难查的一类）。
    """

    if not rerank_capability():
        return _cached(_NOOP_KEY, NoopRerankProvider)

    key = (
        PROVIDER_QWEN3,
        settings.RERANK_BASE_URL,
        _rerank_api_key(),
        settings.RERANK_MODEL,
        settings.RERANK_TIMEOUT_SECONDS,
        settings.RERANK_MAX_CANDIDATES,
        settings.RERANK_CHUNK_MAX_CHARS,
    )
    return _cached(key, _build_qwen_rerank)


def _cached(key: tuple[Any, ...], build: Callable[[], RerankProvider]) -> RerankProvider:
    """取缓存；没有就装一个放进去。"""

    provider = _PROVIDER_CACHE.get(key)
    if provider is None:
        provider = build()
        _PROVIDER_CACHE[key] = provider
    return provider


def _build_qwen_rerank() -> RerankProvider:
    """装专用端点（惰性 import：关闭重排时不为一项没开的能力付 import 代价）。"""

    from app.providers.rerank.qwen import Qwen3RerankProvider

    return Qwen3RerankProvider(
        base_url=settings.RERANK_BASE_URL,
        api_key=_rerank_api_key(),
        model=settings.RERANK_MODEL,
        max_candidates=settings.RERANK_MAX_CANDIDATES,
        chunk_max_chars=settings.RERANK_CHUNK_MAX_CHARS,
        timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
    )


def build_llm_rerank_provider() -> RerankProvider:
    """**显式**装配一期的大模型重排（生产工厂不再返回它）。

    唯一用途是对照：评测用 ``--rerank-provider llm`` 让同一份题集跑旧实现，从而把
    「换件前后」的差异归因到**实现**而不是题集或语料。测试也用它（替身大模型注入在
    ``app.providers.llm.get_llm_provider`` 上，惰性 import 因此照样生效）。
    """

    from app.providers.llm import get_llm_provider
    from app.providers.rerank.llm import LLMRerankProvider

    return LLMRerankProvider(
        # 重排是延迟敏感的一步：固定用 fast **模型角色**（与查询改写同角色，
        # 父 spec「两档模型」里的快档）。角色 ≠ 套餐 ≠ 第 7 步的质量档位。
        llm=get_llm_provider("fast"),
        max_candidates=settings.RERANK_MAX_CANDIDATES,
        chunk_max_chars=settings.RERANK_CHUNK_MAX_CHARS,
        timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
    )


def configured_rerank_provider_name() -> str:
    """生产工厂这一次会装出哪种重排（``qwen3`` / ``none``）——**不实例化**任何 Provider。

    评测的环境快照要如实写「这一轮是哪种重排」，而快照是在跑题之前拍的；为了写一个
    名字去构造一个大模型（``build_llm_rerank_provider`` 会 ``init_chat_model``）是白花钱，
    也可能因为没 key 而改变路径。所以这里只回答「配置是否够装出专用端点」。
    """

    if settings.RERANK_ENABLED and qwen_rerank_configured():
        return PROVIDER_QWEN3
    return PROVIDER_NONE
