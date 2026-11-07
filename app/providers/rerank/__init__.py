"""重排能力出口：抽象 + 按 ``RERANK_ENABLED`` 选择的工厂。

默认关闭：关闭时返回既有空实现，默认路径（离线测试 / CI）**一次大模型都不调**。
开启后装 ``LLMRerankProvider``——一期它包着 ``LLMProvider``，自己不建 HTTP 客户端
（ADR-0011：Rerank 是我们自写、但复用大模型能力的那一类 Provider）。

用开关而不是两套分支：同一份代码在不同机器上表现为「有没有重排能力」，业务编排
不需要知道今天是哪一种——它只知道「调重排，失败就降级」。
"""

from app.core.config import settings
from app.providers.rerank.base import RerankProvider
from app.providers.rerank.noop import NoopRerankProvider

__all__ = ["RerankProvider", "NoopRerankProvider", "get_rerank_provider"]


def get_rerank_provider() -> RerankProvider:
    if not settings.RERANK_ENABLED:
        return NoopRerankProvider()

    # 惰性 import：关闭重排时（缺省路径）不加载大模型层，离线启动不为一项没开的
    # 能力付出 import 代价。与关键词工厂对 ES 客户端用的是同一条约定。
    from app.providers.llm import get_llm_provider
    from app.providers.rerank.llm import LLMRerankProvider

    return LLMRerankProvider(
        # 重排是延迟敏感的一步：固定用 fast **模型角色**（与查询改写同角色，
        # 父 spec「两档模型」里的快档）。角色 ≠ 套餐 ≠ 第 5 步的质量档位。
        llm=get_llm_provider("fast"),
        max_candidates=settings.RERANK_MAX_CANDIDATES,
        chunk_max_chars=settings.RERANK_CHUNK_MAX_CHARS,
        timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
    )
