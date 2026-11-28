"""Research 的门禁与组合根：白名单 → 能力位 → 档位 → 限流 → 日配额 → 身份 → 状态机 → 归属索引 → 记账（票 #148 / #149 / #150）。

HTTP 入口（本票）与 MCP 工具 ``rag_research``（#151）写的是**同一段序列**。与检索那条
``app/services/retrieval_gate.py`` 同形：判定与装配只在这里，路由只把身份、参数和会话递
进来，再把同一个 :class:`~app.schemas.research.ResearchData` 装进 ``{code, msg, data}``
外壳。两份拷贝的代价在检索那边已经写过一次——「改了一处、另一处忘了」是这类判定最坏的
失败方式。

```text
1  白名单      kb_ids ⊆ allowed_kb_ids              → 20008
2  能力位      research_allowed（套餐 ∧ 密钥配置）
                ∧ 四个配额格都 > 0                  → 20013，**不静默降级**
3  参数解析    复用这套集成的检索参数（模式 / 条数 / 重排 / 折叠）
4  秒级限流    Key 级 → 租户级                       → 20005（该退避重试）
5  日配额      Key 级 → 租户级                       → 20014（该提额或换套餐）
6  范围解析    kb_id → 生效版本（一次调用一次）
7  身份        log_id → trace_id（派生，先于任何一次写库尝试）
8  状态机      注入的内核 + 两个模型角色 → 固定五步
9  归属索引    一次 Research = **一行**（chunk_ids 存最终证据的节；子检索不写行）
10 响应成形    ResearchData（材料是节，轮次只给计数，元数据带身份与三条不丢信息的字段）
11 记账        响应成形之后才 +1，两级各一次（**一次 Research 只记一次**）
```

## 三个错误码，不许混用

``20013``（没这项能力 / 配额为 0）、``20005``（这一秒打得太快）、``20014``（今天的额度
用完了）回答的是三个不同的问题，对应三种不同的处置：换套餐、退避重试、提额。合并任意两个
都会让调用方写不出重试策略。

「配额为 0」这个说法在第 13 步已经不存在了：Research 没有自己的额度列，只剩 ``research``
授权位（位为假 → ``20013``）。成本由 ``llm`` 那本账计，额度用尽是 ``20014``——两个码、
两件事，调用方据此能分清「该升级套餐」与「该等明天」。

## 成本记在 llm 那本账上

一次 Research 约 30 秒、3–4 次大模型调用，它的成本就是那几次调用，所以记在 ``llm`` 账
（``app.services.quota_gate`` 的 ``LLM_NAMESPACE``），不再单开 ``quota:research``
（票 #150 的那本已随第 13 步退役）。

## 进门只查、成功后记一次，且只记一次

记账的唯一调用点在**第 11 步**，也就是整次调用的最外层、响应也成形之后：子检索（第 8 步
那条内核会跑 1~5 次）碰不到它。这正是「一次 Research 只记一次、不按子查询重复扣」在结构
上的保证——**一份账要记的调用点只有一个**，而不是「每个调用点都记得别多记」。响应不成立
就不算一次成功调用，所以记账排在 ``ResearchData`` 校验之后。

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

## 子检索不碰那五件事

``build_kernel_search`` 里没有查询改写（``vector_queries`` 就是子查询本身）、没有可见性
校验（第 1 步与 ``ensure_visible`` 已经在入口做过一次）、没有配额记账、没有归属索引、
没有观测提交。五件事都是「每次调用只做一次」，而内核会被调 N 次（ADR-0028 §2）；五件事
因此全在外层：配额记账在第 10 步，归属索引在第 9 步，观测提交是 #151 的收口。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import KeyContext
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.index_version import resolve_index_versions
from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PlanSpec
from app.observability.stage_timing import StageTimer
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.schemas.research import ResearchData, payload_of
from app.services import result_collapse
from app.services.data_scope import ensure_kb_scope
from app.services.knowledge_base_access import ensure_visible
from app.services.profile_policy import resolve_retrieval_options
from app.services.query_content import ensure_query_has_content
from app.services.quota_gate import llm_quota, rerank_quota
from app.services.rerank_gate import degrade_rerank_if_exhausted
from app.services.research import Clock, LLMFactory, SearchCallable, run_research
from app.services.research_limits import PLAN_NAME
from app.services.retrieval.kernel import retrieve_kernel
from app.services.retrieval_options import RetrievalOptions
from app.services.retrieval_trace import record_retrieval_trace
from app.services.usage_meter import ENDPOINT_RESEARCH, current_key, current_key_id, record_refusal
from app.services.trace_identity import (
    delivered_chunk_ids,
    delivered_doc_versions,
    trace_id_for,
)
from app.utils.id_generator import new_retrieval_log_id


def ensure_research_allowed(*, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
    """Research 能力位：**套餐**与**密钥配置**两层都得给，否则 ``20013``。

    两层都要，是因为它们回答的是两个不同的问题（ADR-0018）：套餐说「这个租户买没买」，
    密钥配置说「这套集成要不要」。任何一层没开就拒绝——不是「悄悄退回普通检索」。

    第 13 步起 Research **没有自己的额度账**（ADR-0032）：它的成本就是那几次大模型调用，
    由 ``llm`` 那本账计。所以这里只判两个位；「额度用尽」由 ``llm`` 账答 ``20014``，
    与位的真假是两个码、两件事。
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
    kb_ids: Sequence[str],
    options: RetrievalOptions,
    on_degraded: Callable[[str], None] | None = None,
    on_rerank: Callable[[], None] | None = None,
    stage_timer: StageTimer | None = None,
) -> SearchCallable:
    """把「一次子查询」装配成「一次内核调用」（ADR-0028 §4 的注入缝在生产侧的实现）。

    三条查询串全等于子查询本身，这是**故意的**：规划器已经把问题分解好了，再改写一轮会
    把已分解的子查询揉回原问题（ADR-0028 背景第 1 条），而且每条子查询再多一次模型调用
    就直接撞破「4 次模型调用」的上限。

    条数与折叠口径与 ``/rag/retrieve`` 逐项相同（读同一份 :class:`RetrievalOptions`）：
    同一个集成用它自己的档位参数跑子检索，Research 不发明第二套「检索多好」。两个服务端
    旋钮（召回深度、融合池宽度）在这里读、当参数交给内核——内核自己不碰部署配置。

    ``on_degraded`` 是**子检索降级的观察点**（票 #149）：内核交回一条固定串时调它一次。
    降级原因不能丢（「关键词一路挂了、只是碰巧向量一路也命中了」的那次调用看起来完全正常），
    但它又不许变成「每条子查询各写一行归属索引」或者「各发一份观测」——所以它被交回给
    调用方攒成一个列表，最后进响应的 ``metadata`` 与那唯一一行归属索引。

    ``on_rerank`` 是**重排实际发生的观察点**（第 13 步）：子检索每真跑一次重排调它一次，
    外层据此按实际发生记 rerank 账。与 ``on_degraded`` 同一条纪律：内核只交回事实，
    记账仍在门禁那唯一一处。

    ``stage_timer`` 是**共用的收集器**（第 13 步）：分解检索要把 N 次内核的耗时记进
    **同一份** trace，段名与 ``/rag/retrieve`` 同源（``StageTimer`` 允许同名段依次追加）。
    子检索仍然不各发一份观测（ADR-0028），只是各自往同一个收集器里记一笔。
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
            kb_ids=list(kb_ids),
            query=sub_query,
            mode=options.mode,
            # 不做改写：召回用句与重排用句都是这一条子查询。
            recall_queries=[sub_query],
            rerank_query=sub_query,
            top_k=options.top_k,
            recall_top_k=recall_top_k,
            recall_depth=recall_depth,
            rerank_enabled=options.rerank,
            rerank_quota_exhausted=options.rerank_skipped_reason is not None,
            rerank_candidates=rerank_candidates,
            fold_to_sections=options.fold_to_sections,
            expand_table_rows=options.expand_table_rows,
            max_unit_chars=options.max_unit_chars,
            # 同一个收集器：N 次内核的段依次追加进这一份 trace。
            stage_timer=stage_timer,
            # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有 monkeypatch 接缝）。
            embedding_factory=get_embedding_provider,
            vector_factory=get_vector_store,
            keyword_factory=get_keyword_search_provider,
        )
        # 降级原因原样上交（一因一码的固定串，不拼异常文本）；其余「这一次调用」的事实
        # （重排、耗时）由外层的观测负责（ADR-0028：子检索不单独发观测）。
        if outcome.degraded_reason and on_degraded is not None:
            on_degraded(outcome.degraded_reason)
        # 重排**真的发生了**才报一次：外层据此按实际发生记 rerank 账（ADR-0032）。
        # 内核不碰账本，它只交回事实——记账仍是外层那唯一一处。
        if on_rerank is not None and (outcome.rerank or {}).get("applied"):
            on_rerank()
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
    clock: Clock | None = None,
) -> ResearchData:
    """Research 的公开入口：**被拒也要落一行账**（第 14 步 #200）。

    判定全在 :func:`_gated_research`；这一层只把 ``AppError`` 抄成一行 ``usage_events``
    再原样抛出，理由与 :func:`app.services.retrieval_gate.gated_retrieve` 逐字相同：
    MCP 与 HTTP 共用这个函数，抄在路由上 MCP 的拒绝就不进账。
    """

    started_at = time.perf_counter()
    try:
        # 成功那一行账由状态机之后那段写；它要知道「是哪把 Key」，靠上下文拿。
        with current_key(key.key_id):
            return await _gated_research(
                db,
                key=key,
                kb_ids=kb_ids,
                query=query,
                search=search,
                llm_factory=llm_factory,
                clock=clock,
            )
    except AppError as exc:
        await record_refusal(
            endpoint=ENDPOINT_RESEARCH,
            key=key,
            kb_ids=kb_ids,
            code=int(exc.code),
            started_at=started_at,
        )
        raise


async def _gated_research(
    db: AsyncSession,
    *,
    key: KeyContext,
    kb_ids: list[str],
    query: str,
    search: SearchCallable | None = None,
    llm_factory: LLMFactory | None = None,
    clock: Clock | None = None,
) -> ResearchData:
    """按门禁顺序跑完整次 Research，返回**已经校验成形**的响应数据。

    ``search`` / ``llm_factory`` / ``clock`` 是三个注入缝（父 spec #138）。生产路径一个
    都不传——缺省走真实内核、唯一的 ``default`` 模型角色与 ``time.monotonic``；用例换掉
    它们就能在不发网络的前提下验完门禁与上限。第 13 步起模型调用失败整单 50003，
    不再有强模型工厂与兜底注入缝。

    参数非法、越权、无授权、超限都在这里抛出 ``AppError``（ADR-0007：业务失败，HTTP 200 +
    非零 ``code``）。**子检索自身的故障原样上抛**：它意味着这套集成的检索不可用，而
    Research 的降级出口只覆盖三个模型阶段，不覆盖检索本身。

    归属索引写在这里、且**只写一次**（票 #149）：子检索的内核里没有写库调用，所以 N 条
    子查询不可能各写一行；状态机跑完之后写那唯一的一行，``chunk_ids`` 存最终证据的节。
    配额记账同理**只记一次**（票 #150），而且排在响应成形之后——见下面第 10 步。
    """

    # 第 1 步：白名单。越权的库先拒，一次嵌入都不该烧（与检索那条同一条纪律）。
    ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)

    # 第 2 步：能力位。拒绝是明确的 20013，不退回普通检索。
    ensure_research_allowed(policy=key.policy, plan=key.plan)

    # 第 2.5 步：问题里没有可检索的内容 → 20033。位置在**任何模型调用之前**：
    # Research 一次要花 3~4 次模型调用与 30 秒，为 ``啊啊啊`` 付这笔钱、再拿规划器
    # 自述的几条「要点」当证据召回，是这一票要根治的那个现象。
    ensure_query_has_content(query)

    # 第 3 步：这套集成的检索参数（模式 / 条数 / 重排 / 折叠 / 库数上限）。Research 没有
    # 自己的质量参数：子检索用这套集成本来会用的一组参数，套餐复核也在这里一并做。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(kb_ids),
    )

    # 第 4 步：秒级限流 → 第 5 步：日配额（``20005`` / ``20014``）。**进门只查**，记账在
    # 最后一步——ADR-0018「被拒绝的请求不扣额度」。
    #
    # 第 13 步：Research 不再有独立的一本账（ADR-0032）。它的成本就是那几次大模型调用，
    # 记在 ``llm`` 那本上——另开一本等于把同一份成本记两遍，而「一次 Research 记一次」
    # 这条纪律仍然成立：唯一的记账点在状态机之后。
    quota = llm_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await quota.ensure_available()

    # 第 5.5 步：重排额度用尽 → 子检索跳过重排 + 记原因，整单照跑（ADR-0032）。
    # 「一次可选优化没额度」不该让一次 30 秒的 Research 白等之后失败。
    options = await degrade_rerank_if_exhausted(options, key)
    visible_kbs = [
        await ensure_visible(db, tenant_id=key.tenant_id, kb_id=kb_id) for kb_id in kb_ids
    ]
    index_versions = resolve_index_versions(visible_kbs)

    # 第 7 步：身份先于旁路。``log_id`` / ``trace_id`` 在任何一次写库尝试之前就有值：
    # 那一次写入是旁路（失败只留 warning），而响应里的 ``trace_id`` 是契约的一部分。
    log_id = new_retrieval_log_id()
    trace_id = trace_id_for(log_id)

    # 第 8 步：状态机。检索内核按「一次子查询一次」注入，两个模型角色同理；子检索的降级
    # 原因经由同一个列表回到这里（它既要进响应，也要进那唯一一行归属索引）。
    sub_search_degraded: list[str] = []
    rerank_count = 0

    def _count_rerank() -> None:
        nonlocal rerank_count
        rerank_count += 1

    outcome = await run_research(
        query=query,
        tenant_id=key.tenant_id,
        search=search
        or build_kernel_search(
            db,
            tenant_id=key.tenant_id,
            kb_ids=list(kb_ids),
            options=options,
            on_degraded=sub_search_degraded.append,
            on_rerank=_count_rerank,
        ),
        llm_factory=llm_factory,
        sub_search_degraded=sub_search_degraded,
        clock=clock,
    )

    # 第 9 步：归属索引——**一次 Research 一行**。放在这里而不是更早，是因为它要记「这一
    # 次的结局」；而 ``trace_id`` 早在第 7 步就派生好了，所以这次写入失败不影响响应。
    # 耗时用状态机自己那份（``duration_ms``）：库里那一行与回给调用方的那个数字必须同一个。
    await record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id=key.tenant_id,
        # 第 14 步（#200）：这一行同时是账——端点是 research，码是成功，
        # 三本账按**实际发生**记：重排每真跑一次算一次，大模型调用就是状态机报的
        # ``model_calls``；r 账不记（Research 不走 retrieve 那本账，见 ADR-0032）。
        key_id=current_key_id(),
        endpoint=ENDPOINT_RESEARCH,
        code=0,
        meter_rr=rerank_count,
        meter_llm=outcome.model_calls,
        kb_ids=list(kb_ids),
        index_versions=dict(index_versions),
        # 一次 Research = 一行归属索引（ADR-0028）：稿号同样取**最终证据**那些单元上的
        # 真实列（#179）。子检索各自命中的稿不在这里记——它们不单独成行。
        doc_versions=delivered_doc_versions(outcome.materials),
        chunk_ids=delivered_chunk_ids(outcome.materials),
        top_k=options.top_k,
        latency_ms=outcome.duration_ms,
        degraded_reason=_degraded_reason_of(outcome),
        empty_reason=None,
    )

    # 第 10 步：响应成形。材料按交付单元的契约校验（形状与 ``/rag/retrieve`` 同一个值对象），
    # 校验不过就不是一次成功调用——与检索侧「响应不成立就不算成功」同一条。
    data = ResearchData.model_validate(
        payload_of(
            query=query,
            kb_ids=kb_ids,
            outcome=outcome,
            plan_name=PLAN_NAME,
            trace_id=trace_id,
        )
    )
    # 第 11 步：成功之后才记账，两级各一次。这是整次调用**唯一**的记账点：第 8 步那条
    # 内核被调了 1~5 次，但它碰不到这本账；被拒绝、失败或响应校验不过的调用都走不到这里。
    # llm 账按**实际发生**记 N（``model_calls``），不再额外 +1——「实际发生几次就记几次」
    # 是契约本身（ADR-0032），多记一次就把 Research 的成本凭空抬高了一截。
    llm = llm_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await llm.record_n(outcome.model_calls)
    # 重排账同样按实际发生记：子检索每真跑一次重排，观察点就 +1（全 0 时账本 no-op）。
    await rerank_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    ).record_n(rerank_count)
    return data


def _degraded_reason_of(outcome: Any) -> str | None:
    """归属索引那一列只记**一条**原因（一因一码，先到的才是根因）。

    第 13 步之后唯一的产出方是子检索自己的降级：模型调用失败整单 50003，不会走到
    写归属索引这一步；强模型兜底字段已经删除。
    """

    if outcome.sub_search_degraded:
        return outcome.sub_search_degraded[0]
    return None


__all__ = ["build_kernel_search", "ensure_research_allowed", "gated_research"]
