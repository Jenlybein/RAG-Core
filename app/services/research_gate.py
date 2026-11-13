"""Research 的门禁与组合根：白名单 → 能力位 → 子检索内核 → 状态机 → 响应形状（票 #148）。

HTTP 入口（本票）与 MCP 工具 ``rag_research``（#151）写的是**同一段序列**。与检索那条
``app/services/retrieval_gate.py`` 同形：判定与装配只在这里，路由只把身份、参数和会话递
进来，再把同一个 :class:`~app.schemas.research.ResearchData` 装进 ``{code, msg, data}``
外壳。两份拷贝的代价在检索那边已经写过一次——「改了一处、另一处忘了」是这类判定最坏的
失败方式。

```text
1 白名单      kb_ids ⊆ allowed_kb_ids            → 20008
2 能力位      research_allowed（套餐 ∧ 密钥配置）  → 20013，**不静默降级**
3 参数解析    复用这套集成的检索参数（模式 / 条数 / 重排 / 折叠）
4 范围解析    kb_id → 生效版本（一次调用一次）
5 状态机      注入的内核 + 模型工厂 → 固定五步
6 响应成形    ResearchData（材料是节，轮次只给计数）
```

## 本票只复用两道关卡

第 1 步是白名单（``app.services.data_scope``），第 2 步是能力位（``plans`` /
``key_policies`` 上的 ``research_allowed``）。**配额与独立限流归 #150**：那里会给套餐与
密钥配置各加 ``research_qps`` / ``research_daily`` 两列，并在这里补上「进门只查、成功后
记一次」。本模块因此**没有**任何记账调用——这也正是「子检索不重复记账」在结构上的保证：
一份账要记的调用点只有一个，而它现在还不存在。

## 为什么没有授权是拒绝而不是降级

``20013`` 是明确拒绝（ADR-0026 §1）。静默降级成一次普通检索会让调用方以为自己在用
Research，实际只拿到一份材料——**它花了 Research 的预算做了非 Research 的事，而响应里
看不出任何区别**。这条与命名档位越权是同一个判断：名字是契约，换一条链路就得换一个名字。

## 子检索不碰那五件事

``build_kernel_search`` 里没有查询改写（``vector_queries`` 就是子查询本身）、没有可见性
校验（第 1 步与 ``ensure_visible`` 已经在入口做过一次）、没有配额记账、没有归属索引、
没有观测提交。五件事都是「每次调用只做一次」，而内核会被调 N 次（ADR-0028 §2）。一次
Research 的身份仍然是一行归属索引、一份 trace——那两件在外层，属于 #149 / #150 的收口。
"""

from __future__ import annotations

from collections.abc import Mapping
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
) -> SearchCallable:
    """把「一次子查询」装配成「一次内核调用」（ADR-0028 §4 的注入缝在生产侧的实现）。

    三条查询串全等于子查询本身，这是**故意的**：规划器已经把问题分解好了，再改写一轮会
    把已分解的子查询揉回原问题（ADR-0028 背景第 1 条），而且每条子查询再多一次模型调用
    就直接撞破「3 次模型调用」的上限。

    条数与折叠口径与 ``/rag/retrieve`` 逐项相同（读同一份 :class:`RetrievalOptions`）：
    同一个集成用它自己的档位参数跑子检索，Research 不发明第二套「检索多好」。两个服务端
    旋钮（召回深度、融合池宽度）在这里读、当参数交给内核——内核自己不碰部署配置。
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
        # 只要交付单元：降级原因、重排事实、耗时都是「这一次调用」的事，由外层的观测
        # 负责（ADR-0028：子检索不单独发观测）。
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
    """按门禁顺序跑完整次 Research，返回**已经校验成形**的响应数据。

    ``search`` / ``llm_factory`` / ``clock`` 是三个注入缝（父 spec #138：「新服务 Research
    吃注入的检索内核 + 模型工厂」）。生产路径一个都不传——缺省走真实内核、真实模型与
    ``time.monotonic``；用例换掉它们就能在不发网络的前提下验完门禁、降级与上限。

    参数非法、越权、无授权都在这里抛出 ``AppError``（ADR-0007：业务失败，HTTP 200 +
    非零 ``code``）。**子检索自身的故障原样上抛**：它意味着这套集成的检索不可用，而
    Research 的降级出口只覆盖三个模型阶段，不覆盖检索本身。
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

    # 第 5 步：状态机。检索内核按「一次子查询一次」注入，模型工厂同理。
    outcome = await run_research(
        query=query,
        tenant_id=key.tenant_id,
        search=search
        or build_kernel_search(
            db, tenant_id=key.tenant_id, index_versions=index_versions, options=options
        ),
        llm_factory=llm_factory,
        clock=clock,
    )

    # 第 6 步：响应成形。材料按交付单元的契约校验（形状与 ``/rag/retrieve`` 同一个值对象），
    # 校验不过就不是一次成功调用——与检索侧「响应不成立就不算成功」同一条。
    return ResearchData.model_validate(
        payload_of(query=query, kb_ids=kb_ids, outcome=outcome, plan_name=PLAN_NAME)
    )


__all__ = ["build_kernel_search", "ensure_research_allowed", "gated_research"]
