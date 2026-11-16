"""第 13 步 · T8（契约）：三项参数直存，`profile` 不再是判定的输入。

后端不再有「档位」：一次调用用多好，完全由密钥配置里直存的
``mode`` / ``top_k`` / ``rerank`` 三项参数决定。``profile`` 列已被迁移删除，夹具词汇里
它只剩「组合别名」的身份——**换别名不该改变任何检索行为**，这正是本文件要钉死的性质。
"""

from __future__ import annotations

import pytest

from app.models.key_policy import (
    KeyPolicySnapshot,
)
from app.services.retrieval_options import RetrievalParams, retrieval_params_of
from tests.support.policy import PROFILE_BALANCED, PROFILE_QUALITY, PROFILE_SPEED
from tests.support.policy import policy as support_policy


def _policy(**overrides: object) -> KeyPolicySnapshot:
    """一份只关心三项参数与授权位的快照；夹具词汇由 tests.support 翻译。

    文件默认值就是「一条 hybrid / 9 条 / 要重排」的链路——本文件测的是「参数直读」，
    所以默认值本身必须三项都非缺省，才能与「按档位展开」区分开。
    """

    values: dict[str, object] = {
        "custom_options": (("mode", "hybrid"), ("top_k", 9), ("rerank", True))
    }
    values.update(overrides)
    return support_policy(**values)


def test_parameters_are_read_directly_from_the_policy() -> None:
    """三项参数直读值，不按 profile 展开预设。"""

    chain = retrieval_params_of(_policy())

    assert chain == RetrievalParams(mode="hybrid", top_k=9, rerank=True)


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
