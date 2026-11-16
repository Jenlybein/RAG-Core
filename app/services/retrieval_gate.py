"""整段门禁化的检索：白名单 → 授权位 → 参数解析 → 配额 → 召回 → 成功记账（票 #76 / #78 / #79）。

HTTP 路由与 MCP 工具写的是**同一段五步序列**，过去是两处各写一遍、注释都说「同一份实现」。
两份拷贝的第六道关卡意味着改两处，而「改了一处、另一处忘了」正是这类判定最坏的失败方式：
两条入口从此判出不同答案，测试还恰好只覆盖一边。本模块是那段序列的**唯一实现**，
两个入口只负责把身份、参数和会话递进来，再把同一个 :class:`RetrieveData` 装成各自的形状。

顺序即判定的一部分（ADR-0018 第 4 条），后一步依赖前一步的产出：

```text
1 白名单      kb_ids ⊆ allowed_kb_ids            → 20008
2 套餐复核    授权位 + 参数解析 + 单次库数        → 20013
3 配额        秒级限流 → 日配额                  → 20005 / 20014
4 召回        交给组合根 rag_service.retrieve
5 记账        业务成功、响应也成形之后才 +1
```

**记账时点刻意在最后**：配额「进门只查、成功后记」（ADR-0018），所以被拒的请求一次都不
扣额度。``RetrieveData`` 的校验也放在记账之前——响应形状不成立就不算一次成功调用。

失败一律抛 :class:`AppError`：越权与超限是业务失败（ADR-0007），不是协议错误。两个入口
对它的处理不同（HTTP 交给异常处理器变成 ``{code, msg}`` 外壳，MCP 转成 JSON-RPC 的
``isError`` 文本），但**抛什么、什么时候抛**只有这一份。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import KeyContext
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.schemas.rag import RetrieveData
from app.services import rag_service
from app.services.data_scope import ensure_kb_scope
from app.services.profile_policy import resolve_retrieval_options
from app.services.quota_gate import llm_quota, rerank_quota, retrieval_quota
from app.services.retrieval.rerank import REASON_RERANK_QUOTA_EXHAUSTED
from app.services.retrieval_authorization import ensure_endpoint_allowed

#: ``rag_service.retrieve`` 的形状：组合根只认「一个会话 + 已解析参数」。
#: MCP 的 ``create_mcp_server`` 把它做成可注入的回调（既有接缝，测试与 app.main 都在用），
#: 所以这里也允许显式传入；不传就取模块属性，monkeypatch 替换 ``rag_service.retrieve``
#: 之后两个入口仍然一起换掉。
RetrieveCallable = Callable[..., Awaitable[dict[str, Any]]]


async def gated_retrieve(
    db: AsyncSession,
    *,
    key: KeyContext,
    kb_ids: list[str],
    query: str,
    variant: Literal["direct", "rewrite"] = "direct",
    retrieve: RetrieveCallable | None = None,
) -> RetrieveData:
    """按六道关卡的顺序跑完整次检索，返回**已经校验成形**的响应数据。

    ``db`` 是**请求级异步会话**（第 11 步票 #127）：第 1~3 步不碰库，第 4 步把它原样交给
    组合根。身份判定仍然走同步会话——那是 ``get_current_key`` 自己的依赖，不在本函数里。

    ``key`` 是判定内核的结论（租户、套餐、密钥配置都在上面）。``kb_ids`` / ``query`` 是
    请求的全部内容——质量参数由 ``profile_policy`` 从密钥配置解析，调用方看不到。

    参数非法、越权、超限、配额用尽都在这里抛出 ``AppError``；业务异常原样上抛。调用方
    不再补任何判定，也不许在调用之后再判一次「这一步该不该做」。
    """

    retriever: RetrieveCallable = rag_service.retrieve if retrieve is None else retrieve

    # 第 1 步：白名单。身份已经解出来（``key`` 就在手上），限流、配额与召回都还没开始——
    # 顺序反了会让越权请求先扣掉客户当天的额度（ADR-0018「六道关卡」）。
    ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)

    # 第 1.5 步（第 13 步）：端点级授权位。位为假 → 20013，且不降级成另一条端点。
    # 它比参数解析更早：没买这个能力的请求连参数都不该算。
    ensure_endpoint_allowed(
        policy=key.policy,
        plan=key.plan,
        endpoint="rewrite" if variant == "rewrite" else "retrieve",
    )
    # 改写还有一道**部署能力**：这台机器没开改写器时，/rag/rewrite 明确拒绝（20013），
    # 不静默退化成原句直查——那会让调用方以为买到的改写真的发生了（与重排「折进生效值」
    # 不同：重排是可选优化，改写是这条端点的全部身份）。影响预览对这条开关的承诺据此成立。
    if variant == "rewrite" and not settings.QUERY_REWRITE_ENABLED:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            "rewrite is not available on this deployment (QUERY_REWRITE_ENABLED is off)",
        )

    # 第 2 步：套餐复核 + 参数解析 + 单次库数。越权在这里就失败，一次嵌入都不该烧。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(kb_ids),
    )

    # 第 3 步：秒级限流 → 日配额。**进门只查**，成功后记账由第 5 步负责。开关关着时这本账
    # 是 no-op，默认套件因此不需要 Redis。计数命令是异步的（票 #133）：它们在这条异步
    # 链路上，同步 Redis 会把整个事件循环按住。
    quota = retrieval_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await quota.ensure_available()

    # 第 13 步：改写端点额外吃一次 llm 账（全 0 时账本 no-op，旧 Key 不受影响）。
    llm = (
        llm_quota(
            policy=key.policy,
            plan=key.plan,
            tenant_id=key.tenant_id,
            key_id=key.key_id,
        )
        if variant == "rewrite"
        else None
    )
    if llm is not None:
        await llm.ensure_available()

    # 第 3.5 步（第 13 步）：重排是**可选的优化**，额度用尽时跳过而不是拒绝整单
    # （ADR-0032、用户故事 15）。用 ``has_capacity`` 问一句而不是 try/except：后者会把
    # 「这把 Key 没买重排」（该拒绝）与「今天排太多次了」（该跳过）混成一件事。
    # 跳过时必须留下原因——少了这一步却在响应里一个字都不提，就是看不见的降级。
    if options.rerank:
        rerank_ledger = rerank_quota(
            policy=key.policy,
            plan=key.plan,
            tenant_id=key.tenant_id,
            key_id=key.key_id,
        )
        if not await rerank_ledger.has_capacity():
            options = replace(
                options,
                rerank=False,
                rerank_skipped_reason=REASON_RERANK_QUOTA_EXHAUSTED,
            )

    # 第 4 步：召回。组合根拿到的是已经解析完的参数，它不再读请求体、也不再读请求里的质量
    # 字段（那些字段根本不存在）。
    payload = await retriever(
        db,
        tenant_id=key.tenant_id,
        kb_ids=kb_ids,
        query=query,
        options=options,
        query_variant=variant,
    )

    # 第 5 步：响应成形之后才 +1。校验失败的响应不是一次成功调用，所以顺序不能倒过来；
    # 失败 / 异常 / 校验不通过的路径都不会走到这里（ADR-0018「成功后才计数」）。
    result = RetrieveData.model_validate(payload)
    await quota.record_success()
    if llm is not None:
        await llm.record_n(1)  # 改写这一次大模型调用，按实际发生计入 llm 账。
    # 第 13 步：重排账按实际发生记（applied=true 才算一次）。全 0 时账本 no-op。
    rerank_meta = (result.metadata or {}).get("retrieval", {}).get("rerank") or {}
    if rerank_meta.get("applied"):
        rerank = rerank_quota(
            policy=key.policy,
            plan=key.plan,
            tenant_id=key.tenant_id,
            key_id=key.key_id,
        )
        await rerank.record_n(1)
    return result


async def gated_decompose(
    db: AsyncSession,
    *,
    key: KeyContext,
    kb_ids: list[str],
    query: str,
) -> RetrieveData:
    """`/rag/decompose` 的五道关卡：与 `gated_retrieve` 同构，召回换成多内核链。

    白名单 → 套餐复核 / 参数展开 → 配额 → 分解检索 → 成功记账。第 4 步的组合根是
    ``app.services.decompose_gate.decompose_retrieve``（一次分解 + N 次内核 + 合并），
    其余四步与直接检索共用同一份判定，两条入口不可能判出两种答案。
    """

    from app.services.decompose_gate import decompose_retrieve

    # 第 1 步：白名单。
    ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)
    # 第 1.5 步（第 13 步）：分解检索的端点位。位为假 → 20013，不降级成直接检索。
    ensure_endpoint_allowed(policy=key.policy, plan=key.plan, endpoint="decompose")
    # 第 2 步：套餐复核 + 参数展开 + 单次库数。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(kb_ids),
    )
    # 第 3 步：秒级限流 → 日配额。retrieve 那本账记**内核调用次数**，所以进门按 1 次
    # 预检、成功之后按实际跑的 N 次记账（见第 5 步）。
    quota = retrieval_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await quota.ensure_available()

    # 第 13 步：分解那一次规划是大模型调用，吃 llm 账（全 0 时 no-op）。
    llm = llm_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await llm.ensure_available()

    # 第 3.5 步：重排额度用尽 → 跳过重排 + 记原因（不拒绝整单），与直接检索同一条纪律。
    if options.rerank:
        rerank_ledger = rerank_quota(
            policy=key.policy,
            plan=key.plan,
            tenant_id=key.tenant_id,
            key_id=key.key_id,
        )
        if not await rerank_ledger.has_capacity():
            options = replace(
                options,
                rerank=False,
                rerank_skipped_reason=REASON_RERANK_QUOTA_EXHAUSTED,
            )

    # 第 4 步：分解 → N 次内核 → 合并。重排实际发生几次由观察点交回（第 5 步记账用）。
    rerank_count = 0

    def _count_rerank() -> None:
        nonlocal rerank_count
        rerank_count += 1

    payload = await decompose_retrieve(
        db,
        tenant_id=key.tenant_id,
        kb_ids=kb_ids,
        query=query,
        options=options,
        on_rerank=_count_rerank,
    )

    # 第 5 步：响应成形之后才记账。
    result = RetrieveData.model_validate(payload)
    # retrieve 那本账记的是**内核调用次数**，所以分解检索记 N 而不是 1（ADR-0032）。
    # N 从响应的 ``decomposition.queries`` 读：那就是这次真正跑过的子查询清单，
    # 是链路自己交回的事实，而不是门禁在外面又数一遍（数两遍迟早不一致）。
    decomposition = (result.metadata or {}).get("retrieval", {}).get("decomposition") or {}
    kernel_calls = len(decomposition.get("queries") or ())
    await quota.record_n(kernel_calls)
    await llm.record_n(1)  # 那一次分解规划，按实际发生计入 llm 账。
    # rerank 账按实际发生记：N 条子检索各自真跑的那几次（全 0 时账本 no-op）。
    await rerank_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    ).record_n(rerank_count)
    return result
