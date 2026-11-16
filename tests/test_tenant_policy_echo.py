"""第 13 步 · T2：`tenant_policy` 改报参数本身（回显接缝）。

后端不再有档位概念，回显不该再出现 ``requested_profile``；改写也不再由配置决定，
不该再出现 ``rewrite``。剩下的就是这次**实际生效的参数**与「被裁了什么」。
"""

from __future__ import annotations

from app.services.retrieval_options import (
    RetrievalOptions,
    TenantPolicy,
    tenant_policy_echo,
)


def test_echo_reports_the_parameters_not_a_profile_name() -> None:
    options = RetrievalOptions(
        mode="hybrid",
        top_k=8,
        fold_to_sections=True,
        expand_table_rows=True,
        rerank=True,
        tenant_policy=TenantPolicy(plan="pro", clipped=("top_k",)),
    )

    echo = tenant_policy_echo(options, kb_count=2)

    assert echo == {
        "plan": "pro",
        "mode": "hybrid",
        "top_k": 8,
        "rerank": True,
        "kb_count": 2,
        "clipped": ["top_k"],
    }
    assert "requested_profile" not in echo
    assert "rewrite" not in echo


def test_echo_is_none_without_a_gate_resolved_policy() -> None:
    options = RetrievalOptions(
        mode="vector",
        top_k=5,
        fold_to_sections=True,
        expand_table_rows=True,
        rerank=False,
    )

    assert tenant_policy_echo(options, kb_count=1) is None
