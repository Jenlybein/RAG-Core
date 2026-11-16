"""第 13 步 · T8（契约）：三项参数直存，`profile` 不再是判定的输入。

后端不再有「档位」：一次调用用多好，完全由密钥配置里直存的
``mode`` / ``top_k`` / ``rerank`` 三项参数决定。``profile`` 字段可能还在库里（兼容旧缓存），
但**改它不该改变任何检索行为**——这正是本文件要钉死的性质。
"""

from __future__ import annotations

import pytest

from app.models.key_policy import (
    KeyPolicySnapshot,
)
from app.services.retrieval_options import RetrievalParams, retrieval_params_of
from tests.support.policy import PROFILE_BALANCED, PROFILE_QUALITY, PROFILE_SPEED


def _policy(**overrides: object) -> KeyPolicySnapshot:
    base: dict[str, object] = dict(
        allowed_kb_ids=("kb-a",),
        research_allowed=True,
        hybrid_allowed=True,
        rerank_allowed=True,
        query_rewrite_allowed=False,
        retrieve_qps=10,
        retrieve_daily=1000,
        research_qps=1,
        research_daily=100,
        profile=PROFILE_SPEED,
        include_parent=True,
        expand_table_rows=True,
        # 三项参数直存：mode/top_k/rerank。
        custom_options=(("mode", "hybrid"), ("top_k", 9), ("rerank", True)),
    )
    base.update(overrides)
    return KeyPolicySnapshot(**base)  # type: ignore[arg-type]


def test_parameters_are_read_directly_from_the_policy() -> None:
    """三项参数直读值，不按 profile 展开预设。"""

    chain = retrieval_params_of(_policy())

    assert chain == RetrievalParams(mode="hybrid", top_k=9, rerank=True, rewrite=False)


def test_changing_the_profile_name_changes_nothing() -> None:
    """改档位名不改变一次调用的参数——名字已经不是 truth。"""

    speed = retrieval_params_of(_policy(profile=PROFILE_SPEED))
    quality = retrieval_params_of(_policy(profile=PROFILE_QUALITY))
    balanced = retrieval_params_of(_policy(profile=PROFILE_BALANCED))

    assert speed == quality == balanced


def test_missing_keys_fall_back_to_the_deployment_defaults() -> None:
    """缺键逐键回落部署缺省（不是回落到某个档位预设）。"""

    chain = retrieval_params_of(_policy(custom_options=()))

    assert chain.mode == "hybrid"  # settings.RETRIEVAL_MODE 的缺省
    assert chain.top_k == 5  # settings.TOP_K 的缺省


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"mode": "not-a-mode", "top_k": 3, "rerank": True}, "hybrid"),
        ({"mode": "bm25", "top_k": -1, "rerank": True}, "bm25"),
        ({"mode": "bm25", "top_k": 3, "rerank": "false"}, "bm25"),
    ],
)
def test_bad_values_do_not_silently_apply(raw: dict, expected: str) -> None:
    """错值按缺键处理，不把链路改成谁也没同意过的形状。"""

    chain = retrieval_params_of(_policy(custom_options=tuple(sorted(raw.items()))))

    assert chain.mode == expected
