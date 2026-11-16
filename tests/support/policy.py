"""测试侧构造密钥配置快照的**唯一**入口。

生产里没有「档位」这个概念（ADR-0032）：三项参数直存（``retrieval_mode`` /
``retrieval_top_k`` / ``retrieval_rerank``），五个授权位各自独立。但用例想表达的往往是
**意图**——「我要一条 quality 那样的链路」。逐条写三元组会让几百处夹具各抄一遍同样的
三个值，抄错一个是静默的。

所以翻译只在这里做一次：``profile=`` / ``custom_options=`` 是**夹具词汇**，等价于
「按这张表展开成三项参数」。它和后端行为无关——``app/`` 里没有任何模块 import 本文件，
删掉它不影响任何运行时判定。这也是为什么它可以保留旧词：它是一个测试构造器，不是一条
兼容分支。

表的取值与第 13 步之前 ``profile_presets`` 表种下的三行逐项一致，所以「旧用例想说的是
哪种链路」与「今天的三项参数」是一一对应的：

* ``speed``    → vector / 5 / false
* ``balanced`` → hybrid / 5 / false
* ``quality``  → hybrid / 5 / true

``custom`` 不是表里的一行：它表示「逐键覆盖**部署缺省**」，缺键取当前部署配置
（``RETRIEVAL_MODE`` / ``TOP_K`` / 不重排），与旧语义「缺键跟部署走」一致。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.core.auth import DEV_KEY_POLICY
from app.core.config import settings
from app.models.key_policy import KeyPolicy, KeyPolicySnapshot

PROFILE_SPEED: str = "speed"
PROFILE_BALANCED: str = "balanced"
PROFILE_QUALITY: str = "quality"
PROFILE_CUSTOM: str = "custom"

#: 三个参数组合别名（spec #184：它们只剩「前端填充按钮 + 文档/评测别名」两处身份）。
PARAM_PRESETS: dict[str, tuple[str, int, bool]] = {
    PROFILE_SPEED: ("vector", 5, False),
    PROFILE_BALANCED: ("hybrid", 5, False),
    PROFILE_QUALITY: ("hybrid", 5, True),
}


#: 已被迁移删除的列名：夹具可以继续传，构造器会丢掉（理由见 :func:`policy`）。
RETIRED_FIELDS: tuple[str, ...] = (
    "profile",
    "custom_options",
    "hybrid_allowed",
    "query_rewrite_allowed",
    "research_qps",
    "research_daily",
)


def params_of(
    *, profile: str = PROFILE_CUSTOM, custom_options: tuple[tuple[str, object], ...] = ()
) -> tuple[str, int, bool]:
    """把夹具词汇展开成 ``(mode, top_k, rerank)``。

    **显式给的参数永远赢过档位名**：第 13 步之后三项参数才是真相，档位名只是一个「打底
    用的组合别名」。所以命名档位先按表铺一层底，``custom_options`` 里出现的键再逐键覆盖
    ——这与生产一致（生产连档位名都没有，参数就是全部）。

    ``custom``（以及表里没有的名字）的底是**部署缺省**（``RETRIEVAL_MODE`` / ``TOP_K`` /
    不重排），而不是写死的 ``vector``：旧语义里「缺键」就是「跟部署配置走」，写成字面量会
    让「本地 vs CI 跑的不是同一条链路」。
    """

    base = PARAM_PRESETS.get(profile)
    if base is None:
        mode, top_k, rerank = (settings.RETRIEVAL_MODE, settings.TOP_K, False)
    else:
        mode, top_k, rerank = base
    raw = dict(custom_options)
    if raw.get("mode") in ("vector", "bm25", "hybrid"):
        mode = str(raw["mode"])
    value = raw.get("top_k")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        top_k = value
    flag = raw.get("rerank")
    if isinstance(flag, bool):
        rerank = flag
    # ``custom_options.rewrite`` 刻意忽略：改写已是端点，不再是参数（spec 现状）。
    return mode, top_k, rerank


def policy(
    *,
    profile: str = PROFILE_CUSTOM,
    custom_options: tuple[tuple[str, object], ...] = (),
    include_parent: bool = True,
    expand_table_rows: bool = True,
    retrieve_allowed: bool = True,
    rewrite_allowed: bool = True,
    decompose_allowed: bool = True,
    research_allowed: bool = False,
    rerank_allowed: bool = True,
    retrieve_qps: int = 1,
    retrieve_daily: int = 100,
    rerank_qps: int = 0,
    rerank_daily: int = 0,
    llm_qps: int = 0,
    llm_daily: int = 0,
    allowed_kb_ids: tuple[str, ...] = ("kb-1",),
    #: 真实配置行永远是**受限**的（``policy_snapshot_of`` 不映射这个标记，它默认 False）。
    #: 只有内置开发身份那份是 ``True``；夹具默认跟真实行一致，免得「白名单关卡」在用例里
    #: 被一个上游缺省悄悄关掉。
    unrestricted_scope: bool = False,
    **overrides: Any,
) -> KeyPolicySnapshot:
    """一份只关心参数与授权位的快照；其余字段取内置开发身份那份。

    ``**overrides`` 透传给 ``dataclasses.replace``，让个别用例能一次性覆盖任何字段
    （例如把 ``retrieval_top_k`` 顶到上限之外去测裁剪）。

    **已退役的列名在这里被丢掉**（``hybrid_allowed`` / ``query_rewrite_allowed`` /
    ``research_qps`` / ``research_daily``）：它们对应的列已被迁移删除，而仍有用例按旧
    词汇传它们。丢掉而不是报错，是因为「传了但没有这一列」在这层等价于「这个开关不再
    存在」——真正关心那件事的用例会去看行为，而行为断言必须按新契约写（这正是本次
    改造要区分开的东西）。
    """

    mode, top_k, rerank = params_of(profile=profile, custom_options=custom_options)
    for retired in RETIRED_FIELDS:
        overrides.pop(retired, None)
    values: dict[str, Any] = {
        "include_parent": include_parent,
        "expand_table_rows": expand_table_rows,
        "retrieve_allowed": retrieve_allowed,
        "rewrite_allowed": rewrite_allowed,
        "decompose_allowed": decompose_allowed,
        "research_allowed": research_allowed,
        "rerank_allowed": rerank_allowed,
        "retrieve_qps": retrieve_qps,
        "retrieve_daily": retrieve_daily,
        "rerank_qps": rerank_qps,
        "rerank_daily": rerank_daily,
        "llm_qps": llm_qps,
        "llm_daily": llm_daily,
        "allowed_kb_ids": allowed_kb_ids,
        "retrieval_mode": mode,
        "retrieval_top_k": top_k,
        "retrieval_rerank": rerank,
        "unrestricted_scope": unrestricted_scope,
    }
    values.update(overrides)
    return dataclasses.replace(DEV_KEY_POLICY, **values)


def policy_row(
    *,
    policy_id: str = "policy-1",
    tenant_id: str = "tenant_demo",
    name: str = "默认配置",
    profile: str = PROFILE_CUSTOM,
    custom_options: tuple[tuple[str, object], ...] = (),
    include_parent: bool = True,
    expand_table_rows: bool = True,
    retrieve_allowed: bool = True,
    rewrite_allowed: bool = False,
    decompose_allowed: bool = False,
    research_allowed: bool = False,
    rerank_allowed: bool = False,
    retrieve_qps: int = 1,
    retrieve_daily: int = 100,
    rerank_qps: int = 0,
    rerank_daily: int = 0,
    llm_qps: int = 0,
    llm_daily: int = 0,
    allowed_kb_ids: tuple[str, ...] = ("kb-1",),
    **overrides: Any,
) -> KeyPolicy:
    """一份密钥配置**行**（列的形状）；夹具词汇与 :func:`policy` 相同。"""

    mode, top_k, rerank = params_of(profile=profile, custom_options=custom_options)
    for retired in RETIRED_FIELDS:
        overrides.pop(retired, None)
    values: dict[str, Any] = {
        "id": policy_id,
        "tenant_id": tenant_id,
        "name": name,
        "allowed_kb_ids": list(allowed_kb_ids),
        "retrieve_allowed": retrieve_allowed,
        "rewrite_allowed": rewrite_allowed,
        "decompose_allowed": decompose_allowed,
        "research_allowed": research_allowed,
        "rerank_allowed": rerank_allowed,
        "retrieve_qps": retrieve_qps,
        "retrieve_daily": retrieve_daily,
        "rerank_qps": rerank_qps,
        "rerank_daily": rerank_daily,
        "llm_qps": llm_qps,
        "llm_daily": llm_daily,
        "retrieval_mode": mode,
        "retrieval_top_k": top_k,
        "retrieval_rerank": rerank,
        "include_parent": include_parent,
        "expand_table_rows": expand_table_rows,
    }
    values.update(overrides)
    return KeyPolicy(**values)


__all__ = [
    "PARAM_PRESETS",
    "PROFILE_BALANCED",
    "PROFILE_CUSTOM",
    "PROFILE_QUALITY",
    "PROFILE_SPEED",
    "RETIRED_FIELDS",
    "params_of",
    "policy",
    "policy_row",
]
