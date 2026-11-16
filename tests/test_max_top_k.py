"""第 13 步 · T2：套餐级 ``max_top_k`` 是条数上限（回显接缝）。

四档套餐各有一个 ``max_top_k``：free 5 / plus 10 / pro 15 / max 20。
配置里的 `top_k` 超限时夹到套餐上限并回显；0 表示旧数据没这一档，退回平台硬上限。
"""

from __future__ import annotations

from app.core.config import settings
from app.models.plan import PLAN_CODES, PLAN_PRESETS
from app.services.profile_policy import resolve_retrieval_options
from app.services.retrieval_options import (
    MAX_TOP_K,
    RetrievalOptions,
    tenant_policy_echo,
)
from tests.test_retrieval_options import _policy  # noqa: F401 - 复用既有假配置构造器


def test_the_four_tiers_carry_the_agreed_max_top_k() -> None:
    """四档的 max_top_k 是 spec 拍过的数：free/plus/pro/max = 5/10/15/20。"""

    assert [PLAN_PRESETS[code].max_top_k for code in PLAN_CODES] == [5, 10, 15, 20]


def test_top_k_is_clamped_to_the_plan_cap_and_reported() -> None:
    """配置的 top_k 超套餐 cap → 夹到 cap + clipped 回显。"""

    from app.models.plan import PLAN_PRO
    from dataclasses import replace

    plan = replace(PLAN_PRESETS[PLAN_PRO], max_top_k=7)
    options = resolve_retrieval_options(
        policy=_policy(custom_options=(("top_k", 99),)),
        plan=plan,
        kb_count=1,
    )

    assert options.top_k == 7
    assert options.tenant_policy is not None
    assert "top_k" in options.tenant_policy.clipped


def test_a_fit_does_not_appear_in_clipped() -> None:
    """没超套餐 cap 时 clipped 为空，不给调用方假信号。"""

    from app.models.plan import PLAN_PRO
    from dataclasses import replace

    plan = replace(PLAN_PRESETS[PLAN_PRO], max_top_k=7)
    options = resolve_retrieval_options(
        policy=_policy(custom_options=(("top_k", 3),)),
        plan=plan,
        kb_count=1,
    )

    assert options.top_k == 3
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ()
