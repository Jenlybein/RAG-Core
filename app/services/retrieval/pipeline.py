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
   另一路。向量分支是**普通 ``await``**，跑在主循环上、用**请求的异步会话**——
   第 11 步票 #127 把 pgvector 查询换成了 ``await db.execute(...)``，于是这条分支不再
   卡住事件循环，「并行」不再需要另开线程。第 10 步那个固定规模的检索执行池
   （``app/core/loop_pool.py``，N 条常驻线程 + 常驻循环 + 排队预算）因此一并删除：
   它存在的唯一理由就是「同步查询会卡住循环」。
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

**取消要能传导**（#127）：两路都只是主循环上的协程，所以「客户端断开」会直接把
``CancelledError`` 送进正在 await 的那一段（嵌入 / 召回），活当场停下。执行池时代做不到
这件事——取消只能取消「等结果」，池里的线程会把手上的活干完。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import default_index_versions
from app.observability.stage_timing import (
    STAGE_FUSION,
    STAGE_KEYWORD_RECALL,
    STAGE_RERANK,
    STAGE_VECTOR_RECALL,
    StageTimer,
)
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
    db: AsyncSession,
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
    rerank_enabled: bool,
    rerank_factory: Callable[[], Any] | None = None,
    stage_timer: StageTimer | None = None,
) -> HybridOutcome:
    """两路并行召回，融合、重排后返回；一路失败不取消另一路。

    ``db`` 是**请求的异步会话**，两条分支共用它（只有向量一路真的读库）。异步会话不绑
    线程，所以「共用」在这里是安全的——第 10 步之所以要另开一个 ``SessionLocal``，是因为
    那时向量分支跑在别的线程上，而同步 ``Session`` 不能跨线程。

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

    ``rerank_enabled`` 是**这次调用要不要重排**，同样由组合根按已解析参数传入（#78：
    档位意图 ∧ 本机能力，合取在平台层算完）。它必须显式给：这台机器装了重排，不代表
    每一个档位都该用——``balanced`` 与 ``quality`` 在同一个部署上跑，两者的区别恰好
    就是这一步。靠 ``provider.available`` 一个信号判断，会把「本次不要」静默变成「本次跑了」。

    ``stage_timer`` 是可选的收集器（#102）：组合根传进来就记两路召回与融合的耗时。
    不传时本模块**自己造一个**——「度量常开」不该依赖调用方记得传：那会让「有人忘了传」
    悄悄变成「这次没有耗时」。造出来的那个没人收，效果等同于丢弃去向。

    ``rerank_factory`` 是重排的**注入缝**（#116）：缺省 ``None`` 时 ``rerank_stage``
    用既有工厂出口（``get_rerank_provider``），评测传一个别的工厂进来就能跑旧实现做
    对照。它和上面三个工厂同形——「重排用哪种实现」在这条链路上只有一个开关。
    """

    timer = stage_timer if stage_timer is not None else StageTimer()

    depth = max(top_k, recall_depth or 0)
    # 重排开着的时候，融合池必须宽于返回条数：只有 5 条候选时，重排只能原地挪位置，
    # 「重新挑」这件事根本无从发生。不开重排时这里就等于 top_k，行为不变。
    fuse_pool = max(top_k, rerank_candidates or 0)

    # 两路都只是主循环上的协程：向量一路用**请求的异步会话**，不再另开线程、另开会话。
    gathered: list[Any] = list(
        await asyncio.gather(
            _vector_branch(
                db,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                queries=vector_queries,
                top_k=depth,
                embedding_factory=embedding_factory,
                vector_factory=vector_factory,
                stage_timer=timer,
            ),
            _keyword_branch(
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                query=keyword_query,
                top_k=depth,
                keyword_factory=keyword_factory,
                stage_timer=timer,
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
    with timer.measure(STAGE_FUSION):
        fused_chunks = fuse_by_rrf(vector_rows, keyword_rows, top_n=fuse_pool)
    # 重排是**语义那一侧**的一步：它看到的是 ``effective_query``（改写成功时是改写句），
    # 扩展出来的同义词只该帮关键词一路做字面匹配，塞进重排提示词只会变成噪音。
    # ``rerank_enabled`` 来自本次的已解析参数：不要重排的档位在装了重排的机器上也不跑。
    reranked = await rerank_stage(
        query=rerank_query,
        chunks=fused_chunks,
        top_n=top_k,
        enabled=rerank_enabled,
        provider_factory=rerank_factory,
    )
    # 「重排发生没发生」只有一个答案：``RerankOutcome.duration_ms``。它为空就说明这一步
    # 根本没走（关闭 / 没装 / 没有候选），于是**不记录这一段**，而不是记一个 0。
    # 耗时也直接用它，不另量一次：两处数字不一样会立刻变成「哪个才是真的」的问题。
    if reranked.duration_ms is not None:
        timer.record(STAGE_RERANK, reranked.duration_ms)
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


async def _vector_branch(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: list[str],
    queries: Sequence[str],
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    stage_timer: StageTimer,
) -> list[dict]:
    """向量通道：每句各嵌一次、各做一次范围召回，合成**一个**池（#31）。

    这里没有「按库循环」：分库各召回一批再合并看着公平，实际让不相关的库也各占
    名额，把真正该进来的切片挤掉（父 spec「多库：一次全局召回 + 一次融合」）。
    嵌入不随知识库数量增加，只随 ``queries`` 的句子数增加（关闭改写时仍是 1 次）。

    会话是**请求那一个**（``db``）：异步会话不绑线程，所以不需要像同步时代那样在
    池里另开一个 ``SessionLocal()``。这也意味着取消能直接传导到这条分支上。

    嵌入失败发生在这里，也就是**向量分支内部**：它会被 gather 收成这一路的错误，
    于是混合模式降级成「仅关键词」，而不是整单失败。
    """

    with stage_timer.measure(STAGE_VECTOR_RECALL):
        return await vector_recall(
            db=db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            queries=queries,
            top_k=top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
            # 混合向量分支的历史排序口径：同分按 kb_id / chunk_id 兜底（回归红线）。
            single_query_order="tiebreak",
        )


async def _keyword_branch(
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    top_k: int,
    keyword_factory: Callable[[], Any],
    stage_timer: StageTimer,
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤；本身是异步 HTTP，不必进线程。

    可见性不在这里校验：由 ``rag_service.retrieve`` 在进入任何分支之前统一预校验
    一次（两条分支各查一遍 = 每库 2 次查询，且「拒绝」会有两个来源）。

    失败的那一路也留下这一段耗时：它确实被走过、也的确花了时间（哪怕只是拒绝服务），
    把失败从计量里抹掉会让「这一次为什么慢」少掉最该看的那一行。
    """

    with stage_timer.measure(STAGE_KEYWORD_RECALL):
        provider = keyword_factory()
        if not provider.available:
            # 空实现是「能力不存在」，不是「查过了没命中」——混合模式下它是这一路失败。
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured"
            )

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
