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


def get_rerank_provider() -> RerankProvider:
    """生产工厂：关闭 / 端点没配都返回空实现，配齐才装专用端点。"""

    if not settings.RERANK_ENABLED or not qwen_rerank_configured():
        return NoopRerankProvider()

    # 惰性 import：关闭重排时（缺省路径）不加载 httpx 与那个模块，离线启动不为一项
    # 没开的能力付出 import 代价。与关键词工厂对 ES 客户端用的是同一条约定。
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
