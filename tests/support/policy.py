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

``custom`` 不是表里的一行：它表示「逐键覆盖部署缺省」，缺键取 vector / 5 / false。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.core.auth import DEV_KEY_POLICY
from app.models.key_policy import KeyPolicySnapshot

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
    "hybrid_allowed",
    "query_rewrite_allowed",
    "research_qps",
    "research_daily",
)


def params_of(
    *, profile: str = PROFILE_CUSTOM, custom_options: tuple[tuple[str, object], ...] = ()
) -> tuple[str, int, bool]:
    """把夹具词汇展开成 ``(mode, top_k, rerank)``；规则见模块文档。"""

    if profile in PARAM_PRESETS:
        return PARAM_PRESETS[profile]

    mode, top_k, rerank = ("vector", 5, False)
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
    }
    values.update(overrides)
    return dataclasses.replace(DEV_KEY_POLICY, **values)


__all__ = [
    "PARAM_PRESETS",
    "PROFILE_BALANCED",
    "PROFILE_CUSTOM",
    "PROFILE_QUALITY",
    "PROFILE_SPEED",
    "params_of",
    "policy",
]
