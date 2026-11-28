"""检索内核：一次检索的五步（ADR-0028）。

```text
召回（两路并行）→ 融合 → 重排 → TopK → 折叠
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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_KEYWORD_RECALL,
    STAGE_VECTOR_RECALL,
    StageTimer,
)
from app.services.retrieval.collapse import collapse_results
from app.services.retrieval.keyword_recall import keyword_recall
from app.services.retrieval.pipeline import hybrid_retrieve
from app.services.retrieval.vector_recall import vector_recall
from app.services.retrieval_options import MODE_BM25, MODE_HYBRID
from app.utils.retrieval_fields import with_score_columns


@dataclass(frozen=True)
class KernelOutcome:
    """内核交回的三样事实：交付单元、降级原因、重排事实。

    **不含**空结果原因、``metadata``、``trace_id``、耗时去向与归属索引行——那几件由外层
    各算一次（ADR-0028 §2），而 ``trace_id`` 更是要在任何一次写库尝试之前就有值。

    ``degraded_reason`` 只覆盖内核自己这五步（通道故障 / 重排失败）。改写失败
    发生在外层而且更早，两条原因谁先到谁是根因——那条合成也在外层做（一因一码，先到的
    才算根因）。
    """

    chunks: list[dict]
    degraded_reason: str | None = None
    rerank: dict[str, object] | None = None


async def retrieve_kernel(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: Sequence[str],
    query: str,
    mode: str,
    recall_queries: Sequence[str],
    rerank_query: str,
    top_k: int,
    recall_top_k: int,
    rerank_enabled: bool,
    #: 重排本该跑、但重排额度用尽（第 13 步）：跳过并把原因记进本次结果。
    #: 与 ``rerank_enabled=False`` 的区别只在原因串——「跳过」与「不要」必须分得开。
    rerank_quota_exhausted: bool = False,
    fold_to_sections: bool,
    expand_table_rows: bool,
    #: 一条结果正文的硬上限（第 16 步 #207）：折叠时按它从两端收缩。
    max_unit_chars: int,
    embedding_factory: Callable[[], Any],
    vector_factory: Callable[[], Any],
    keyword_factory: Callable[[], Any],
    recall_depth: int | None = None,
    rerank_candidates: int | None = None,
    rerank_factory: Callable[[], Awaitable[Any]] | None = None,
    stage_timer: StageTimer | None = None,
) -> KernelOutcome:
    """跑一遍「召回 → 融合 → 重排 → TopK → 折叠」，只返回这一步能看到的事实。

    ``kb_ids`` 是**本次的范围**（``kb_id → 索引版本``的映射，不是两个平行数组）：
    它由调用方解析好递进来，内核自己不去查「这个库现在生效哪个版本」——那是每次调用
    一次的事（ADR-0025），而内核会被调 N 次。

    ``recall_queries`` / ``rerank_query`` 是**外层查询优化**的产物：**两路召回都逐句搜**
    这一组（原句 + 改写句，第 15 步起两路对称）、重排只看 ``rerank_query``（原句，
    重排与改写无关）。内核原样使用、不再做第二次加工（Research 里再改写一遍会把
    已分解的子查询揉回原问题）。

    ``top_k`` 是**最终返回条数**，``recall_top_k`` 是**召回条数**（折叠开着时已经含了
    多取倍数），``recall_depth`` 是混合模式每路召回深度的下限。三者都与「融合池宽度」
    （``rerank_candidates``）不是一回事：召回要宽、融合池要宽，「两路共识」与「重排挑得
    动」才有机会发生。所有深度都由调用方给，内核不读配置。

    ``query`` 是本次的**原句**。上面那三条查询串已经各自加工好了，五步本身都不消费它；
    收它是为了让内核认得「这次检索在回答什么」——将来的降级日志要这一句原文，而内核不接
    请求体，只能由调用方递进来。

    ``stage_timer`` 是可选的收集器：不传就自己造一个。「度量常开」不该依赖调用方记得传
    ——那会让「有人忘了传」悄悄变成「这次没有耗时」（与 ``hybrid_retrieve`` 同一处理）。
    """

    timer = stage_timer if stage_timer is not None else StageTimer()

    degraded_reason: str | None = None
    rerank_info: dict[str, object] | None = None

    # 未知模式到不了这里：组合根在可见性校验与改写之前就把它拒了（拒绝先于召回，
    # 被拒的请求不该白烧一次改写）。这里因此只有三条分支，不重复一遍词表校验——
    # 同一个拒绝有两个来源，查起来第一件事就是「到底是哪一道拒的」。
    if mode == MODE_HYBRID:
        outcome = await hybrid_retrieve(
            db=db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            # 两路都逐句搜这一组；重排看 rerank_query（原句）。
            recall_queries=recall_queries,
            rerank_query=rerank_query,
            top_k=recall_top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
            keyword_factory=keyword_factory,
            recall_depth=recall_depth,
            rerank_candidates=rerank_candidates,
            # 「这次要不要重排」由本次的已解析参数说了算：装了重排的机器上，不要重排的
            # 那一档也不该被顺手排一遍。
            rerank_enabled=rerank_enabled,
            rerank_quota_exhausted=rerank_quota_exhausted,
            rerank_factory=rerank_factory,
            stage_timer=timer,
        )
        chunks = outcome.chunks
        degraded_reason = outcome.degraded_reason
        rerank_info = outcome.rerank
    elif mode == MODE_BM25:
        # 单模式不降级：关键词引擎没配或调用失败一律 50002，绝不偷偷退回向量——
        # 调用方选 bm25 往往就是在评测单路效果。
        chunks = await _retrieve_by_keyword(
            db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            queries=recall_queries,
            top_k=recall_top_k,
            keyword_factory=keyword_factory,
            stage_timer=timer,
        )
    else:  # 向量单模式——剩下的唯一可能
        chunks = await _retrieve_by_vector(
            db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            queries=recall_queries,
            top_k=recall_top_k,
            embedding_factory=embedding_factory,
            vector_factory=vector_factory,
            stage_timer=timer,
        )

    with timer.measure(STAGE_COLLAPSE):
        collapsed = await collapse_results(
            db,
            tenant_id=tenant_id,
            chunks=chunks,
            kb_ids=kb_ids,
            top_k=top_k,
            fold_to_sections=fold_to_sections,
            expand_table_rows=expand_table_rows,
            max_unit_chars=max_unit_chars,
        )

    return KernelOutcome(chunks=collapsed, degraded_reason=degraded_reason, rerank=rerank_info)


async def _retrieve_by_vector(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: Sequence[str],
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
            kb_ids=kb_ids,
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
    kb_ids: Sequence[str],
    queries: Sequence[str],
    top_k: int,
    keyword_factory: Callable[[], Any],
    stage_timer: StageTimer,
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤，纯关键词打分。

    可见性不在这里校验：调用方已经在进入内核之前统一校验过一遍（ADR-0006 的先后顺序
    不变：不可见的库先被拒绝）。多句的 gather / 合并 / 错误翻译交给 ``keyword_recall``
    ——与混合模式那一支共用同一份实现（第 15 步：两路对用句的看法必须一致）。
    """

    with stage_timer.measure(STAGE_KEYWORD_RECALL):
        rows = await keyword_recall(
            provider=keyword_factory(),
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            queries=queries,
            top_k=top_k,
        )

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


__all__ = ["KernelOutcome", "retrieve_kernel"]
