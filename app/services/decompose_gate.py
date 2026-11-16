"""问题分解后检索的**组合根**（`/rag/decompose`）。

第 13 步新增的数据面能力：一次分解（单次 ``default`` 模型调用）→ 对每条子查询各跑一次
检索内核 → 合并成一份交付单元列表。它与受控 Research 共用 Planner 的收敛规则与
``build_kernel_search`` 的内核装配，但**没有状态机**：不补证、不覆盖判定、不生成引用块，
交付的仍是切片。

「一次调用只做一次的五件事」与 `/rag/retrieve` 同构，全部留在外层：可见性校验、配额
记账、归属索引写入、观测提交各一次；子查询只复用内核，不各写一行（ADR-0028）。
归属索引的 ``chunk_ids`` 存合并后的最终交付单元，``index_versions`` / ``doc_versions``
仍是映射而不是平行数组。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.index_version import resolve_index_versions
from app.observability import emit_retrieval_trace_off_loop
from app.observability.stage_timing import StageTimer
from app.services import result_collapse
from app.services.decompose import merge_sub_query_results
from app.services.knowledge_base_access import ensure_visible
from app.services.research import (
    ResearchPlan,
    _PLANNER_SYSTEM,
    _coerce_plan,
    build_planner_prompt,
)
from app.services.research_gate import build_kernel_search
from app.services.retrieval_options import RetrievalOptions
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import (
    delivered_chunk_ids,
    delivered_doc_versions,
    tenant_fingerprint,
    trace_id_for,
)
from app.utils.id_generator import new_retrieval_log_id

logger = structlog.get_logger(__name__)

#: 一次分解检索注入的模型工厂（只给测试用；缺省走单一 ``default`` 角色）。
LLMFactory = Callable[[], Awaitable[Any]]


async def _default_llm_factory() -> Any:
    from app.providers.llm import get_llm_provider

    return await get_llm_provider()


async def decompose_retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    options: RetrievalOptions,
    llm_factory: LLMFactory | None = None,
) -> dict:
    """跑一次「分解 → N 次内核 → 合并」，返回与 `/rag/retrieve` 同形的响应数据。

    模型调用失败（规划阶段）→ 整单 ``50003``，与 `/rag/rewrite` / Research 一致。
    子检索自身的故障按内核既有规矩上抛或降级，不在这里二次包装。
    """

    timer = StageTimer()
    started_at = time.perf_counter()
    log_id = new_retrieval_log_id()
    trace_id = trace_id_for(log_id)

    visible_kbs = [
        await ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id) for kb_id in kb_ids
    ]
    index_versions = resolve_index_versions(visible_kbs)

    # ① 分解：复用 Research 的 Planner（同一份提示词与收敛规则），只取子查询文本。
    factory = llm_factory or _default_llm_factory
    try:
        provider = await factory()
        raw = await provider.chat_structured(
            ResearchPlan,
            system=_PLANNER_SYSTEM,
            user=build_planner_prompt(query=query),
        )
        points, planned_queries = _coerce_plan(raw)
    except Exception as exc:  # noqa: BLE001 - 统一翻成可重试的业务失败
        logger.warning(
            "decompose model call failed",
            tenant=tenant_fingerprint(tenant_id),
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.MODEL_CALL_FAILED) from exc

    sub_queries: list[str] = [item.query for item in planned_queries]
    point_texts: list[str] = [point.text for point in points]

    # ② 内核装配：三条查询串全等于子查询本身（分解出的查询不再二次改写）。
    # ``stage_timer`` 由本函数持有并交给每次内核调用：N 次内核的召回 / 融合 / 重排 / 折叠
    # 段**依次追加进同一份 trace**（同名段不覆盖），于是「哪一条子问题慢」在一条 trace 里
    # 就能看出来，而不是 N 份互不相干的观测（ADR-0028 §「子检索不单独发观测」）。
    degraded_reasons: list[str] = []
    search = build_kernel_search(
        db,
        tenant_id=tenant_id,
        index_versions=index_versions,
        options=options,
        on_degraded=degraded_reasons.append,
        stage_timer=timer,
    )

    # ③ 并行跑 N 次内核（保序：asyncio.gather 的结果顺序与入参一致）。
    import asyncio

    results: list[list[dict[str, Any]]] = list(
        await asyncio.gather(*(search(sub_query) for sub_query in sub_queries))
    )

    # ④ 合并：每条子问题保底 1 条 + 轮转填满 + 去重。
    merged = merge_sub_query_results(
        list(zip(sub_queries, results, strict=True)), top_k=options.top_k
    )

    # 降级原因只保留子检索自己的一条（先到的算根因），不额外发明新串。
    degraded_reason = degraded_reasons[0] if degraded_reasons else None

    # 空结果原因：与 /rag/retrieve 同一口径（换一个版本口径去数会把「没命中」说成「空库」）。
    from app.services.rag_service import _empty_reason

    empty_reason = await _empty_reason(
        db, tenant_id=tenant_id, index_versions=index_versions, chunks=merged
    )

    retrieval_meta: dict[str, object] = {
        "mode": options.mode,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "empty_reason": empty_reason,
        "rerank": None,  # 合并后跨子查询不可比，不宣称一次整体重排
        # 分解的可见事实：不记录它，评测里分不清「拆成了什么」。
        "decomposition": {
            "points": point_texts,
            "queries": sub_queries,
        },
    }

    # ⑤ 观测与归属索引：一次调用一份 trace、一行归属索引。
    await emit_retrieval_trace_off_loop(
        trace_id=trace_id,
        spans=timer.spans(),
        query=query,
        attributes={
            "mode": options.mode,
            "top_k": options.top_k,
            "decompose_queries": len(sub_queries),
            "degraded_reason": degraded_reason,
            "empty_reason": empty_reason,
            "tenant": tenant_fingerprint(tenant_id),
        },
    )
    await record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id=tenant_id,
        kb_ids=list(kb_ids),
        index_versions=dict(index_versions),
        doc_versions=delivered_doc_versions(merged),
        chunk_ids=delivered_chunk_ids(merged),
        top_k=options.top_k,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        degraded_reason=degraded_reason,
        empty_reason=empty_reason,
    )

    from app.services.retrieval_options import tenant_policy_echo

    metadata: dict[str, object] = {
        "mode": options.mode,
        "top_k": options.top_k,
        "vector_store": settings.VECTOR_STORE,
        "retrieval": retrieval_meta,
    }
    echo = tenant_policy_echo(options, kb_count=len(kb_ids))
    if echo is not None:
        metadata["tenant_policy"] = echo
    metadata["trace_id"] = trace_id

    return {
        "query": query,
        "kb_ids": list(kb_ids),
        "retrieved_chunks": merged,
        "metadata": metadata,
    }


__all__ = ["decompose_retrieve"]
