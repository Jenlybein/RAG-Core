"""套餐门禁：把「配置想要的参数」变成「这次调用实际生效的参数」，并给回显备好事实。

一条调用要过两道与套餐有关的判定：

1. **端点授权位**（五道位，见 ``app.services.retrieval_authorization``）：由各端点的
   组合根在调用本模块之前先判（``ensure_endpoint_allowed``）。
2. **参数与额度**（本模块）：``top_k`` 超套餐封顶就**裁剪 + 回显**；数值额度与单次库数
   超限就**拒绝**（``20013``）。

**第 13 步起这里没有「档位」**（ADR-0032）：不再有档位白名单、不再有「命名档位所需
能力」这一层、也不再按档位名裁剪 ``mode`` / ``rerank``。三项参数是存下来的事实，本模块
只做两件与套餐有关的事——按 ``max_top_k`` 夹条数、按授权位折重排的生效值。

## 生效值 = 意图 ∧ 授权位 ∧ 本机能力

``rerank`` 的生效值由三件事合取，只在 ``resolve_retrieval_options`` 一处实现：
配置里写了要重排（意图）、套餐与配置都给 ``rerank`` 位（授权）、这台机器真的装了重排
端点（能力）。少折一层就会出现「回显说重排了、日志里一次模型都没调」——比直接不承诺
更难查。

## 配置时自洽校验（:func:`ensure_policy_consistent`）

运行时只复核**套餐**（管理员事后降级要立刻生效），而「这套配置本身成不成立」只在
**创建 / 修改配置时**判一次（ADR-0018 第 2 条）：创建是一瞬间、有人盯着的动作，报一次
错改一下就好；留到运行时，就是客户的生产调用在午夜开始报错。

## 能力词汇表只有一个

能力名与「它落在哪个布尔列上」的对应关系只在
``app.services.retrieval_authorization`` 一处声明；本模块从那里取，不写第二份。
"""

from __future__ import annotations

from typing import Final

from app.core.config import SUPPORTED_RETRIEVAL_MODES
from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PlanSpec
from app.providers.rerank import rerank_capability
from app.services.quota_gate import METER_BOOKS
from app.services.retrieval_authorization import CAPABILITIES, CAPABILITY_COLUMNS
from app.services.retrieval_options import (
    MAX_TOP_K,
    RetrievalOptions,
    RetrievalParams,
    TenantPolicy,
    options_from_params,
    retrieval_params_of,
)

#: 裁剪只剩**一个**来源：条数。``clipped`` 里的名字就是响应 ``tenant_policy`` 里的
#: 字段名（``top_k``），与生效值同源。
FIELD_TOP_K: Final[str] = "top_k"


def resolve_retrieval_options(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    kb_count: int,
    rerank_capable: bool | None = None,
) -> RetrievalOptions:
    """两道与套餐有关的判定走完，给出这次调用**实际生效**的参数与回显事实。

    ``kb_count`` 是这次请求要查几个库——判定要看的是**数量**，不是哪些库；
    「哪些库」由白名单关卡（更早的一道）判。

    失败一律抛 ``AppError``：越权与超限是业务失败（ADR-0007），不是协议错误，也不是
    500。参数非法（配置里根本读不出的东西）由写入时的自洽校验挡住，这里不重复判。
    """

    params, clipped = _clip_top_k(retrieval_params_of(policy), plan)

    # 第 5 道：单次检索库数。拒绝而不是裁剪——把 3 个库悄悄截成 1 个，改的是「查什么」，
    # 调用方会拿到一份看起来完整、其实范围少了一半的答案（作用域不许被静默改写）。
    #
    # 与白名单那道关卡（``app.services.data_scope.ensure_kb_scope``）是两件事，都判「库」
    # 但判的不是一回事：那道判**这些库是不是你的**（答「不是」用 20008，连存在性都不能
    # 泄露），这道判**一次能问几个**（答「太多」用 20013，调用方可以拆成两次再问）。
    if kb_count > plan.max_kb_per_retrieve:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            f"too many knowledge bases in one retrieve: {kb_count} > "
            f"{plan.max_kb_per_retrieve} (plan {plan.code!r})",
        )

    # 意图 ∧ 授权位 ∧ 本机能力：三者缺一，重排就不该跑，回显也不该说跑了。
    capable = rerank_capability() if rerank_capable is None else rerank_capable
    rerank_authorized = policy.rerank_allowed and plan.rerank_allowed

    return options_from_params(
        policy=policy,
        params=params,
        rerank_capable=capable and rerank_authorized,
        tenant_policy=TenantPolicy(plan=plan.code, clipped=clipped),
    )


def ensure_policy_consistent(*, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
    """配置时自洽校验（ADR-0018 第 2 条）：不自洽的配置**根本不允许被创建**。

    两条逐级包含，缺一不可：

    ```text
    配置自己的授权位 ⊆ 套餐允许的授权位
    配置的参数与额度      ⊆ 套餐上限（模式在词表内、重排要有位、额度不超、库数不超）
    ```

    为什么在创建时判而不是运行时判：创建是一瞬间、有人盯着的动作，报一次错改一下就好；
    留到运行时，就是客户的生产调用在无人改动的午夜突然开始失败——而失败原因藏在另一个
    人上周的一次改动里（ADR-0018「被否决的两种做法」第一条）。

    错误码用「参数 / 配置非法」（``20015``）：它不是「这次调用越权」，而是「这套配置本身
    不成立」，调用方改不了它，改它的是签发 Key 的那个人。
    """

    # 第一段：配置的授权位 ⊆ 套餐的授权位。配置只能在套餐上限内分配，不能凭空造能力
    # ——否则运行时门禁会先放行、再在下一次套餐复核时失败。
    for capability in CAPABILITIES:
        column = CAPABILITY_COLUMNS[capability]
        if getattr(policy, column) and not getattr(plan, column):
            raise AppError(
                ErrorCode.PARAM_ERROR,
                f"policy allows {capability!r} but plan {plan.code!r} does not",
            )

    # 第二段：参数本身要成立。模式在词表内（拼错一个字母不该冒充请求校验失败，但更不该
    # 把链路改成谁也没同意过的形状）；要重排就得有重排位，否则每次调用都会静默跳过。
    if policy.retrieval_mode not in SUPPORTED_RETRIEVAL_MODES:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"unknown retrieval mode {policy.retrieval_mode!r}，"
            f"可选 {sorted(SUPPORTED_RETRIEVAL_MODES)}",
        )
    if policy.retrieval_rerank and not policy.rerank_allowed:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            "policy wants rerank but the rerank authorization bit is off",
        )

    # 第三段：数值不得超过套餐。三本账各两格，逐对判——写成「超过就拒绝」，与面板上
    # 那几个输入框一一对应。加工能力的两本账允许为 0（= 这套集成不用它），所以判据是
    # 「大于」，不是「≤ 0 即拒绝」。账本名字从词表取，不写第二份。
    for book in METER_BOOKS:
        for window in ("qps", "daily"):
            column = f"{book}_{window}"
            granted = getattr(policy, column)
            limit = getattr(plan, column)
            if granted > limit:
                raise AppError(
                    ErrorCode.PARAM_ERROR,
                    f"policy {column} {granted} > plan {plan.code!r} {column} {limit}",
                )

    if len(policy.allowed_kb_ids) > plan.max_kb:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"policy allows {len(policy.allowed_kb_ids)} knowledge bases > plan "
            f"{plan.code!r} max_kb {plan.max_kb}",
        )


def _clip_top_k(params: RetrievalParams, plan: PlanSpec) -> tuple[RetrievalParams, tuple[str, ...]]:
    """把条数夹到套餐封顶与平台硬上限；返回夹过的参数与被裁字段名。

    **这是唯一的裁剪来源。** 先夹平台硬上限，再夹套餐的 ``max_top_k``（每档的质量封顶）；
    ``max_top_k=0`` 表示这一档不额外封顶，退回平台硬上限。

    只夹条数，不改模式、不改重排：那两个是授权位与部署能力的事——位为假是 ``20013``，
    不是悄悄降级。裁剪而不回显等于欺骗（调用方要 70 条、拿到 5 条，响应里没有一处说明），
    所以被夹时一定把 ``top_k`` 记进 ``clipped``。
    """

    cap = min(MAX_TOP_K, plan.max_top_k) if plan.max_top_k > 0 else MAX_TOP_K
    if params.top_k <= cap:
        return params, ()

    # frozen 值对象：造一个新的，不改入参（调用方可能还持有它）。
    return (
        RetrievalParams(mode=params.mode, top_k=cap, rerank=params.rerank),
        (FIELD_TOP_K,),
    )
