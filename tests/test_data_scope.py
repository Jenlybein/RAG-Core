"""票 #76 数据范围关卡（六道关卡的第二道）：``kb_ids ⊆ allowed_kb_ids``。

接缝是**纯函数**：``app.services.data_scope`` 只认「一份密钥配置快照 + 一串库标识」，
不碰数据库、不碰请求对象。两个入口（HTTP 与 MCP）共用它，规则只有这一份。

三条语义在这里钉死，因为它们都是「一旦理解错就变成泄露」的那类：

* 白名单外的库与跨租户的库**同一个码**（``20008``）——可区分的「无权限」等于告诉攻击者
  这个资源存在、只是不归你；
* 真实配置的白名单为空**什么都不许**——空列表一旦被读成「全部」，清空白名单就成了开闸；
* 内置开发身份**不做范围收窄**，但那是快照上一个显式布尔（``unrestricted_scope``）
  说的，**不是**靠一条空列表兼职表达两件事。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import (
    PROFILE_SPEED,
    KeyPolicy,
    KeyPolicySnapshot,
    policy_snapshot_of,
)


def _policy(allowed: tuple[str, ...] = ("kb-a", "kb-b")) -> KeyPolicySnapshot:
    """一套「真实」密钥配置（白名单受限）。字段值抄自一处手工构造的期望，不是再算一遍。"""

    return KeyPolicySnapshot(
        allowed_kb_ids=allowed,
        research_allowed=False,
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        orchestration_allowed=False,
        retrieve_qps=5,
        retrieve_daily=50,
        research_qps=0,
        research_daily=0,
        profile=PROFILE_SPEED,
        include_parent=True,
        expand_table_rows=True,
    )


def _dev_policy() -> KeyPolicySnapshot:
    """内置开发身份的那一份：白名单空着，但显式声明不做范围收窄。"""

    from app.core.auth import DEV_KEY_POLICY

    return DEV_KEY_POLICY


# ───────────────────────────────────────────── 白名单：范围内放行、范围外「不存在」


def test_a_real_policy_lets_whitelisted_knowledge_bases_through() -> None:
    from app.services.data_scope import ensure_kb_scope

    ensure_kb_scope(policy=_policy(), kb_ids=["kb-a", "kb-b"])


def test_an_out_of_whitelist_id_is_reported_as_not_found() -> None:
    """越界必须是「知识库不存在」（20008），不是「无权限」（20011）。"""

    from app.services.data_scope import ensure_kb_scope

    with pytest.raises(AppError) as excinfo:
        ensure_kb_scope(policy=_policy(), kb_ids=["kb-a", "kb-other"])

    assert excinfo.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert excinfo.value.code is not ErrorCode.FORBIDDEN


def test_one_bad_id_rejects_the_whole_request() -> None:
    """``kb_ids`` 是「这次要查哪些库」的一整句话：有一个越界就整句不成立。

    不许「把越界的那个悄悄摘掉、剩下的照查」——调用方会拿到一份缺了库的结果，
    却看不出是被拦了。
    """

    from app.services.data_scope import ensure_kb_scope

    with pytest.raises(AppError) as excinfo:
        ensure_kb_scope(policy=_policy(), kb_ids=["kb-b", "kb-other", "kb-third"])

    assert excinfo.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


# ───────────────────────────────────────────── 空白名单：失败关闭


def test_an_empty_whitelist_denies_everything() -> None:
    """真实配置的白名单为空 = 什么都不许（失败关闭），**不是**「全部」。"""

    from app.services.data_scope import ensure_kb_scope

    with pytest.raises(AppError) as excinfo:
        ensure_kb_scope(policy=_policy(allowed=()), kb_ids=["kb-a"])

    assert excinfo.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_an_empty_whitelist_also_denies_an_empty_request_list() -> None:
    """连「一个库都不查」也不放行：空列表按空结果处理会把判定变成恒真。

    请求模型本来就不接受空的 ``kb_ids``（``min_length=1``），这里守的是**函数本身**
    不出现「空输入恒通过」这个洞——它在 MCP 入口与内部调用上同样可被调到。
    """

    from app.services.data_scope import ensure_kb_scope

    with pytest.raises(AppError) as excinfo:
        ensure_kb_scope(policy=_policy(allowed=()), kb_ids=[])

    assert excinfo.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


# ───────────────────────────────────────────── 内置开发身份：显式例外


def test_the_development_identity_is_not_scope_restricted() -> None:
    """开发身份的 ``allowed_kb_ids`` 是空的，但本地开发与既有 734 条测试必须照常跑。"""

    from app.services.data_scope import ensure_kb_scope

    policy = _dev_policy()
    assert policy.allowed_kb_ids == (), "开发身份没有白名单可言"
    assert policy.unrestricted_scope is True, "例外靠显式标记，不靠空列表表达两种意思"

    ensure_kb_scope(policy=policy, kb_ids=["kb-anything", "kb-else"])


def test_the_exception_marker_defaults_to_restricted() -> None:
    """标记默认「受限」：任何忘记写它的构造点都落在安全的那一侧。"""

    assert _policy().unrestricted_scope is False


def test_a_policy_row_maps_to_a_restricted_snapshot() -> None:
    """从库里读出来的配置**永远**是受限的：只有合成出来的开发快照能豁免。"""

    row = KeyPolicy(
        id="22222222-2222-4222-8222-222222222222",
        tenant_id="tenant_acme",
        name="console",
        allowed_kb_ids=["kb-a"],
        research_allowed=False,
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=5,
        retrieve_daily=50,
        research_qps=0,
        research_daily=0,
        profile=PROFILE_SPEED,
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )

    snapshot = policy_snapshot_of(row)

    assert snapshot.allowed_kb_ids == ("kb-a",)
    assert snapshot.unrestricted_scope is False, "真实配置不许靠映射函数顺手豁免"


# ───────────────────────────────────────────── 列库：同一套范围，用来过滤而不是抛错


def test_scoped_kb_ids_drops_what_the_key_cannot_see() -> None:
    from app.services.data_scope import scoped_kb_ids

    assert scoped_kb_ids(policy=_policy(), kb_ids=["kb-a", "kb-x", "kb-b"]) == [
        "kb-a",
        "kb-b",
    ]


def test_scoped_kb_ids_hides_everything_under_an_empty_whitelist() -> None:
    from app.services.data_scope import scoped_kb_ids

    assert scoped_kb_ids(policy=_policy(allowed=()), kb_ids=["kb-a"]) == []


def test_scoped_kb_ids_keeps_everything_for_the_development_identity() -> None:
    from app.services.data_scope import scoped_kb_ids

    assert scoped_kb_ids(policy=_dev_policy(), kb_ids=["kb-a", "kb-z"]) == ["kb-a", "kb-z"]
