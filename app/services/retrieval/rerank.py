"""重排阶段：融合之后、组装之前（``recall → fuse → rerank``）。

它是 GLOSSARY「检索链路」里「重排」那一步的落地，也是父 spec 降级矩阵里
「重排失败 → 降级：保留融合顺序」这一行的实现。

本模块只做四件事：

1. **有候选、且这次调用要重排，才调 Provider**：空结果、档位不要重排（#78）、本机
   没装重排，都不是重排失败，也都不必花一次大模型调用；
2. **失败即降级**：抛错 / 空返回 / 结构不合法一律回到融合顺序，并给出原因
   ``rerank failed``——一因一码，绝不把异常文本拼进去（调用方按码做判断）。
   降级回的是融合顺序的**前 ``top_n`` 条**，不是整个融合池：返回条数只由 ``top_k``
   决定，这条承诺在失败路径上同样成立；
3. **不吞取消信号**：``except Exception`` 只兜业务失败，``CancelledError`` /
   ``SystemExit`` 这类 ``BaseException`` 继续上抛。与 pipeline 里 ``_split`` 的
   理由完全相同：被取消的请求不该把剩下的活干完；
4. **把「重排到底发生了什么」带回去**：``RerankOutcome`` 里有是否发生、候选数、
   返回数与耗时。日志与响应元数据读的是同一份事实，不会出现「日志说排了、响应没痕迹」。

**是否覆盖已有的降级原因由编排层决定**——先到的原因才是根因（通道故障优先于重排
失败），这条判断留在 ``pipeline`` 里更显眼。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from app.providers.rerank import RerankProvider, get_rerank_provider

logger = structlog.get_logger(__name__)

# 降级原因：一因一码。与通道故障的两个原因（pipeline 里的 ``REASON_*_FAILED``）
# 同一层级，文案写死，不随异常信息变化。
REASON_RERANK_FAILED = "rerank failed"


@dataclass(frozen=True)
class RerankOutcome:
    """一次重排尝试的全部事实：结果、失败原因、以及「发生了什么」。

    ``provider`` / ``model`` 是**身份**（这次是哪种重排、哪个模型），不是状态：状态由
    ``applied`` 与 ``reason`` 表达。没跑重排时它们都是 ``None``——「没有实现可署名」，
    而不是编一个名字出来让人以为排过一遍。失败时仍署名：降级之后最该能回答的，就是
    「刚才试的是哪一种重排」。
    """

    chunks: list[dict]
    reason: str | None
    applied: bool
    candidates: int
    returned: int
    duration_ms: float | None
    provider: str | None = None
    model: str | None = None

    def as_metadata(self) -> dict[str, object]:
        """给响应 ``metadata.retrieval.rerank`` 用：只放可观测的事实，不放内容。"""

        return {
            "applied": self.applied,
            "candidates": self.candidates,
            "returned": self.returned,
            "duration_ms": self.duration_ms,
            "provider": self.provider,
            "model": self.model,
        }


async def rerank_stage(
    *,
    query: str,
    chunks: list[dict],
    top_n: int,
    enabled: bool,
    provider_factory: Callable[[], RerankProvider] | None = None,
) -> RerankOutcome:
    """对融合后的候选重排一次；失败时保留融合顺序并给出原因。

    ``top_n`` 是**重排吐出的条数（折叠前）**，不是候选数；**最终返回条数由 ``top_k``
    与折叠共同决定**——折叠开着时召回的深度是 ``top_k × OVERFETCH_FACTOR``，重排对这
    一整池排序后才会折叠回 ``top_k`` 条，所以 ``RerankOutcome.returned`` 记的往往大于
    ``top_k``，这是如实记录而不是多返回。候选的封顶与单条截断在 Provider 里（它才知道
    模型的上下文限制）。

    ``enabled`` 是**这次调用要不要重排**，由编排层按本次的已解析参数传入（#78：档位
    表达意图，部署开关表达「这台机器装没装」，两者的合取已经在平台层算完）。为什么
    不能只靠 ``provider.available``：``quality`` 档要重排、``balanced`` 档不要，而在同一
    台装了重排的机器上两者的 Provider 都是可用的——只问 Provider，就会让「不要重排」的
    那一档也白烧一次模型，而回显里写着 ``rerank: false``。

    **每一条返回路径都截到 ``top_n``**：开启重排时入参 ``chunks`` 是融合池
    （``RERANK_MAX_CANDIDATES``，默认 20），远宽于 ``top_k``。降级路径若原样返回它，
    ``top_k=5`` 的请求碰上重排失败就会拿回 20 条——「返回条数由 top_k 决定」当场失效。
    截断放在这里而不是只放在 Provider 里，是因为失败 / 未开启这两条路径根本不经过
    Provider。

    ``provider_factory`` 是**注入缝**（票 #116）：缺省仍是 ``get_rerank_provider``
    （既有 monkeypatch 接缝，一行不用改），评测可以显式装配旧的大模型重排做对照。
    工厂以可调用对象传进来而不是在这里直接 import，与 ``hybrid_retrieve`` 里几个
    工厂同形——替换点因此仍然只有一处。
    """

    if not chunks:
        # 没有候选：不是失败，也没有「重排发生」可言。
        return RerankOutcome([], None, False, 0, 0, None)

    if not enabled:
        # 这次调用不含重排 = 「没有这一步」，与「本机没装重排」是同一件事的两面：
        # 不装配 Provider、不为它花时间，融合顺序原样返回（截到 top_n）。
        kept = list(chunks)[:top_n]
        return RerankOutcome(kept, None, False, len(chunks), len(kept), None)

    # 缺省走模块级名字（不是默认参数）：既有用例 monkeypatch 的就是这个名字，默认参数
    # 会在 import 期把旧函数对象焊死在签名里，那条接缝就失效了。
    factory = get_rerank_provider if provider_factory is None else provider_factory
    provider = factory()
    if not provider.available:
        # 关闭重排 = 「没有这一步」，不是「排了一遍恰好没变」：不调用、不记耗时，
        # 融合顺序原样返回（截到 top_n）。日志与元数据里的「重排是否发生」才不会被误读。
        kept = list(chunks)[:top_n]
        return RerankOutcome(kept, None, False, len(chunks), len(kept), None)

    started = time.perf_counter()
    try:
        ranked = await provider.rerank(query=query, chunks=list(chunks), top_n=top_n)
    except Exception as exc:  # noqa: BLE001 - 重排失败必须降级，绝不冒泡成整单失败
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "rerank failed",
            candidates=len(chunks),
            duration_ms=duration_ms,
            # 只记异常类型，不记原文：异常文案可能夹带提示词或切片内容。
            error=type(exc).__name__,
            provider=provider.provider_name,
        )
        return RerankOutcome(
            list(chunks)[:top_n],
            REASON_RERANK_FAILED,
            False,
            len(chunks),
            0,
            duration_ms,
            provider=provider.provider_name,
            model=provider.model_name,
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    ranked_chunks = list(ranked)[:top_n] if ranked else []
    if not ranked_chunks:
        # 空返回同样算失败：只回一条「重排没结果」比回半截结果更容易排查。
        logger.warning(
            "rerank returned nothing",
            candidates=len(chunks),
            duration_ms=duration_ms,
            provider=provider.provider_name,
        )
        return RerankOutcome(
            list(chunks)[:top_n],
            REASON_RERANK_FAILED,
            False,
            len(chunks),
            0,
            duration_ms,
            provider=provider.provider_name,
            model=provider.model_name,
        )

    logger.info(
        "rerank applied",
        candidates=len(chunks),
        returned=len(ranked_chunks),
        duration_ms=duration_ms,
        provider=provider.provider_name,
        model=provider.model_name,
    )
    return RerankOutcome(
        ranked_chunks,
        None,
        True,
        len(chunks),
        len(ranked_chunks),
        duration_ms,
        provider=provider.provider_name,
        model=provider.model_name,
    )
