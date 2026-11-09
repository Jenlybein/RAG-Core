"""混合检索的编排：两路并行召回 → 融合 → 重排 → 降级判定。

它是 ADR-0011 边界图里「我们自写」的那一层：**知道发生了什么**（哪一路挂了、
这次是不是降级），但**不负责**把它翻译成 HTTP 响应——响应壳与业务码归
``app.services.rag_service``。

**两条通道拿不同的查询**（票 6 / #31）：查询优化在召回之前把用户那句话加工成
``vector_queries``（向量一路逐句都搜；改写成功时是原句 + 改写句）、``effective_query``
（给重排）与 ``search_query``（给关键词一路）。三者不同是设计而不是 bug——同义词补的
是字面匹配，改写补的是语义；合成一句等于用其中一路的优化去污染另一路。两个开关都关
时 ``vector_queries`` 只有原句，本模块的行为与票 4 逐字段一致。

四条不许动的约定：

1. **两路真并行**：``asyncio.gather(..., return_exceptions=True)`` 让一路挂掉不取消
   另一路。向量分支是同步 psycopg + 同步 Session，必须 ``asyncio.to_thread`` 丢到
   独立线程，并且**持有自己的 ``SessionLocal()``**——否则它会卡住事件循环，
   「并行」只是字面上的（ADR-0011「它管不到的三件事」）。
2. **对称降级**：嵌入失败（含多查询里任意一句）发生在向量分支内部，必须被该分支
   捕获后变成「仅关键词」，绝不冒泡成整单失败。只兜关键词那一半是本票最容易漏掉的一条。
3. **降级原因一因一码**：通道故障只用下面两个常量，重排失败用
   ``app/services/retrieval/rerank.py`` 里的 ``rerank failed``；一律不拼异常文本——
   调用方按码做判断，异常文案变了不该影响判断。可见性拒绝（20008）不属于「通道故障」，
   照旧抛出。
4. **多库 = 每个查询一次全局召回**：关键词一路查一次；向量一路按 ``vector_queries``
   逐句各查一次，每次都覆盖全部「库 + 索引版本」配对，再并成**一个**池进既有 RRF
   （#31；池内去重与取优在 ``vector_recall.py``，**不是**再嵌一层融合）。
   按库循环召回再合并是假均衡——不相关的库也各占名额，把真正该进来的切片挤掉。
   可见性校验不在这里：它在 ``rag_service.retrieve`` 进入任何分支之前统一做一遍
   （票 25），两条分支内不再重复。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.exceptions import AppError, ErrorCode
from app.db.session import SessionLocal
from app.models.chunk import default_index_versions
from app.services.retrieval.fusion import fuse_by_rrf
from app.services.retrieval.rerank import rerank_stage
from app.services.retrieval.vector_recall import vector_recall

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
    """一次混合检索的结果：切片 + 是否降级 + 降级原因 + 重排事实。

    ``rerank`` 是 ``metadata.retrieval.rerank`` 的那一份（是否发生、候选数、返回数、
    耗时）。它和日志读的是同一个 ``RerankOutcome``，所以不会出现「日志说排了、
    响应里没痕迹」。
    """

    chunks: list[dict]
    degraded: bool
    degraded_reason: str | None
    rerank: dict[str, object] | None = None


async def hybrid_retrieve(
    *,
    tenant_id: str,
    kb_ids: list[str],
    vector_queries: Sequence[str],
    rerank_query: str,
    keyword_query: str,
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    keyword_factory: Callable[[], Any],
    recall_depth: int | None = None,
    rerank_candidates: int | None = None,
) -> HybridOutcome:
    """两路并行召回，融合、重排后返回；一路失败不取消另一路。

    工厂以**可调用对象**传进来，而不是在这里直接 import：``rag_service`` 是组合根，
    测试替换工厂的位置因此仍然只有一处（沿用既有 monkeypatch 接缝，不新造接缝）。

    **两条通道拿不同的查询**（``vector_queries`` / ``keyword_query``）：查询优化在
    召回之前就把一句话加工成「向量逐句列表 + 关键词一句 + 重排一句」。改写成功时
    ``vector_queries`` 是原句与改写句两句，两句各自召回并成一个池（#31）；关掉开关时
    它是单元素序列，行为与票 4 逐字节一致。重排仍只看 ``rerank_query``（``effective_query``：
    「用户最终想问什么」），不看召回用的两句。

    为什么不留 ``vector_query: str`` 的单数别名：唯一调用方是组合根，双形态只会把
    「这次到底是替换还是并集」变成调用方的隐式选择——那不会报错，只会静默地把
    一半优化关掉。

    ``recall_depth`` 是**每路召回条数**，``rerank_candidates`` 是**融合后保留的
    候选数**，两者都与 ``top_k``（最终返回条数）不是一回事：召回要宽、融合池要宽，
    「两路共识」与「重排挑得动」才有机会发生。它们由组合根从配置读出来传进来，
    这里不碰 settings——编排层不该知道配置从哪来。
    """

    depth = max(top_k, recall_depth or 0)
    # 重排开着的时候，融合池必须宽于返回条数：只有 5 条候选时，重排只能原地挪位置，
    # 「重新挑」这件事根本无从发生。不开重排时这里就等于 top_k，行为不变。
    fuse_pool = max(top_k, rerank_candidates or 0)

    gathered: list[Any] = list(
        await asyncio.gather(
            asyncio.to_thread(
                _run_vector_branch,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                queries=vector_queries,
                top_k=depth,
                embedding_factory=embedding_factory,
                vector_factory=vector_factory,
            ),
            _keyword_branch(
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                query=keyword_query,
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
    # 合成后的向量池直接作为「向量一路」进既有的 ``fuse_by_rrf``，不新增内层融合。
    fused_chunks = fuse_by_rrf(vector_rows, keyword_rows, top_n=fuse_pool)
    # 重排是**语义那一侧**的一步：它看到的是 ``effective_query``（改写成功时是改写句），
    # 扩展出来的同义词只该帮关键词一路做字面匹配，塞进重排提示词只会变成噪音。
    reranked = await rerank_stage(query=rerank_query, chunks=fused_chunks, top_n=top_k)
    if reranked.reason is not None:
        degraded_reason = degraded_reason or reranked.reason

    return HybridOutcome(
        chunks=reranked.chunks,
        degraded=degraded_reason is not None,
        degraded_reason=degraded_reason,
        rerank=reranked.as_metadata(),
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
    queries: Sequence[str],
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
                queries=queries,
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
    queries: Sequence[str],
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
) -> list[dict]:
    """向量通道：每句各嵌一次、各做一次范围召回，合成**一个**池（#31）。

    这里没有「按库循环」：分库各召回一批再合并看着公平，实际让不相关的库也各占
    名额，把真正该进来的切片挤掉（父 spec「多库：一次全局召回 + 一次融合」）。
    嵌入不随知识库数量增加，只随 ``queries`` 的句子数增加（关闭改写时仍是 1 次）。

    嵌入失败发生在这里，也就是**向量分支内部**：它会被 gather 收成这一路的错误，
    于是混合模式降级成「仅关键词」，而不是整单失败。
    """

    return await vector_recall(
        db=db,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        queries=queries,
        top_k=top_k,
        embedding_factory=embedding_factory,
        vector_factory=vector_factory,
    )


async def _keyword_branch(
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    keyword_factory: Callable[[], Any],
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤；本身是异步 HTTP，不必进线程。

    可见性不在这里校验：由 ``rag_service.retrieve`` 在进入任何分支之前统一预校验
    一次（两条分支各查一遍 = 每库 2 次查询，且「拒绝」会有两个来源）。
    """

    provider = keyword_factory()
    if not provider.available:
        # 空实现是「能力不存在」，不是「查过了没命中」——混合模式下它是这一路失败。
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured")

    try:
        rows = await provider.keyword_search(
            query,
            tenant_id=tenant_id,
            index_versions=default_index_versions(kb_ids),
            top_k=top_k,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 引擎故障一律归为「这一路不可用」
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search failed") from exc

    return list(rows[:top_k])
