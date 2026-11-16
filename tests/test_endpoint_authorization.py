"""第 13 步 · T2：端点级授权位（门禁接缝）。

四条检索/研究端点各对应一个布尔位；**套餐与密钥配置两层都得给**，
否则 ``20013``（FEATURE_NOT_ALLOWED）。位为假与额度用尽是两个不同的问题：
前者是配置事实（20013），后者由计数门禁给 20014，不许混成一个码。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.services.retrieval_authorization import (
    CAPABILITIES,
    ENDPOINT_CAPABILITY,
    ensure_endpoint_allowed,
)


class _Holder:
    """只带布尔位的替身：够让门禁读，不必造一套 ORM 行。"""

    def __init__(self, **bits: bool) -> None:
        for name in CAPABILITIES:
            setattr(self, f"{name}_allowed", True)
        for name, value in bits.items():
            setattr(self, f"{name}_allowed", value)


def test_each_endpoint_maps_to_exactly_one_capability_bit() -> None:
    assert set(ENDPOINT_CAPABILITY) == {"retrieve", "rewrite", "decompose", "research"}
    # 映射的值是**能力名**；列名由 CAPABILITY_COLUMNS 统一派生（一处词表）。
    assert set(ENDPOINT_CAPABILITY.values()) == {"retrieve", "rewrite", "decompose", "research"}
    # rerank 是质量位，不是端点位：它不通过这道门禁，而是由重排额度单独判。
    assert "rerank" not in ENDPOINT_CAPABILITY


@pytest.mark.parametrize("endpoint", ["retrieve", "rewrite", "decompose", "research"])
def test_both_layers_must_grant_the_bit(endpoint: str) -> None:
    """任何一层没开都拒绝；两层都开才放行。"""

    capability = ENDPOINT_CAPABILITY[endpoint]

    assert ensure_endpoint_allowed(
        policy=_Holder(), plan=_Holder(), endpoint=endpoint
    ) is None

    with pytest.raises(AppError) as denied:
        ensure_endpoint_allowed(
            policy=_Holder(**{capability: False}), plan=_Holder(), endpoint=endpoint
        )
    assert denied.value.code is ErrorCode.FEATURE_NOT_ALLOWED

    with pytest.raises(AppError) as denied:
        ensure_endpoint_allowed(
            policy=_Holder(), plan=_Holder(**{capability: False}), endpoint=endpoint
        )
    assert denied.value.code is ErrorCode.FEATURE_NOT_ALLOWED


def test_a_denied_endpoint_names_itself_in_the_message() -> None:
    with pytest.raises(AppError) as denied:
        ensure_endpoint_allowed(
            policy=_Holder(rewrite=False), plan=_Holder(), endpoint="rewrite"
        )

    assert "rewrite" in denied.value.msg


def test_an_unknown_endpoint_is_rejected_out_right() -> None:
    with pytest.raises(ValueError):
        ensure_endpoint_allowed(policy=_Holder(), plan=_Holder(), endpoint="nope")
