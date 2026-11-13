"""检索内核：一次检索的六步（ADR-0028）。

```text
召回（两路并行）→ 融合 →（图谱注入位）→ 重排 → TopK → 折叠
```

**它是「每条子查询各做一次」的那一段**，所以受控 Research 会逐条复用同一份实现：
一条子查询 = 一次内核调用。判据只有一句（ADR-0028 §3）——「这一步是每条子查询都要各做
一次，还是每次调用只应做一次」？前者进内核，后者留在外层。于是这五件事一件都不在这里：

* **查询改写**（外层，进入内核之前；Research 里由 Planner 代替）；
* **可见性校验**（入口一次）；
* **配额记账**（外层一次）；
* **归属索引写入**（一次调用一行）；
* **观测提交**（一份 trace，子检索在里面以阶段出现）。

它们被塞进内核的后果一样且都不报错：改写把已分解的子查询改坏、归属索引写 N 行让「一次
Research」在统计里消失、trace 碎成 N 份让阶段预算失准、配额扣 N 次。

**内核吃已解析好的上下文，不吃请求**：租户、``kb_id → version`` 范围、原句与三条查询串
（向量逐句 / 重排 / 关键词）、条数与深度、重排开关与工厂、注入的 Provider 工厂、计时器。
它不读请求体、不解析密钥、**不碰 ``settings``**——那两个服务端旋钮（``HYBRID_RECALL_TOP_K``
/ ``RERANK_MAX_CANDIDATES``）由组合根读出来当参数递进来，范围只有一个来源。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_KEYWORD_RECALL,
    STAGE_VECTOR_RECALL,
    StageTimer,
)
from app.services.graph_injection import GraphInjection, InjectionOutcome
from app.services.retrieval.collapse import collapse_results
from app.services.retrieval.pipeline import hybrid_retrieve
from app.services.retrieval.vector_recall import vector_recall
from app.services.retrieval_options import MODE_BM25, MODE_HYBRID
from app.utils.retrieval_fields import with_score_columns

logger = structlog.get_logger(__name__)

#: 图谱注入位（ADR-0027 / #144）：融合之后、重排之前拿到候选池与本次范围。
#: 类型与常量住在 :mod:`app.services.graph_injection`（上限、优先级、``score = 0``
#: 都在那里），这里只留这个别名——内核只认「一个能吃候选池、回候选池的可调用对象」，
#: 不认识边、节、代表片。默认 ``None`` 即「这一步不存在」，链路与注入之前逐字节一致。


@dataclass(frozen=True)
class KernelOutcome:
    """内核交回的三样事实：交付单元、降级原因、重排事实。

    **不含**空结果原因、``metadata``、``trace_id``、耗时去向与归属索引行——那几件由外层
    各算一次（ADR-0028 §2），而 ``trace_id`` 更是要在任何一次写库尝试之前就有值。

    ``degraded_reason`` 只覆盖内核自己这六步（通道故障 / 重排失败 / 图谱注入失败）。改写失败
    发生在外层而且更早，两条原因谁先到谁是根因——那条合成也在外层做（一因一码，先到的
    才算根因）。

    ``graph`` 是注入那一步的事实（注入条数、种子数、来源分布；``None`` = 这一步不存在）。
    它在这里而不是在 ``metadata`` 里被拼成响应字段：内核只交事实，措辞归外层。
    """

    chunks: list[dict]
    degraded_reason: str | None = None
    rerank: dict[str, object] | None = None
    graph: InjectionOutcome | None = None


async def retrieve_kernel(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    query: str,
    mode: str,
    vector_queries: Sequence[str],
    rerank_query: str,
    keyword_query: str,
    top_k: int,
    recall_top_k: int,
    rerank_enabled: bool,
    fold_to_sections: bool,
    expand_table_rows: bool,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    keyword_factory: Callable[[], Any],
    recall_depth: int | None = None,
    rerank_candidates: int | None = None,
    rerank_factory: Callable[[], Awaitable[Any]] | None = None,
    stage_timer: StageTimer | None = None,
    graph_injection: GraphInjection | None = None,
) -> KernelOutcome:
    """跑一遍「召回 → 融合 → 重排 → TopK → 折叠」，只返回这一步能看到的事实。

    ``index_versions`` 是**本次的范围**（``kb_id → 索引版本``的映射，不是两个平行数组）：
    它由调用方解析好递进来，内核自己不去查「这个库现在生效哪个版本」——那是每次调用
    一次的事（ADR-0025），而内核会被调 N 次。

    ``vector_queries`` / ``rerank_query`` / ``keyword_query`` 是**外层查询优化**的产物：
    向量一路逐句都搜、重排只看「用户最终想问什么」、关键词一路拿扩展后的那一句。三者
    不同是设计而不是 bug，内核原样使用、不再做第二次加工（Research 里再改写一遍会把
    已分解的子查询揉回原问题）。

    ``top_k`` 是**最终返回条数**，``recall_top_k`` 是**召回条数**（折叠开着时已经含了
    多取倍数），``recall_depth`` 是混合模式每路召回深度的下限。三者都与「融合池宽度」
    （``rerank_candidates``）不是一回事：召回要宽、融合池要宽，「两路共识」与「重排挑得
    动」才有机会发生。所有深度都由调用方给，内核不读配置。

    ``query`` 是本次的**原句**。上面那三条查询串已经各自加工好了，六步本身都不消费它；
    收它是为了让内核认得「这次检索在回答什么」——注入位（#152）与将来的降级日志都要这一句
    原文，而内核不接请求体，只能由调用方递进来。

    ``graph_injection`` 是**注入位**（ADR-0027）：候选池成形之后、截断 TopK 之前。
    三种模式各经过它一次——混合模式在 ``hybrid_retrieve`` 内部那一步（融合与重排之间），
    单模式在召回之后、折叠之前；两种位置是同一个语义位置，所以注入项的待遇完全一样
    （``score = 0``、来源标记、是否入选交给名次）。默认 ``None`` = 这一步不存在。

    ``stage_timer`` 是可选的收集器：不传就自己造一个。「度量常开」不该依赖调用方记得传
    ——那会让「有人忘了传」悄悄变成「这次没有耗时」（与 ``hybrid_retrieve`` 同一处理）。
    """

    timer = stage_timer if stage_timer is not None else StageTimer()

    degraded_reason: str | None = None
    rerank_info: dict[str, object] | None = None
    graph: InjectionOutcome | None = None

    # 未知模式到不了这里：组合根在可见性校验与改写之前就把它拒了（拒绝先于召回，
    # 被拒的请求不该白烧一次改写）。这里因此只有三条分支，不重复一遍词表校验——
    # 同一个拒绝有两个来源，查起来第一件事就是「到底是哪一道拒的」。
    if mode == MODE_HYBRID:
        outcome = await hybrid_retrieve(
            db=db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            # 向量一路逐句都搜；重排仍看 rerank_query；关键词一路拿扩展后的句子。
            vector_queries=vector_queries,
            rerank_query=rerank_query,
            keyword_query=keyword_query,
            top_k=recall_top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
            keyword_factory=keyword_factory,
            recall_depth=recall_depth,
            rerank_candidates=rerank_candidates,
            # 「这次要不要重排」由本次的已解析参数说了算：装了重排的机器上，不要重排的
            # 那一档也不该被顺手排一遍。
            rerank_enabled=rerank_enabled,
            rerank_factory=rerank_factory,
            stage_timer=timer,
            # 注入位递进 pipeline：融合与重排都在它里面，那个「之间」的位置只有它知道。
            graph_injection=graph_injection,
        )
        chunks = outcome.chunks
        degraded_reason = outcome.degraded_reason
        rerank_info = outcome.rerank
        graph = outcome.graph
    elif mode == MODE_BM25:
        # 单模式不降级：关键词引擎没配或调用失败一律 50002，绝不偷偷退回向量——
        # 调用方选 bm25 往往就是在评测单路效果。
        chunks = await _retrieve_by_keyword(
            db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            query=keyword_query,
            top_k=recall_top_k,
            keyword_factory=keyword_factory,
            stage_timer=timer,
        )
    else:  # 向量单模式——剩下的唯一可能
        chunks = await _retrieve_by_vector(
            db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            queries=vector_queries,
            top_k=recall_top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
            stage_timer=timer,
        )

    # 单模式（vector / bm25）：候选池在这里已经成形，截断 TopK 在折叠那一步之前——
    # 注入正好落在两者之间，与混合模式里那个位置是同一个语义位置。混合模式不在这里
    # 再调一次：它已经在 pipeline 内部注过了（调两次会让重排看到两份同样的候选）。
    if mode != MODE_HYBRID and graph_injection is not None:
        graph = await graph_injection(chunks, index_versions)
        chunks = graph.chunks
        degraded_reason = degraded_reason or graph.degraded_reason

    with timer.measure(STAGE_COLLAPSE):
        collapsed = await collapse_results(
            db,
            tenant_id=tenant_id,
            chunks=chunks,
            index_versions=index_versions,
            top_k=top_k,
            fold_to_sections=fold_to_sections,
            expand_table_rows=expand_table_rows,
        )

    return KernelOutcome(
        chunks=collapsed, degraded_reason=degraded_reason, rerank=rerank_info, graph=graph
    )


async def _retrieve_by_vector(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    queries: Sequence[str],
    top_k: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    stage_timer: StageTimer,
) -> list[dict]:
    """向量通道：逐句一次范围召回覆盖全部配对，**不按库循环、不按库分名额**。

    多句时先并成一个池（``vector_recall`` 里去重取优）；单句（改写关闭 / 失败）时
    走原来的快路径，SQL 语义、排序口径与打分全部与票 4 一致（回归红线）。
    """

    with stage_timer.measure(STAGE_VECTOR_RECALL):
        rows = await vector_recall(
            db=db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            queries=queries,
            top_k=top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
        )

    # 单查询时这一步是稳定排序（``vector_recall`` 已按同一口径排过），逐字节不变。
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return [
        # 五列契约字段统一由 ``app.utils.retrieval_fields`` 补齐：单模式下只有本通道
        # 那一路是实值，另一路是 ``None``（不是 0），融合分为 ``None``（没有融合）。
        with_score_columns(row, vector_score=row.get("score"), vector_rank=rank)
        for rank, row in enumerate(rows[:top_k], start=1)
    ]


async def _retrieve_by_keyword(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    query: str,
    top_k: int,
    keyword_factory: Callable[[], Any],
    stage_timer: StageTimer,
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤，纯关键词打分。

    可见性不在这里校验：调用方已经在进入内核之前统一校验过一遍（ADR-0006 的先后顺序
    不变：不可见的库先被拒绝）。关键词引擎的可用性检查仍在这里做——空实现是「能力不存在」，
    不是「查过了没命中」。
    """

    with stage_timer.measure(STAGE_KEYWORD_RECALL):
        try:
            provider = keyword_factory()
            if not provider.available:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured"
                )
            # 预算由 Provider 自己兜住（`ElasticsearchKeywordSearchProvider.timeout_s`）：
            # 关键词通道有单通道与混合两份调用点，写在这里就得写两遍，写两遍必漂移。
            rows = await provider.keyword_search(
                query,
                tenant_id=tenant_id,
                index_versions=dict(index_versions),
                top_k=top_k,
            )
        except AppError:
            raise
        except Exception as exc:
            # 引擎故障属于「这一路不可用」，不是整单 500：错误分类固定为 50002。
            # 日志只记类型与范围，不记检索原文。
            logger.warning(
                "keyword search failed",
                tenant_id=tenant_id,
                kb_ids=list(index_versions),
                error=type(exc).__name__,
            )
            raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search failed") from exc

    return [
        with_score_columns(
            {
                "document_id": row["document_id"],
                "chunk_id": row["chunk_id"],
                "title": row.get("title", ""),
                "content": row.get("content", ""),
                "kb_id": row.get("kb_id"),
                "score": row.get("score", 0.0),
            },
            bm25_score=row.get("score", 0.0),
            bm25_rank=rank,
        )
        for rank, row in enumerate(rows[:top_k], start=1)
    ]


__all__ = ["GraphInjection", "KernelOutcome", "retrieve_kernel"]
