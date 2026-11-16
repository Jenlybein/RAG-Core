"""检索能力的授权词表：五个布尔位，以及「端点 → 位」的对应关系。

这是**唯一**一处声明「有哪些能力」的地方：门禁、报错、自洽校验、控制面面板、四档种子
都从这里取，任何别处出现的 ``"retrieve"`` / ``"rewrite"`` 字面量都算重复定义。

三件事必须分开，别混成一个「配置对象」（ADR-0032）：

* **授权位**（本模块）：回答「这把 Key 能不能用这条端点」，是布尔位；
* **计量账**（``app.services.quota_gate``）：回答「这次调用花了多少资源」，是计数器；
* **参数**（``app.services.retrieval_options``）：回答「这次用多好」，是三项直参数。

**为什么记账不按端点开户**：一次分解检索会跑 N 次内核、可能各重排一次、再加一次大模型
调用；按「端点」开户会造出一个虚构单位（「一次 decompose」），而按资源开户，成本表里的
每一行都能在链路上找到真实的对应动作。
"""

from __future__ import annotations

from typing import Any, Final

# ───────────────────────────────────────────────────────────────── 授权位


#: 五个端点级授权位。``retrieve`` 是基线能力（仍显式给位：free 档只开它一个）。
#: 顺序即控制面面板与四档种子的列顺序，不参与任何判定。
CAPABILITIES: Final[tuple[str, ...]] = (
    "retrieve",
    "rewrite",
    "decompose",
    "research",
    "rerank",
)

#: 能力名 → 布尔列名。``KeyPolicySnapshot`` 与 :class:`app.models.plan.PlanSpec` 上
#: 这五列同名同义，所以一份映射就够：判定不再区分「问配置」还是「问套餐」。
CAPABILITY_COLUMNS: Final[dict[str, str]] = {name: f"{name}_allowed" for name in CAPABILITIES}

#: 端点 → 它的授权位名。``rerank`` 不在这里：它是质量位，不是一次调用对应的端点，
#: 由「重排额度 + 生效值合取」单独处理（额度用尽 → 跳过重排 + 降级串，不拒绝整单）。
ENDPOINT_CAPABILITY: Final[dict[str, str]] = {
    "retrieve": "retrieve",
    "rewrite": "rewrite",
    "decompose": "decompose",
    "research": "research",
}


def ensure_endpoint_allowed(*, policy: Any, plan: Any, endpoint: str) -> None:
    """套餐与密钥配置**两层都得给**这个端点的位，否则 ``20013``。

    两层回答的是两个不同的问题（ADR-0018）：套餐说「这个租户买没买」，密钥配置说
    「这套集成要不要」。位为假是配置事实，答 ``FEATURE_NOT_ALLOWED``；额度用尽是
    另一回事（``QUOTA_EXCEEDED`` 20014），这里的门禁不碰计数、不把两者混成一个码。
    """

    capability = ENDPOINT_CAPABILITY.get(endpoint)
    if capability is None:
        raise ValueError(f"未知检索端点 {endpoint!r}，只支持 {sorted(ENDPOINT_CAPABILITY)}")
    column = CAPABILITY_COLUMNS[capability]
    if getattr(policy, column, False) and getattr(plan, column, False):
        return
    from app.core.exceptions import AppError, ErrorCode

    raise AppError(
        ErrorCode.FEATURE_NOT_ALLOWED,
        f"{endpoint} is not allowed (plan {getattr(plan, 'code', '?')!r}, "
        f"key policy {column}={getattr(policy, column, False)})",
    )


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_COLUMNS",
    "ENDPOINT_CAPABILITY",
    "ensure_endpoint_allowed",
]
