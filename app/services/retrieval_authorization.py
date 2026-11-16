"""检索能力的**新**授权与计量词表（第 13 步 · T1，expand 段）。

这一模块是 expand–contract 里的「新形式」：它把第 13 步拍板的词表、四档种子、
参数组合别名与旧配置搬迁规则**加在旁边**，旧的 ``PLAN_CODES`` / ``PLAN_PRESETS`` /
``PlanSpec`` / ``KeyPolicySnapshot`` 一个都不碰。T2 起把判定链接到这份词表上，
T8 contract 才删除旧形式。在此之前，本模块不参与任何运行时判定，因此升级后的
对外行为与改造前逐字节一致。

三件事分开，别混成一个「配置对象」：

* **授权位**（:data:`CAPABILITIES`）：回答「这把 Key 能不能用这条端点」，是布尔位；
* **计量账**（:data:`METER_BOOKS` 等）：回答「这次调用花掉了多少资源」，是计数器；
  三本账 × 两级（Key / 租户）× 两格（QPS / 日额）共 12 个计数器；
* **参数**（:class:`RetrievalParams`）：回答「这次用多好」，是三项直参数。后端从此
  不再有「档位」这个概念，``speed / balanced / quality`` 只是前端编辑器的填充按钮与
  文档/评测里的别名（:data:`PARAM_PRESETS`）。

**为什么 12 个计数器不按能力开户**：一次分解检索会跑 N 次内核、可能各重排一次、
再加一次大模型调用；按「端点」开户会造出一个虚构单位（「一次 decompose」），
而按资源开户，成本表里的每一行都能在链路上找到真实的对应动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# ───────────────────────────────────────────────────────────────── 授权位


#: 五个端点级授权位。``retrieve`` 是基线能力（仍显式给位：free 档只开它一个）。
#: 能力名只有这一处定义；判定、报错、自洽校验都从这里取，不在别处写第二份字面量。
CAPABILITIES: Final[tuple[str, ...]] = (
    "retrieve",
    "rewrite",
    "decompose",
    "research",
    "rerank",
)

#: 端点 → 它的授权位列名。``rerank`` 不在这里：它是质量位，不是一次调用对应的端点，
#: 由重排额度单独判（额度用尽 → 跳过重排 + 降级串，不拒绝整单）。
ENDPOINT_CAPABILITY: Final[dict[str, str]] = {
    "retrieve": "retrieve_allowed",
    "rewrite": "rewrite_allowed",
    "decompose": "decompose_allowed",
    "research": "research_allowed",
}


def ensure_endpoint_allowed(*, policy: Any, plan: Any, endpoint: str) -> None:
    """套餐与密钥配置**两层都得给**这个端点的位，否则 ``20013``。

    两层回答的是两个不同的问题（ADR-0018）：套餐说「这个租户买没买」，密钥配置说
    「这套集成要不要」。位为假是配置事实，答 ``FEATURE_NOT_ALLOWED``；额度用尽是
    另一回事（``QUOTA_EXCEEDED`` 20014），这里的门禁不碰计数、不把两者混成一个码。
    """

    bit = ENDPOINT_CAPABILITY.get(endpoint)
    if bit is None:
        raise ValueError(f"未知检索端点 {endpoint!r}，只支持 {sorted(ENDPOINT_CAPABILITY)}")
    if getattr(policy, bit, False) and getattr(plan, bit, False):
        return
    from app.core.exceptions import AppError, ErrorCode

    raise AppError(
        ErrorCode.FEATURE_NOT_ALLOWED,
        f"{endpoint} is not allowed (plan {getattr(plan, 'code', '?')!r}, "
        f"key policy {bit}={getattr(policy, bit, False)})",
    )


# ───────────────────────────────────────────────────────────────── 计量维度


#: 三本账：retrieve 记**内核调用次数**、rerank 记**重排调用次数**、llm 记**实际发生的**
#: 大模型调用次数。名字与既有 ``retrieve_qps`` / ``retrieve_daily`` 同形，便于对照。
METER_BOOKS: Final[tuple[str, ...]] = ("retrieve", "rerank", "llm")
#: 两级计数：Key 级卡住单个外部应用，租户级把同一租户下所有 Key 聚合起来。
METER_LEVELS: Final[tuple[str, ...]] = ("key", "tenant")
#: 两格：每秒与每日。
METER_WINDOWS: Final[tuple[str, ...]] = ("qps", "daily")


def meter_column_names() -> tuple[str, ...]:
    """6 个计数器列名（账本_窗口）；套餐表与密钥配置表**各存一份同名列**，共 12 个计数器。

    两级（Key / 租户）由所在表区分，不靠列名前缀——这与 ADR-0018 里
    ``retrieve_qps`` 在两张表同名同义的既有形状一致。顺序固定为「账本 → 窗口」，
    于是迁移、种子、面板、审计逐列对账时读的是同一份清单。
    """

    return tuple(f"{book}_{window}" for book in METER_BOOKS for window in METER_WINDOWS)


# ───────────────────────────────────────────────────────────────── 直参数


@dataclass(frozen=True)
class RetrievalParams:
    """密钥配置直接存的三项参数：检索方式、返回条数、是否重排。

    它是 frozen 的值对象：一次调用的参数在构造完就只读，链路里不存在「谁顺手改一下」
    的空子。``mode`` 是字符串而不是枚举——词表校验在平台层的门禁里做，本模块只承载值。
    """

    mode: str
    top_k: int
    rerank: bool


#: 旧档位名的**新身份**：只是三项参数的组合别名。它们不参与判定、不进响应、不上送
#: 后端（前端三个一键填充按钮与评测报告里的历史对比表用）。名字与顺序沿用旧词，
#: 是为了让历史评测报告仍能和今天的参数组合对得上，不是为了保留档位概念。
PARAM_PRESETS: Final[dict[str, RetrievalParams]] = {
    "speed": RetrievalParams(mode="vector", top_k=5, rerank=False),
    "balanced": RetrievalParams(mode="hybrid", top_k=5, rerank=False),
    "quality": RetrievalParams(mode="hybrid", top_k=5, rerank=True),
}

#: 合法的模式词表（搬迁时用它拦住 ``custom_options`` 里的错字；运行时门禁另有自己的
#: 词表校验，这里只为数据搬迁服务）。
_SUPPORTED_MODES: Final[frozenset[str]] = frozenset({"vector", "bm25", "hybrid"})
_MIN_TOP_K: Final[int] = 1


def migrate_legacy_params(
    *,
    profile: str | None,
    custom_options: dict,
    defaults: RetrievalParams,
) -> RetrievalParams:
    """把一条旧密钥配置搬成三项直参数（一次性数据搬迁用，纯函数）。

    * 命名档位：按 :data:`PARAM_PRESETS` **完整替换**，``custom_options`` 一个都不读；
    * ``custom``：逐键覆盖部署缺省，缺键 / 错值按缺键处理；
    * ``rewrite`` 键**丢弃**：改写已经是端点，不再是参数；
    * 未知或缺失档位：回落部署缺省。

    错值不许悄悄生效：``"false"`` 字符串不是布尔、负数不是条数、拼错的 mode 不是模式。
    这与旧 ``custom`` 档的收敛口径一致——配置里的一个错字不该冒充请求参数校验失败，
    但更不该把链路改成谁也没同意过的形状。
    """

    if profile in PARAM_PRESETS:
        return PARAM_PRESETS[profile]

    if profile != "custom":
        return defaults

    mode = defaults.mode
    raw_mode = custom_options.get("mode")
    if isinstance(raw_mode, str) and raw_mode in _SUPPORTED_MODES:
        mode = raw_mode

    top_k = defaults.top_k
    raw_top_k = custom_options.get("top_k")
    if isinstance(raw_top_k, int) and not isinstance(raw_top_k, bool) and raw_top_k >= _MIN_TOP_K:
        top_k = raw_top_k

    rerank = defaults.rerank
    raw_rerank = custom_options.get("rerank")
    if isinstance(raw_rerank, bool):
        rerank = raw_rerank

    return RetrievalParams(mode=mode, top_k=top_k, rerank=rerank)


# ───────────────────────────────────────────────────────────────── 四档种子


@dataclass(frozen=True)
class TierSeed:
    """一档套餐的种子快照（只在表为空时写入，运营改过的值不被覆盖）。

    字段与将落在 ``plans`` 上的新列一一对应：五个授权位收成一个 ``frozenset``
    （不是五个布尔字段——词表里的名字才是真相，别在两处各写一遍），六列计量额度按
    :func:`meter_column_names` 的顺序展开，再加一个 ``max_top_k``。
    """

    capabilities: frozenset[str]
    retrieve_qps: int
    retrieve_daily: int
    rerank_qps: int
    rerank_daily: int
    llm_qps: int
    llm_daily: int
    max_top_k: int


#: 四档套餐：free 是基线，plus = 1x、pro = 5x、max = 20x。数值来自第 13 步 spec，
#: 是**种子**不是常量——管理员在后台改的是库里的行，改完即时生效。
TIER_SEEDS: Final[dict[str, TierSeed]] = {
    "free": TierSeed(
        capabilities=frozenset({"retrieve"}),
        retrieve_qps=3,
        retrieve_daily=500,
        rerank_qps=0,
        rerank_daily=0,
        llm_qps=0,
        llm_daily=0,
        max_top_k=5,
    ),
    "plus": TierSeed(
        capabilities=frozenset(CAPABILITIES),
        retrieve_qps=10,
        retrieve_daily=5000,
        rerank_qps=3,
        rerank_daily=1000,
        llm_qps=1,
        llm_daily=300,
        max_top_k=10,
    ),
    "pro": TierSeed(
        capabilities=frozenset(CAPABILITIES),
        retrieve_qps=50,
        retrieve_daily=25000,
        rerank_qps=15,
        rerank_daily=5000,
        llm_qps=5,
        llm_daily=1500,
        max_top_k=15,
    ),
    "max": TierSeed(
        capabilities=frozenset(CAPABILITIES),
        retrieve_qps=200,
        retrieve_daily=100000,
        rerank_qps=60,
        rerank_daily=20000,
        llm_qps=20,
        llm_daily=6000,
        max_top_k=20,
    ),
}


__all__ = [
    "CAPABILITIES",
    "ENDPOINT_CAPABILITY",
    "ensure_endpoint_allowed",
    "METER_BOOKS",
    "METER_LEVELS",
    "METER_WINDOWS",
    "PARAM_PRESETS",
    "TIER_SEEDS",
    "RetrievalParams",
    "TierSeed",
    "meter_column_names",
    "migrate_legacy_params",
]
