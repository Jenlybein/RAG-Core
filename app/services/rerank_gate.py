"""重排这道「可选优化」的门：额度用尽就跳过它，并如实记下原因。

这条纪律出现在三条链路上（``/retrieve``、``/decompose``、``/research``），而它有三个
容易写错的地方：

1. 跳过时**必须留下原因**——少了这一步却在响应里一个字都不提，就是看不见的降级；
2. 不能顺手**拒绝整单**——重排只是锦上添花，一次 Research 不该因为它白等 30 秒；
3. 没开重排时**不该白读一次 Redis**——`rerank_quota` 会连计数器，而这次调用本来就不排。

三处各写一遍时，改一处就会漏两处。所以它是**唯一的一处实现**。

**与「没买重排」严格区分**：授权位为假是 ``20013``（该拒绝，调用方要改配置）；额度用尽
是跳过 + 降级（该继续，调用方什么也改不了）。两者都回答「要不要重排」这一个问题，处置
却相反，所以不能用同一个 ``try/except`` 兜住——那正是 :meth:`QuotaLedger.has_capacity`
存在的理由。
"""

from __future__ import annotations

from dataclasses import replace

from app.core.auth import KeyContext
from app.services.quota_gate import rerank_quota
from app.services.retrieval.rerank import REASON_RERANK_QUOTA_EXHAUSTED
from app.services.retrieval_options import RetrievalOptions


async def degrade_rerank_if_exhausted(
    options: RetrievalOptions, key: KeyContext
) -> RetrievalOptions:
    """额度够用（或压根没开重排）就原样返回；不够就关掉重排并写上原因。

    返回**新的** options 而不是就地改：:class:`RetrievalOptions` 是 frozen 的，而且调用方
    之后还要把它递给组合根——「这次到底排不排」必须以一个值的形式往下传，不能靠共享状态。
    """

    if not options.rerank:
        return options

    ledger = rerank_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    if await ledger.has_capacity():
        return options
    return replace(options, rerank=False, rerank_skipped_reason=REASON_RERANK_QUOTA_EXHAUSTED)
