"""档位门禁与裁剪回显：一次检索「能用到多好」的**唯一**判定处（票 #78）。

这个模块是**平台层**，不是路由层：HTTP 与 MCP 两个入口各递一份「这次是谁、要查几个库」，
判定只在这里做一遍。放进路由的代价是两条入口迟早各长一套——HTTP 改了 MCP 没改，
测试又恰好只覆盖一边。

## 五道门，顺序不可换

```text
1 档位在套餐白名单        → 不在，20013
2 展开档位                → 命名档位查预设；custom 读手动参数
3 命名档位的能力门禁      → 越权，20013（不降级）
4 custom 裁剪             → 裁到套餐上限 + 平台硬上限，记下被裁字段
5 单次检索库数            → 超上限，20013（不裁剪）
```

顺序是判定的一部分：

* **先档位、后库数**：两处都越权时报的必须始终是同一条。反过来的话，同一次配置错误
  有时报「档位不允许」、有时报「库太多」，取决于这次顺手带了几 个库——排障只能靠猜。
* **先白名单、后门禁**：白名单（``app.services.data_scope``）是第六道关卡的第二道，
  由入口先调；它比本模块更早，因为「这些库是不是你的」连档位都不必展开。

## 两种超限反应刻意不同

| 情形 | 反应 | 为什么 |
| --- | --- | --- |
| 命名档位越权（套餐不再允许这一档，或撤了它需要的能力） | **拒绝**（``20013``） | 档位名是产品契约。降级沿用同名等于欺骗：客户以为还在跑 ``quality``，实际少了重排 |
| ``custom`` 档参数超套餐上限 | **裁剪 + 回显** | 上限要兜住，使用权不必剥夺；但**裁剪必须回显**，否则调用方以为一切照配置执行 |

这条非对称不是疏忽，是父 spec 与 ADR-0018 第 5 条的原话：一个改的是**名字的含义**，
另一个只是把没买够的额度夹到买得起的那一档。

## 档位意图 vs 部署能力

档位（以及 ``custom`` 的手动参数）表达的是**意图**；``RERANK_ENABLED`` /
``QUERY_REWRITE_ENABLED`` 表达的是**这台机器装没装**。生效值是两者的合取（落在
``retrieval_options.options_from_bundle``）：一个档位绝不能凭空变出一个部署没配置的
Provider——那会变成「回显说重排了、日志里一次模型都没调」，比直接不承诺更难查。
开关关着时，``quality`` 与 ``balanced`` 的链路参数逐项相同，行为与 #78 之前逐字节一致。

## 配置时自洽校验（:func:`ensure_policy_consistent`）

运行时不重复判「档位所需能力 ⊆ 配置开关 ⊆ 套餐能力」，那三层只在**创建配置时**判一次
（ADR-0018 第 2 条）：创建是一瞬间、有人盯着的动作，改一下就行；换成运行时失败，就是
客户的生产调用在午夜开始报错。运行时的五道门只复核**套餐**（管理员事后降级要立刻生效）。
"""

from __future__ import annotations

from dataclasses import replace

from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import (
    PROFILES,
    PROFILE_CUSTOM,
    KeyPolicySnapshot,
)
from app.models.plan import PlanSpec
from app.services.retrieval_options import (
    CUSTOM_OPTION_MODE,
    CUSTOM_OPTION_RERANK,
    CUSTOM_OPTION_REWRITE,
    MAX_TOP_K,
    MODE_HYBRID,
    TIER_PRESETS,
    RetrievalOptions,
    TenantPolicy,
    TierBundle,
    options_from_bundle,
    server_default_bundle,
    tier_bundle_of,
)

# 能力名：判定、报错文案与自洽校验共用同一套词。与 ``plans`` 表上那四个布尔列一一对应，
# 单独写成常量是因为它们要在**三种**载体之间传递（档位预设、配置开关、套餐能力），
# 任何一处拼错都会变成「看起来判过了」。
CAPABILITY_HYBRID: str = "hybrid"
CAPABILITY_RERANK: str = "rerank"
CAPABILITY_REWRITE: str = "rewrite"
CAPABILITY_RESEARCH: str = "research"

#: 裁剪只动这四个字段；``clipped`` 里的名字就是响应 ``tenant_policy`` 里的字段名，
#: 顺序固定（回显因此可断言、两条入口因此可逐字节比对）。
FIELD_MODE: str = "mode"
FIELD_TOP_K: str = "top_k"
FIELD_RERANK: str = "rerank"
FIELD_REWRITE: str = "rewrite"


def resolve_retrieval_options(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    kb_count: int,
    rerank_capable: bool | None = None,
    rewrite_capable: bool | None = None,
) -> RetrievalOptions:
    """五道门走完，给出这次调用**实际生效**的参数（含回显要用的那部分事实）。

    ``kb_count`` 是这次请求要查几个库——判定要看的是**数量**，不是哪些库；
    「哪些库」由白名单关卡（更早的一道）判。

    失败一律抛 ``AppError``：越权与超限是业务失败（ADR-0007），不是协议错误，也不是
    500。参数非法（配置里根本展开不出的东西）由写入时的自洽校验挡住，这里不重复判。
    """

    tier = policy.profile

    # 第 1 道：档位必须在套餐的白名单里。管理员把套餐调低之后，挂旧档位的 Key 下一次
    # 调用就撞在这里——不是「等缓存过期」，也不是「悄悄按新套餐降级跑」。
    if tier not in plan.allowed_profiles:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            f"tier {tier!r} is not allowed by plan {plan.code!r}",
        )

    # 第 2 道：展开。命名档位是完整替换，custom 才读手动参数（见 retrieval_options）。
    bundle = tier_bundle_of(policy)
    clipped: tuple[str, ...] = ()

    if tier == PROFILE_CUSTOM:
        # 第 4 道：custom 档超限只裁剪，不拒绝（平台兜住上限，但不剥夺使用权）。
        bundle, clipped = _clip_to_plan(bundle, plan)
    else:
        # 第 3 道：命名档位的能力门禁。**不降级**：名字是产品契约，少一步就是另一个档。
        _ensure_named_tier_granted(tier=tier, bundle=bundle, plan=plan)

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

    return options_from_bundle(
        policy=policy,
        bundle=bundle,
        # 意图 ∧ 本机能力：档位不凭空变出部署没配的 Provider。
        rerank_capable=rerank_capable,
        rewrite_capable=rewrite_capable,
        tenant_policy=TenantPolicy(plan=plan.code, requested_tier=tier, clipped=clipped),
    )


def ensure_policy_consistent(*, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
    """配置时自洽校验（ADR-0018 第 2 条）：不自洽的配置**根本不允许被创建**。

    三段逐级包含，缺一不可：

    ```text
    档位展开所需的能力 ⊆ 配置自己的能力开关 ⊆ 套餐允许的能力
    ```

    再加上数值那一半：配置的 QPS / 日配额不得超过套餐上限，授权的知识库数不得超过
    套餐的库数上限。

    为什么在创建时判而不是运行时判：创建是一瞬间、有人盯着的动作，报一次错改一下就好；
    留到运行时，就是客户的生产调用在无人改动的午夜突然开始失败——而失败原因藏在另一个
    人上周的一次改动里（ADR-0018「被否决的两种做法」第一条）。

    错误码用「参数 / 配置非法」（``20015``）：它不是「这次调用越权」，而是「这套配置本身
    不成立」，调用方改不了它，改它的是签发 Key 的那个人。

    ``custom`` 档只看**写下来**的手动参数：缺键意味着「跟部署缺省走」，而部署缺省是平台
    自己的选择——不该让租户的一次建配置动作因为平台另一次配置而失败（运行时那道
    「意图 ∧ 能力」的合取已经把这种情况兜住了）。
    """

    tier = policy.profile
    if tier not in PROFILES:
        raise AppError(ErrorCode.PARAM_ERROR, f"unknown tier: {tier!r}")

    if tier not in plan.allowed_profiles:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"tier {tier!r} is not allowed by plan {plan.code!r}",
        )

    # 第一段：档位所需能力 ⊆ 配置自己的能力开关。选了 quality 却关着重排，这套配置从
    # 落地那一刻就是自相矛盾的——它配置出来的 Key 每次调用都会撞在门禁上。
    for capability in sorted(_needed_capabilities(policy)):
        if not _policy_grants(policy, capability):
            raise AppError(
                ErrorCode.PARAM_ERROR,
                f"tier {tier!r} needs {capability!r} but the policy switch is off",
            )

    # 第二段：配置自己的能力开关 ⊆ 套餐允许的能力。配置只能在套餐上限内分配，不能凭空
    # 造能力——否则运行时门禁会先放行、再在下一次套餐复核时失败。
    for capability, granted in (
        (CAPABILITY_HYBRID, policy.hybrid_allowed),
        (CAPABILITY_RERANK, policy.rerank_allowed),
        (CAPABILITY_REWRITE, policy.query_rewrite_allowed),
        (CAPABILITY_RESEARCH, policy.research_allowed),
    ):
        if granted and not _plan_grants(plan, capability):
            raise AppError(
                ErrorCode.PARAM_ERROR,
                f"policy allows {capability!r} but plan {plan.code!r} does not",
            )

    # 数值同理。三条都写成「超过就拒绝」，与面板上那三个输入框一一对应。
    if policy.retrieve_qps > plan.retrieve_qps:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"policy qps {policy.retrieve_qps} > plan {plan.code!r} qps {plan.retrieve_qps}",
        )
    if policy.retrieve_daily > plan.retrieve_daily:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"policy daily {policy.retrieve_daily} > plan {plan.code!r} daily "
            f"{plan.retrieve_daily}",
        )
    if len(policy.allowed_kb_ids) > plan.max_kb:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"policy allows {len(policy.allowed_kb_ids)} knowledge bases > plan "
            f"{plan.code!r} max_kb {plan.max_kb}",
        )


def _ensure_named_tier_granted(*, tier: str, bundle: TierBundle, plan: PlanSpec) -> None:
    """命名档位需要的能力套餐给不了 → ``20013``。**不裁剪**：裁剪就换了一条链路。"""

    needed = _bundle_capabilities(bundle)
    for capability, allowed in (
        (CAPABILITY_HYBRID, plan.hybrid_allowed),
        (CAPABILITY_RERANK, plan.rerank_allowed),
        (CAPABILITY_REWRITE, plan.query_rewrite_allowed),
    ):
        if capability in needed and not allowed:
            raise AppError(
                ErrorCode.FEATURE_NOT_ALLOWED,
                f"tier {tier!r} needs {capability!r} but plan {plan.code!r} does not allow it",
            )


def _clip_to_plan(bundle: TierBundle, plan: PlanSpec) -> tuple[TierBundle, tuple[str, ...]]:
    """把 ``custom`` 档的链路裁到套餐上限与平台硬上限；返回裁剪后的链路与被裁字段。

    只动**没买够**的那几项，不改「想要什么」：``mode`` 从 ``hybrid`` 退到 ``vector``
    是因为套餐不给混合检索（没有别的模式能"部分"满足它）；``top_k`` 夹到
    ``MAX_TOP_K``（平台硬上限，套餐里没有这一列——条数不是成本旋钮）。
    """

    clipped: list[str] = []

    mode = bundle.mode
    if mode == MODE_HYBRID and not plan.hybrid_allowed:
        mode = "vector"
        clipped.append(FIELD_MODE)

    top_k = bundle.top_k
    if top_k > MAX_TOP_K:
        top_k = MAX_TOP_K
        clipped.append(FIELD_TOP_K)

    rerank = bundle.rerank
    if rerank and not plan.rerank_allowed:
        rerank = False
        clipped.append(FIELD_RERANK)

    rewrite = bundle.rewrite
    if rewrite and not plan.query_rewrite_allowed:
        rewrite = False
        clipped.append(FIELD_REWRITE)

    return replace(bundle, mode=mode, top_k=top_k, rerank=rerank, rewrite=rewrite), tuple(clipped)


def _bundle_capabilities(bundle: TierBundle) -> frozenset[str]:
    """一条链路**需要**哪些能力。``vector`` / ``bm25`` 不需要任何能力开关。"""

    needed: set[str] = set()
    if bundle.mode == MODE_HYBRID:
        needed.add(CAPABILITY_HYBRID)
    if bundle.rerank:
        needed.add(CAPABILITY_RERANK)
    if bundle.rewrite:
        needed.add(CAPABILITY_REWRITE)
    return frozenset(needed)


def _needed_capabilities(policy: KeyPolicySnapshot) -> frozenset[str]:
    """配置里那**一个**档位需要哪些能力。

    命名档位看预设；``custom`` 只看写下来的手动参数（缺键不算「需要」，理由见
    :func:`ensure_policy_consistent` 末段）。

    预设缺失时按部署缺省算，与 ``tier_bundle_of`` 的兜底同一条口径：一个刚被加进
    ``PROFILES`` 还没配预设的档位名，不该在这里炸成 ``KeyError``——那会让「建配置」这条
    低频路径以 500 的方式失败，而真正的信息只是「这个名字还没有预设」。
    """

    if policy.profile != PROFILE_CUSTOM:
        preset = TIER_PRESETS.get(policy.profile)
        return _bundle_capabilities(
            server_default_bundle() if preset is None else preset
        )

    manual = dict(policy.custom_options)
    needed: set[str] = set()
    if manual.get(CUSTOM_OPTION_MODE) == MODE_HYBRID:
        needed.add(CAPABILITY_HYBRID)
    if manual.get(CUSTOM_OPTION_RERANK) is True:
        needed.add(CAPABILITY_RERANK)
    if manual.get(CUSTOM_OPTION_REWRITE) is True:
        needed.add(CAPABILITY_REWRITE)
    return frozenset(needed)


def _policy_grants(policy: KeyPolicySnapshot, capability: str) -> bool:
    if capability == CAPABILITY_HYBRID:
        return policy.hybrid_allowed
    if capability == CAPABILITY_RERANK:
        return policy.rerank_allowed
    if capability == CAPABILITY_REWRITE:
        return policy.query_rewrite_allowed
    return policy.research_allowed


def _plan_grants(plan: PlanSpec, capability: str) -> bool:
    if capability == CAPABILITY_HYBRID:
        return plan.hybrid_allowed
    if capability == CAPABILITY_RERANK:
        return plan.rerank_allowed
    if capability == CAPABILITY_REWRITE:
        return plan.query_rewrite_allowed
    return plan.research_allowed
