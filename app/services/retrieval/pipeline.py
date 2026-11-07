"""混合检索的编排：两路并行召回 → 融合 → 重排 → 降级判定。

它是 ADR-0011 边界图里「我们自写」的那一层：**知道发生了什么**（哪一路挂了、
这次是不是降级），但**不负责**把它翻译成 HTTP 响应——响应壳与业务码归
``app.services.rag_service``。

三条不许动的约定：

1. **两路真并行**：``asyncio.gather(..., return_exceptions=True)`` 让一路挂掉不取消
   另一路。向量分支是同步 psycopg + 同步 Session，必须 ``asyncio.to_thread`` 丢到
   独立线程，并且**持有自己的 ``SessionLocal()``**——否则它会卡住事件循环，
   「并行」只是字面上的（ADR-0011「它管不到的三件事」）。
2. **对称降级**：嵌入失败发生在向量分支内部，必须被该分支捕获后变成「仅关键词」，
   绝不冒泡成整单失败。只兜关键词那一半是本票最容易漏掉的一条。
3. **降级原因一因一码**：通道故障只用下面两个常量，重排失败用
   ``app/services/retrieval/rerank.py`` 里的 ``rerank failed``；一律不拼异常文本——
   调用方按码做判断，异常文案变了不该影响判断。可见性拒绝（20008）不属于「通道故障」，
   照旧抛出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.exceptions import AppError, ErrorCode
from app.db.session import SessionLocal
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.services.knowledge_base_access import ensure_visible
from app.services.retrieval.fusion import fuse_by_rrf
from app.services.retrieval.rerank import rerank_stage

logger = structlog.get_logger(__name__)

# 降级原因：一因一码。文案与 ADR-0011「一个查询的对照」里的示例一致。
REASON_VECTOR_FAILED = "vector search failed"
REASON_KEYWORD_FAILED = "keyword search failed"

# 这些错误是「拒绝」，不是「这一路挂了」：它们必须在召回阶段就把整单打回，
# 不能被降级吞掉——把越权当成故障降级，等于用另一半结果掩盖了一个安全事件。
_REJECTION_CODES = frozenset(
    {
        ErrorCode.KNOWLEDGE_BASE_NOT_FOUND,
        ErrorCode.FORBIDDEN,
        ErrorCode.UNAUTHORIZED,
    }
)


@dataclass(frozen=True)
class HybridOutcome:
    """一次混合检索的结果：切片 + 是否降级 + 降级原因。"""

    chunks: list[dict]
    degraded: bool
    degraded_reason: str | None


async def hybrid_retrieve(
    db: Any,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    keyword_factory: Callable[[], Any],
    recall_depth: int | None = None,
) -> HybridOutcome:
    """两路并行召回，融合后返回；一路失败不取消另一路。

    工厂以**可调用对象**传进来，而不是在这里直接 import：``rag_service`` 是组合根，
    测试替换工厂的位置因此仍然只有一处（沿用既有 monkeypatch 接缝，不新造接缝）。

    ``recall_depth`` 是**每路召回条数**，与 ``top_k``（最终返回条数）不是一回事：
    融合要先有足够宽的候选池，否则「两路共识」根本没机会显形。它由组合根从配置
    读出来传进来，这里不碰 settings——编排层不该知道配置从哪来。
    """

    depth = max(top_k, recall_depth or 0)

    gathered: list[Any] = list(
        await asyncio.gather(
            asyncio.to_thread(
                _run_vector_branch,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                query=query,
                top_k=depth,
                embedding_factory=embedding_factory,
                vector_factory=vector_factory,
            ),
            _keyword_branch(
                db,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                query=query,
                top_k=depth,
                keyword_factory=keyword_factory,
            ),
            return_exceptions=True,
        )
    )
    vector_result, keyword_result = gathered

    vector_rows, vector_error = _split(vector_result)
    keyword_rows, keyword_error = _split(keyword_result)
    _reraise_rejection(vector_error)
    _reraise_rejection(keyword_error)

    if vector_error is not None and keyword_error is not None:
        # 两路都挂才拒绝：此时没有任何可用结果，谎报「空」会让调用方以为换个问法就行。
        logger.warning(
            "hybrid retrieval failed on both channels",
            tenant_id=tenant_id,
            kb_ids=list(kb_ids),
            vector_error=type(vector_error).__name__,
            keyword_error=type(keyword_error).__name__,
        )
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "both retrieval channels failed")

    degraded_reason = None
    if vector_error is not None:
        degraded_reason = REASON_VECTOR_FAILED
    elif keyword_error is not None:
        degraded_reason = REASON_KEYWORD_FAILED

    if degraded_reason is not None:
        logger.warning(
            "hybrid retrieval degraded",
            tenant_id=tenant_id,
            kb_ids=list(kb_ids),
            degraded_reason=degraded_reason,
        )

    # 融合之后插入重排（GLOSSARY「检索链路」：召回 → 融合 → 重排）。失败只保留融合
    # 顺序并补一个原因；**已经降级就不覆盖**——先到的原因才是根因。
    fused_chunks = fuse_by_rrf(vector_rows, keyword_rows, top_n=top_k)
    ranked_chunks, rerank_reason = await rerank_stage(query=query, chunks=fused_chunks, top_n=top_k)
    if rerank_reason is not None:
        degraded_reason = degraded_reason or rerank_reason

    return HybridOutcome(
        chunks=ranked_chunks,
        degraded=degraded_reason is not None,
        degraded_reason=degraded_reason,
    )


def _split(result: Any) -> tuple[list[dict], BaseException | None]:
    """``gather(return_exceptions=True)`` 的返回值拆成「结果」或「错误」。

    ``return_exceptions=True`` 会把**任何** ``BaseException`` 都收成返回值，包括
    ``CancelledError`` / ``SystemExit`` / ``KeyboardInterrupt``。后三者不是「这一路
    挂了」，而是「这个进程/请求不该继续了」——把它们当降级材料吞掉，会让一个已经
    被取消的请求把剩下的活干完。所以只把 ``Exception`` 当故障，其余原样上抛。
    """

    if isinstance(result, BaseException):
        if not isinstance(result, Exception):
            raise result
        return [], result
    return list(result), None


def _reraise_rejection(error: BaseException | None) -> None:
    """可见性 / 权限类拒绝不参与降级：原样抛回给入口层。"""

    if isinstance(error, AppError) and error.code in _REJECTION_CODES:
        raise error


def _run_vector_branch(
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
) -> list[dict]:
    """独立线程里的向量分支入口：**自己开会话、自己关会话**。

    ``SessionLocal`` 在调用时从模块命名空间取，因此测试可以整体替换它而不必
    连一个真的 PostgreSQL。``asyncio.run`` 在这个工作线程里新建一个事件循环：
    主线程的事件循环正忙着服务其他请求，不能被这里 await。
    """

    db = SessionLocal()
    try:
        return asyncio.run(
            _vector_branch(
                db,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                query=query,
                top_k=top_k,
                embedding_factory=embedding_factory,
                vector_factory=vector_factory,
            )
        )
    finally:
        db.close()


async def _vector_branch(
    db: Any,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
) -> list[dict]:
    """向量通道：先向量化，再逐库校验可见性并召回（顺序与单模式一致）。"""

    # 嵌入失败发生在这里，也就是**向量分支内部**：它会被 gather 收成这一路的错误，
    # 于是混合模式降级成「仅关键词」，而不是整单失败。
    query_vector = await embedding_factory().embed_query(query)
    store = vector_factory()

    rows: list[dict] = []
    for kb_id in kb_ids:
        ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)
        for row in await store.similarity_search(
            db, query_vector, tenant_id=tenant_id, kb_id=kb_id, top_k=top_k
        ):
            # kb_id 由编排层补上：跨库结果必须能归属到某个库。
            rows.append({**row, "kb_id": kb_id})

    # 显式 tie-breaker：同分时按 kb_id / chunk_id 排，名次才不会随插入顺序漂移。
    rows.sort(key=lambda item: (-item.get("score", 0.0), item.get("kb_id") or "", item["chunk_id"]))
    return rows[:top_k]


async def _keyword_branch(
    db: Any,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    keyword_factory: Callable[[], Any],
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤；本身是异步 HTTP，不必进线程。"""

    # 可见性校验放在召回之前（ADR-0006），且与单模式保持同样的先后顺序：
    # 不可见的库先被拒绝，绝不会因为「引擎没配」而变成一个降级空结果。
    for kb_id in kb_ids:
        ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)

    provider = keyword_factory()
    if not provider.available:
        # 空实现是「能力不存在」，不是「查过了没命中」——混合模式下它是这一路失败。
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured")

    try:
        rows = await provider.keyword_search(
            query,
            tenant_id=tenant_id,
            index_versions={kb_id: DEFAULT_INDEX_VERSION for kb_id in kb_ids},
            top_k=top_k,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 引擎故障一律归为「这一路不可用」
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search failed") from exc

    return list(rows[:top_k])
