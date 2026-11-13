"""Research 的门禁与组合根：白名单 → 能力位 → 子检索内核 → 状态机 → 身份 → 响应形状（票 #148 / #149）。

HTTP 入口（本票）与 MCP 工具 ``rag_research``（#151）写的是**同一段序列**。与检索那条
``app/services/retrieval_gate.py`` 同形：判定与装配只在这里，路由只把身份、参数和会话递
进来，再把同一个 :class:`~app.schemas.research.ResearchData` 装进 ``{code, msg, data}``
外壳。两份拷贝的代价在检索那边已经写过一次——「改了一处、另一处忘了」是这类判定最坏的
失败方式。

```text
1 白名单      kb_ids ⊆ allowed_kb_ids              → 20008
2 能力位      research_allowed（套餐 ∧ 密钥配置）    → 20013，**不静默降级**
3 参数解析    复用这套集成的检索参数（模式 / 条数 / 重排 / 折叠）
4 范围解析    kb_id → 生效版本（一次调用一次）
5 状态机      注入的内核 + 两个模型角色 → 固定五步
6 身份        log_id → trace_id（派生，先于任何一次写库尝试）
7 归属索引    一次 Research = **一行**（chunk_ids 存最终证据的节；子检索不写行）
8 响应成形    ResearchData（材料是节，轮次只给计数，元数据带身份与三条不丢信息的字段）
```

## 本票只复用两道关卡

第 1 步是白名单（``app.services.data_scope``），第 2 步是能力位（``plans`` /
``key_policies`` 上的 ``research_allowed``）。**配额与独立限流归 #150**：那里会给套餐与
密钥配置各加 ``research_qps`` / ``research_daily`` 两列，并在这里补上「进门只查、成功后
记一次」。本模块因此**没有**任何记账调用。

## 为什么没有授权是拒绝而不是降级

``20013`` 是明确拒绝（ADR-0026 §1）。静默降级成一次普通检索会让调用方以为自己在用
Research，实际只拿到一份材料——**它花了 Research 的预算做了非 Research 的事，而响应里
看不出任何区别**。这条与命名档位越权是同一个判断：名字是契约，换一条链路就得换一个名字。

## 一次 Research 一行归属索引（票 #149 的追加要求）

ADR-0026「影响范围」把这条钉死：一次 Research = 一个 ``log_id`` / ``trace_id`` = **一行**
（``chunk_ids`` 存最终证据的节），子检索不写行（ADR-0028 §2）。实现上只有一个理由成立——
**写入点只有一个，而它在子检索之外**：内核 ``build_kernel_search`` 里没有任何写库调用，
所以 N 条子查询不可能各写一行；本模块在状态机跑完之后写那唯一的一行。

身份的顺序与检索那条逐字相同：``log_id`` 先生成、``trace_id`` 立刻派生，**都在任何一次
写库尝试之前**。于是写入失败（旁路，只留 warning）不会让响应里的 ``trace_id`` 缺席——
反过来「写成功才拿到 id」等于把契约挂在旁路的健康度上。

``chunk_ids`` 用 ``delivered_chunk_ids``（代表片 + 溯源片，去重保序）：它回答的是「这一次
交付了哪几节」，与 ``/rag/retrieve`` 那一列同一个口径。``degraded_reason`` 只记**一条**
（一因一码，先到的才是根因）：先后顺序是「子检索降级」>「生成兜底」——前者发生在材料到手
之前，后者是收尾时的事。``empty_reason`` 这一列在 Research 上恒为 ``null``：没有材料时
``stop_reason`` 与 ``evidence_state`` 已经把原因说清楚了，再编一个码只会多出第三种说法。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import KeyContext
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.index_version import resolve_index_versions
from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PlanSpec
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.schemas.research import ResearchData, payload_of
from app.services import result_collapse
from app.services.data_scope import ensure_kb_scope
from app.services.knowledge_base_access import ensure_visible
from app.services.profile_policy import resolve_retrieval_options
from app.services.research import Clock, LLMFactory, SearchCallable, run_research
from app.services.research_limits import PLAN_NAME
from app.services.retrieval.kernel import retrieve_kernel
from app.services.retrieval_options import RetrievalOptions
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import delivered_chunk_ids, trace_id_for
from app.utils.id_generator import new_retrieval_log_id

def ensure_research_allowed(*, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
    """Research 能力位：**套餐**与**密钥配置**两层都得给，否则 ``20013``。

    两层都要，是因为它们回答的是两个不同的问题（ADR-0018）：套餐说「这个租户买没买」，
    密钥配置说「这套集成要不要」。任何一层没开就拒绝——不是「悄悄退回普通检索」。
    """

    if policy.research_allowed and plan.research_allowed:
        return
    raise AppError(
        ErrorCode.FEATURE_NOT_ALLOWED,
        f"research is not allowed (plan {plan.code!r}, key policy "
        f"research_allowed={policy.research_allowed})",
    )


def build_kernel_search(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    options: RetrievalOptions,
    on_degraded: Callable[[str], None] | None = None,
) -> SearchCallable:
    """把「一次子查询」装配成「一次内核调用」（ADR-0028 §4 的注入缝在生产侧的实现）。

    三条查询串全等于子查询本身，这是**故意的**：规划器已经把问题分解好了，再改写一轮会
    把已分解的子查询揉回原问题（ADR-0028 背景第 1 条），而且每条子查询再多一次模型调用
    就直接撞破「3 次模型调用」的上限。

    条数与折叠口径与 ``/rag/retrieve`` 逐项相同（读同一份 :class:`RetrievalOptions`）：
    同一个集成用它自己的档位参数跑子检索，Research 不发明第二套「检索多好」。两个服务端
    旋钮（召回深度、融合池宽度）在这里读、当参数交给内核——内核自己不碰部署配置。

    ``on_degraded`` 是**子检索降级的观察点**（票 #149）：内核交回一条固定串时调它一次。
    降级原因不能丢（「关键词一路挂了、只是碰巧向量一路也命中了」的那次调用看起来完全正常），
    但它又不许变成「每条子查询各写一行归属索引」或者「各发一份观测」——所以它被交回给
    调用方攒成一个列表，最后进响应的 ``metadata`` 与那唯一一行归属索引。
    """

    recall_top_k = (
        options.top_k * result_collapse.OVERFETCH_FACTOR
        if options.fold_to_sections
        else options.top_k
    )
    recall_depth = max(options.top_k, settings.HYBRID_RECALL_TOP_K)
    rerank_candidates = settings.RERANK_MAX_CANDIDATES if options.rerank else None

    async def search(sub_query: str) -> list[dict[str, Any]]:
        outcome = await retrieve_kernel(
            db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            query=sub_query,
            mode=options.mode,
            # 不做改写：三条查询串就是这一条子查询。
            vector_queries=[sub_query],
            rerank_query=sub_query,
            keyword_query=sub_query,
            top_k=options.top_k,
            recall_top_k=recall_top_k,
            recall_depth=recall_depth,
            rerank_enabled=options.rerank,
            rerank_candidates=rerank_candidates,
            fold_to_sections=options.fold_to_sections,
            expand_table_rows=options.expand_table_rows,
            # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有 monkeypatch 接缝）。
            embedding_factory=get_embedding_provider,
            vector_factory=get_vector_store,
            keyword_factory=get_keyword_search_provider,
        )
        # 降级原因原样上交（一因一码的固定串，不拼异常文本）；其余「这一次调用」的事实
        # （重排、耗时）由外层的观测负责（ADR-0028：子检索不单独发观测）。
        if outcome.degraded_reason and on_degraded is not None:
            on_degraded(outcome.degraded_reason)
        # 只要交付单元。
        return outcome.chunks

    return search


async def gated_research(
    db: AsyncSession,
    *,
    key: KeyContext,
    kb_ids: list[str],
    query: str,
    search: SearchCallable | None = None,
    llm_factory: LLMFactory | None = None,
    strong_llm_factory: LLMFactory | None = None,
    clock: Clock | None = None,
) -> ResearchData:
    """按门禁顺序跑完整次 Research，返回**已经校验成形**的响应数据。

    ``search`` / ``llm_factory`` / ``strong_llm_factory`` / ``clock`` 是四个注入缝（父 spec
    #138：「新服务 Research 吃注入的检索内核 + 模型工厂」）。生产路径一个都不传——缺省走
    真实内核、两个真实模型角色与 ``time.monotonic``；用例换掉它们就能在不发网络的前提
    下验完门禁、降级与上限。

    参数非法、越权、无授权都在这里抛出 ``AppError``（ADR-0007：业务失败，HTTP 200 +
    非零 ``code``）。**子检索自身的故障原样上抛**：它意味着这套集成的检索不可用，而
    Research 的降级出口只覆盖三个模型阶段，不覆盖检索本身。

    归属索引写在这里、且**只写一次**（票 #149）：子检索的内核里没有写库调用，所以 N 条
    子查询不可能各写一行；状态机跑完之后写那唯一的一行，``chunk_ids`` 存最终证据的节。
    """

    # 第 1 步：白名单。越权的库先拒，一次嵌入都不该烧（与检索那条同一条纪律）。
    ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)

    # 第 2 步：能力位。拒绝是明确的 20013，不退回普通检索。
    ensure_research_allowed(policy=key.policy, plan=key.plan)

    # 第 3 步：这套集成的检索参数（模式 / 条数 / 重排 / 折叠 / 库数上限）。Research 没有
    # 自己的质量参数：子检索用这套集成本来会用的一组参数，档位门禁也在这里一并复核。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(kb_ids),
    )

    # 第 4 步：范围解析（``kb_id → 生效版本`` 的映射，一次调用一次）。没有生效版本的库
    # 在这里就被拦下，而不是让 N 条子查询各拼一条恒不匹配的条件。
    visible_kbs = [
        await ensure_visible(db, tenant_id=key.tenant_id, kb_id=kb_id) for kb_id in kb_ids
    ]
    index_versions = resolve_index_versions(visible_kbs)

    # 第 5 步：身份先于旁路。``log_id`` / ``trace_id`` 在任何一次写库尝试之前就有值：
    # 那一次写入是旁路（失败只留 warning），而响应里的 ``trace_id`` 是契约的一部分。
    log_id = new_retrieval_log_id()
    trace_id = trace_id_for(log_id)

    # 第 6 步：状态机。检索内核按「一次子查询一次」注入，两个模型角色同理；子检索的降级
    # 原因经由同一个列表回到这里（它既要进响应，也要进那唯一一行归属索引）。
    sub_search_degraded: list[str] = []
    outcome = await run_research(
        query=query,
        tenant_id=key.tenant_id,
        search=search
        or build_kernel_search(
            db,
            tenant_id=key.tenant_id,
            index_versions=index_versions,
            options=options,
            on_degraded=sub_search_degraded.append,
        ),
        llm_factory=llm_factory,
        strong_llm_factory=strong_llm_factory,
        sub_search_degraded=sub_search_degraded,
        clock=clock,
    )

    # 第 7 步：归属索引——**一次 Research 一行**。放在这里而不是更早，是因为它要记「这一
    # 次的结局」；而 ``trace_id`` 早在第 5 步就派生好了，所以这次写入失败不影响响应。
    # 耗时用状态机自己那份（``duration_ms``）：库里那一行与回给调用方的那个数字必须同一个。
    await record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id=key.tenant_id,
        kb_ids=list(kb_ids),
        index_versions=dict(index_versions),
        chunk_ids=delivered_chunk_ids(outcome.materials),
        top_k=options.top_k,
        latency_ms=outcome.duration_ms,
        degraded_reason=_degraded_reason_of(outcome),
        empty_reason=None,
    )

    # 第 8 步：响应成形。材料按交付单元的契约校验（形状与 ``/rag/retrieve`` 同一个值对象），
    # 校验不过就不是一次成功调用——与检索侧「响应不成立就不算成功」同一条。
    return ResearchData.model_validate(
        payload_of(
            query=query,
            kb_ids=kb_ids,
            outcome=outcome,
            plan_name=PLAN_NAME,
            trace_id=trace_id,
        )
    )


def _degraded_reason_of(outcome: Any) -> str | None:
    """归属索引那一列只记**一条**原因（一因一码，先到的才是根因）。

    顺序：子检索自己的降级 > 生成阶段换了快模型。前者发生在材料到手之前，后者是收尾时
    的事——两条中哪一条先发生，哪一条才是解释「这一次为什么不如预期」的那一条。
    """

    if outcome.sub_search_degraded:
        return outcome.sub_search_degraded[0]
    return outcome.strong_model_fallback


__all__ = ["build_kernel_search", "ensure_research_allowed", "gated_research"]
