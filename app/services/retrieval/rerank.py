"""重排阶段：融合之后、组装之前（``recall → fuse → rerank``）。

它是 GLOSSARY「检索链路」里「重排」那一步的落地，也是父 spec 降级矩阵里
「重排失败 → 降级：保留融合顺序」这一行的实现。

本模块只做四件事：

1. **有候选、且重排开着，才调 Provider**：空结果与「这一步没开」都不是重排失败，
   也都不必花一次大模型调用；
2. **失败即降级**：抛错 / 空返回 / 结构不合法一律回到融合顺序，并给出原因
   ``rerank failed``——一因一码，绝不把异常文本拼进去（调用方按码做判断）；
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
from dataclasses import dataclass

import structlog

from app.providers.rerank import get_rerank_provider

logger = structlog.get_logger(__name__)

# 降级原因：一因一码。与通道故障的两个原因（pipeline 里的 ``REASON_*_FAILED``）
# 同一层级，文案写死，不随异常信息变化。
REASON_RERANK_FAILED = "rerank failed"


@dataclass(frozen=True)
class RerankOutcome:
    """一次重排尝试的全部事实：结果、失败原因、以及「发生了什么」。"""

    chunks: list[dict]
    reason: str | None
    applied: bool
    candidates: int
    returned: int
    duration_ms: float | None

    def as_metadata(self) -> dict[str, object]:
        """给响应 ``metadata.retrieval.rerank`` 用：只放可观测的事实，不放内容。"""

        return {
            "applied": self.applied,
            "candidates": self.candidates,
            "returned": self.returned,
            "duration_ms": self.duration_ms,
        }


async def rerank_stage(*, query: str, chunks: list[dict], top_n: int) -> RerankOutcome:
    """对融合后的候选重排一次；失败时原样返回融合顺序并给出原因。

    ``top_n`` 是**最终返回条数**（``top_k``），不是候选数：候选的封顶与单条截断在
    Provider 里（它才知道模型的上下文限制）。
    """

    if not chunks:
        # 没有候选：不是失败，也没有「重排发生」可言。
        return RerankOutcome([], None, False, 0, 0, None)

    provider = get_rerank_provider()
    if not provider.available:
        # 关闭重排 = 「没有这一步」，不是「排了一遍恰好没变」：不调用、不记耗时，
        # 融合顺序原样返回。日志与元数据里的「重排是否发生」才不会被误读。
        return RerankOutcome(list(chunks), None, False, len(chunks), len(chunks), None)

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
        )
        return RerankOutcome(list(chunks), REASON_RERANK_FAILED, False, len(chunks), 0, duration_ms)

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    ranked_chunks = list(ranked) if ranked else []
    if not ranked_chunks:
        # 空返回同样算失败：只回一条「重排没结果」比回半截结果更容易排查。
        logger.warning(
            "rerank returned nothing",
            candidates=len(chunks),
            duration_ms=duration_ms,
        )
        return RerankOutcome(list(chunks), REASON_RERANK_FAILED, False, len(chunks), 0, duration_ms)

    logger.info(
        "rerank applied",
        candidates=len(chunks),
        returned=len(ranked_chunks),
        duration_ms=duration_ms,
    )
    return RerankOutcome(ranked_chunks, None, True, len(chunks), len(ranked_chunks), duration_ms)
