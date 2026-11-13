"""票 #78：档位门禁与裁剪回显（五道门 + 配置自洽 + 部署能力合取）。

三条接缝，与票里的口径一致：

1. **纯函数直测**（上半）：``profile_chain_of`` 把档位展开成一整条链路；
   ``resolve_retrieval_options`` 是五道门的唯一实现（顺序、拒绝、裁剪、回显）；
   ``ensure_policy_consistent`` 是配置时的那一层自洽校验。三者都不碰数据库、不碰请求。
2. **ASGI 边界**（下半）：同一批判定在真实应用上的样子——越权拿 ``20013``、裁剪后
   响应里带着 ``tenant_policy`` 与 ``clipped``。
3. **开发身份**：它必须是 ``custom`` + 手动参数缺省，从而跟着部署缺省漂，而不是钉死
   在一个命名档位上。

命名档位与 ``custom`` 档的**反应刻意不同**，本文件把它钉成对照：
命名档位越权 → 拒绝（名字是产品契约，降级就是欺骗）；
``custom`` 档越权 → 裁剪 + 回显（平台兜住上限，但不剥夺使用权，也不许悄悄改）。
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY, DEV_KEY_PLAN, KeyContext
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import (
    PROFILE_BALANCED,
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
    KeyPolicySnapshot,
)
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PLAN_STANDARD, PlanSpec
from app.services.retrieval_options import (
    MAX_TOP_K,
    RetrievalOptions,
    ProfileChain,
    tenant_policy_echo,
    profile_chain_of,
)
from app.services.profile_policy import ensure_policy_consistent, resolve_retrieval_options
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb

TENANT_ID = "tenant_demo"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
KB = "kb-a"


# ───────────────────────────────────────────── 展开：命名档位是完整替换


def _policy(
    *,
    profile: str = PROFILE_CUSTOM,
    include_parent: bool = True,
    expand_table_rows: bool = True,
    custom_options: tuple[tuple[str, object], ...] = (),
    hybrid_allowed: bool = True,
    rerank_allowed: bool = True,
    query_rewrite_allowed: bool = True,
    research_allowed: bool = False,
    # 限额默认取「三档都容得下」的最小值：本文件测的是档位与能力，不是数值门禁；
    # 数值那几条用例自己覆盖这三个字段。
    retrieve_qps: int = 1,
    retrieve_daily: int = 100,
    allowed_kb_ids: tuple[str, ...] = ("kb-1",),
) -> KeyPolicySnapshot:
    """一份只关心档位与开关的快照；其余字段取内置开发身份那份。"""

    return dataclasses.replace(
        DEV_KEY_POLICY,
        profile=profile,
        include_parent=include_parent,
        expand_table_rows=expand_table_rows,
        custom_options=custom_options,
        hybrid_allowed=hybrid_allowed,
        rerank_allowed=rerank_allowed,
        query_rewrite_allowed=query_rewrite_allowed,
        research_allowed=research_allowed,
        retrieve_qps=retrieve_qps,
        retrieve_daily=retrieve_daily,
        allowed_kb_ids=allowed_kb_ids,
    )


def _plan(code: str, **overrides: Any) -> PlanSpec:
    """三档种子之一，按需改几列（模拟管理员在控制台把某一档调低）。"""

    return dataclasses.replace(PLAN_PRESETS[code], **overrides)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        # 三档的全部内容写在票面上，这里逐字抄一遍：任何一个预设被改动，这条就变红。
        (PROFILE_SPEED, ProfileChain(mode="vector", top_k=5, rerank=False, rewrite=False)),
        (PROFILE_BALANCED, ProfileChain(mode="hybrid", top_k=5, rerank=False, rewrite=False)),
        (PROFILE_QUALITY, ProfileChain(mode="hybrid", top_k=5, rerank=True, rewrite=True)),
    ],
)
def test_each_named_profile_expands_to_exactly_its_chain(
    profile: str, expected: ProfileChain
) -> None:
    """命名档位 = 一整条链路：模式、条数、是否重排、是否改写，四项一个不少。

    同时在配置里塞一批手动参数与两个关掉的结果形状——命名档位下它们**一个都不生效**
    （完整替换，不是「预设打底、配置覆盖」）。
    """

    chain = profile_chain_of(
        _policy(
            profile=profile,
            custom_options=(("mode", "bm25"), ("top_k", 1), ("rerank", False)),
            include_parent=False,
            expand_table_rows=False,
        )
    )

    assert chain == expected


def test_a_named_profile_still_takes_the_result_shape_group() -> None:
    """两个形状列**不是** ``custom`` 档专属：它们是「结果形状组」，每一档都读。"""

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, include_parent=False, expand_table_rows=False),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    assert options.fold_to_sections is False
    assert options.expand_table_rows is False


def test_custom_reads_its_manual_parameters() -> None:
    """``custom`` 档才读那批手动参数：四项都按写下来的走。"""

    chain = profile_chain_of(
        _policy(
            profile=PROFILE_CUSTOM,
            custom_options=(
                ("mode", "bm25"),
                ("top_k", 3),
                ("rerank", True),
                ("rewrite", True),
            ),
        )
    )

    assert chain == ProfileChain(mode="bm25", top_k=3, rerank=True, rewrite=True)


def test_custom_falls_back_per_key_to_the_deployment_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺键 = 「这个键没给」，逐键回落到**部署缺省**（不是整份作废）。"""

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "hybrid")
    monkeypatch.setattr(settings, "TOP_K", 7)
    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    chain = profile_chain_of(_policy(profile=PROFILE_CUSTOM, custom_options=(("mode", "vector"),)))

    assert chain == ProfileChain(mode="vector", top_k=7, rerank=True, rewrite=True)


# ───────────────────────────────────────────── 五道门：顺序、拒绝与裁剪


def test_a_profile_outside_the_plan_is_a_feature_error() -> None:
    """第 1 道：档位不在套餐白名单 → ``20013``，连展开都不该发生。"""

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_FREE), kb_count=1
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert PROFILE_QUALITY in excinfo.value.msg


def test_a_named_profile_that_needs_a_dropped_capability_is_rejected_not_degraded() -> None:
    """第 3 道：档位仍在白名单里，但套餐事后撤了重排 → 拒绝，绝不悄悄少一步。

    管理员把 ``pro`` 的 ``rerank_allowed`` 关掉之后，挂 ``quality`` 的 Key 下一次调用
    就失败——名字是产品契约，静默降级成「高质量 = 混合但没重排」才是欺骗。
    """

    plan = _plan(PLAN_PRO, rerank_allowed=False)

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_QUALITY), plan=plan, kb_count=1
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert "rerank" in excinfo.value.msg


def test_the_same_excess_is_rejected_for_a_named_profile_and_clipped_for_custom() -> None:
    """两种超限反应刻意不同，这是整个票的中心。

    同一个套餐（没有重排）、同一批越权参数：命名档位被**拒绝**，``custom`` 档被
    **裁剪并回显**——前者是名字被改成谎言，后者只是平台兜住上限。
    """

    plan = _plan(PLAN_STANDARD)

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_QUALITY, rerank_allowed=True),
            plan=plan,
            kb_count=1,
        )
    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED

    clipped = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "hybrid"), ("rerank", True), ("rewrite", True)),
        ),
        plan=plan,
        kb_count=1,
    )
    assert clipped.tenant_policy is not None
    assert clipped.tenant_policy.clipped == ("rerank", "rewrite"), "裁剪必须回显被裁字段"
    assert clipped.rerank is False and clipped.rewrite is False


def test_custom_over_the_plan_is_clipped_and_the_clip_list_names_the_fields() -> None:
    """第 4 道：``custom`` 档参数超套餐上限 → 裁剪，并逐个记下被裁的字段。"""

    options = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            custom_options=(
                ("mode", "hybrid"),
                ("top_k", MAX_TOP_K + 20),
                ("rerank", True),
                ("rewrite", True),
            ),
            # 配置开关在套餐上限之内分配给这套集成（自洽校验保证它不超过套餐）。
            rerank_allowed=True,
            query_rewrite_allowed=True,
        ),
        plan=_plan(PLAN_STANDARD),
        kb_count=1,
    )

    assert options.top_k == MAX_TOP_K, "条数夹到平台硬上限"
    assert options.rerank is False, "套餐不给重排 → 关掉"
    assert options.rewrite is False, "套餐不给改写 → 关掉"
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("top_k", "rerank", "rewrite")


def test_a_custom_hybrid_chain_on_a_plan_without_hybrid_falls_back_to_vector() -> None:
    """套餐不给混合检索时，``custom`` 档的 ``hybrid`` 被裁成向量一路并回显 ``mode``。

    用 standard 作底座、把 ``hybrid_allowed`` 关掉：standard 的白名单里有 ``custom``
    （free 只给 ``speed``，连 ``custom`` 都不许用——那一条由第 1 道门判）。
    """

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_CUSTOM, custom_options=(("mode", "hybrid"),)),
        plan=_plan(PLAN_STANDARD, hybrid_allowed=False),
        kb_count=1,
    )

    assert options.mode == "vector"
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("mode",)


def test_nothing_is_clipped_when_the_chain_fits_the_plan() -> None:
    """裁了才回显：一次没裁的调用里 ``clipped`` 必须是空的，否则「被裁过」就无法判断。"""

    options = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "vector"), ("top_k", 3)),
            hybrid_allowed=False,
            rerank_allowed=False,
            query_rewrite_allowed=False,
        ),
        plan=_plan(PLAN_STANDARD),
        kb_count=1,
    )

    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ()


def test_too_many_knowledge_bases_in_one_request_is_rejected_not_narrowed() -> None:
    """第 5 道：单次检索库数超上限 → 拒绝。

    这一条**不裁剪**：把 3 个库悄悄截成 1 个，改的是「查什么」而不是「查多好」，
    调用方会拿到一份看起来完整、其实少了两/三范围的答案。
    """

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_SPEED), plan=_plan(PLAN_FREE), kb_count=3
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert "knowledge bases" in excinfo.value.msg


def test_the_profile_gate_runs_before_the_knowledge_base_count_gate() -> None:
    """顺序：档位先判、库数后判。两处都越权时报的必须是**档位**那一条。

    顺序反了不会更安全，只会让排障变猜谜：同一次配置错误有时报「档位不允许」、
    有时报「库太多」，取决于这次带了几 个库。
    """

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            # free 档只允许 speed；这里同时给了 quality 与 3 个库，两处都越权。
            policy=_policy(profile=PROFILE_QUALITY),
            plan=_plan(PLAN_FREE),
            kb_count=3,
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert PROFILE_QUALITY in excinfo.value.msg
    assert "knowledge bases" not in excinfo.value.msg


# ───────────────────────────────────────────── 部署能力 vs 档位意图


def test_the_deployment_switch_holds_rerank_and_rewrite_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """档位表达**意图**，部署开关表达**这台机器装没装**，生效值是两者的合取。

    本机没配重排 / 改写（``RERANK_ENABLED`` / ``QUERY_REWRITE_ENABLED`` 关着）时，
    ``quality`` 档回显的 rerank / rewrite 必须是 ``False``——档位绝不能凭空变出一个
    部署没配的 Provider。
    """

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO), kb_count=1
    )

    assert options.rerank is False
    assert options.rewrite is False
    # 意图没被抹掉：展开那一层仍然写着 quality 要重排与改写，只是这台机器给不了。
    intent = profile_chain_of(_policy(profile=PROFILE_QUALITY))
    assert (intent.rerank, intent.rewrite) == (True, True)


def test_an_installed_capability_lets_the_profile_run_it() -> None:
    """合取的另一个方向：意图关着时，本机装了也别跑（speed 不该被顺手升级）。"""

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_SPEED),
        plan=_plan(PLAN_PRO),
        kb_count=1,
        rerank_capable=True,
        rewrite_capable=True,
    )

    assert options.rerank is False
    assert options.rewrite is False


def test_with_the_switches_off_a_quality_profile_runs_exactly_the_balanced_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署开关关着时，``quality`` 与 ``balanced`` 的链路参数**逐项相同**。

    这就是「开关关着时行为逐字节不变」的可断言形态：多买的一档在这台机器上带不来
    任何额外动作，回显里也如实写着 rerank / rewrite 为 false。
    """

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    quality = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO), kb_count=1
    )
    balanced = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO), kb_count=1
    )

    chain = ("mode", "top_k", "rerank", "rewrite", "fold_to_sections", "expand_table_rows")
    assert tuple(getattr(quality, name) for name in chain) == tuple(
        getattr(balanced, name) for name in chain
    )


# ───────────────────────────────────────────── 回显：本次实际生效了什么


def test_the_echo_reports_what_actually_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tenant_policy`` 的字段表：套餐名、要求档位、模式、条数、重排、改写、库数、裁剪。"""

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO), kb_count=2
    )

    assert tenant_policy_echo(options, kb_count=2) == {
        "plan": PLAN_PRO,
        "requested_profile": PROFILE_QUALITY,
        "mode": "hybrid",
        "top_k": 5,
        "rerank": True,
        "rewrite": True,
        "kb_count": 2,
        "clipped": [],
    }


def test_the_echo_carries_the_clipped_fields() -> None:
    """被裁过的调用里，回显要能回答「哪些字段被裁掉了」。"""

    options = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM, custom_options=(("top_k", MAX_TOP_K + 1), ("rerank", True))
        ),
        plan=_plan(PLAN_STANDARD),
        kb_count=1,
    )

    echo = tenant_policy_echo(options, kb_count=1)

    assert echo is not None
    assert echo["clipped"] == ["top_k", "rerank"]
    assert echo["plan"] == PLAN_STANDARD
    assert echo["requested_profile"] == PROFILE_CUSTOM


def test_an_unresolved_options_object_has_no_echo() -> None:
    """不经套餐门禁构造的 options（评测脚本 / 内部调用）不回显套餐事实。

    与其编一份「看起来合理」的套餐名，不如承认这一次没有套餐参与判定。
    """

    options = RetrievalOptions(
        mode="vector", top_k=5, fold_to_sections=True, expand_table_rows=True,
        rerank=False, rewrite=False,
    )

    assert options.tenant_policy is None
    assert tenant_policy_echo(options, kb_count=1) is None


def test_resolved_options_never_drift_from_their_echo() -> None:
    """回显里的模式 / 条数 / 重排 / 改写就是生效值本身，不是另存一份。

    实现上 ``tenant_policy_echo`` 从值对象上读，这条用例把「不许另存一份」钉死：
    手改一个字段之后，回显跟着变——它没有第二个真相。
    """

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO), kb_count=1
    )
    edited = dataclasses.replace(options, top_k=1)

    assert tenant_policy_echo(edited, kb_count=1)["top_k"] == 1


# ───────────────────────────────────────────── 配置时自洽校验


def test_a_quality_profile_on_a_free_plan_is_rejected_at_creation() -> None:
    """免费档的 Key 配成高质量档：**建配置时**就拒绝（``20015``），不进库。

    创建是一瞬间、有人盯着的动作；留到运行时，就是客户的生产调用在午夜开始报错。
    """

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_FREE)
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert PROFILE_QUALITY in excinfo.value.msg


def test_a_profile_needing_a_capability_the_policy_switches_do_not_grant_is_rejected() -> None:
    """第一段包含：档位所需能力 ⊆ 配置自己的能力开关。

    ``quality`` 要重排、配置却关着重排——这套配置从落地那一刻就是自相矛盾的。
    """

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(
                profile=PROFILE_QUALITY,
                hybrid_allowed=True,
                rerank_allowed=False,
                query_rewrite_allowed=True,
            ),
            plan=_plan(PLAN_PRO),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert "rerank" in excinfo.value.msg


def test_a_policy_switch_beyond_the_plan_is_rejected() -> None:
    """第二段包含：配置自己的能力开关 ⊆ 套餐能力——配置不能凭空造能力。"""

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(profile=PROFILE_SPEED, rerank_allowed=True),
            plan=_plan(PLAN_STANDARD),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieve_qps": PLAN_PRESETS[PLAN_STANDARD].retrieve_qps + 1},
        {"retrieve_daily": PLAN_PRESETS[PLAN_STANDARD].retrieve_daily + 1},
        {"allowed_kb_ids": ("kb-1", "kb-2", "kb-3", "kb-4", "kb-5", "kb-6")},
    ],
)
def test_a_policy_limit_beyond_the_plan_is_rejected(overrides: dict[str, Any]) -> None:
    """数值同理：QPS / 日配额 / 授权库数不得超过套餐上限。"""

    policy = dataclasses.replace(_policy(profile=PROFILE_SPEED), **overrides)

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(policy=policy, plan=_plan(PLAN_STANDARD))

    assert excinfo.value.code is ErrorCode.PARAM_ERROR


def test_a_consistent_named_profile_policy_is_accepted() -> None:
    """自洽的配置照常放行：三段包含都成立、数值不超限。"""

    ensure_policy_consistent(
        policy=_policy(
            profile=PROFILE_QUALITY,
            hybrid_allowed=True,
            rerank_allowed=True,
            query_rewrite_allowed=True,
        ),
        plan=_plan(PLAN_PRO),
    )


def test_a_consistent_custom_policy_needs_nothing_it_does_not_ask_for() -> None:
    """``custom`` 档只看**写下来**的参数：没写重排就不要求重排能力。

    缺键等于「跟部署缺省走」，而部署缺省是平台自己的选择——不该让租户的一次建配置
    动作因为平台另一次配置而失败。

    （standard 的白名单里有 ``custom``；free 只给 ``speed``，第 1 段包含就过不去。）
    """

    ensure_policy_consistent(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            hybrid_allowed=False,
            rerank_allowed=False,
            query_rewrite_allowed=False,
            custom_options=(("mode", "vector"),),
        ),
        plan=_plan(PLAN_STANDARD),
    )


def test_an_unknown_profile_is_rejected_at_creation() -> None:
    """档位名必须是已知枚举：手打成 ``qualty`` 就不许落库。"""

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(policy=_policy(profile="qualty"), plan=_plan(PLAN_PRO))

    assert excinfo.value.code is ErrorCode.PARAM_ERROR


# ───────────────────────────────────────────── 开发身份跟着部署缺省走


def test_the_development_identity_is_custom_so_it_tracks_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置开发身份 = ``custom`` + 全部缺键 → 每一项都跟着 ``.env`` 走。

    钉在一个命名档位上是错的：#78 之后 ``speed`` 意味着「只走向量一路」，本地的默认
    模式会从 ``RETRIEVAL_MODE`` 悄悄变成 ``vector``；预设值将来再改一次，本地又漂一次。
    """

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "bm25")
    monkeypatch.setattr(settings, "TOP_K", 9)
    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    assert DEV_KEY_POLICY.profile == PROFILE_CUSTOM
    assert DEV_KEY_POLICY.custom_options == ()

    options = resolve_retrieval_options(
        policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN, kb_count=1
    )

    assert (options.mode, options.top_k) == ("bm25", 9)
    assert (options.rerank, options.rewrite) == (True, False)


# ───────────────────────────────────────────── ASGI 边界：真实响应里的码与回显


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    async def similarity_search_scope(self, _db, _vector, *, tenant_id, index_versions, top_k):
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "kb_id": kb_id,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 0.9,
            }
            for kb_id in index_versions
        ]


class _FakeKeyword:
    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return [
            {
                "chunk_id": "chunk-hit",
                "document_id": "doc-hit",
                "kb_id": KB,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 12.5,
            }
        ]


class _Entry:
    """一条 HTTP 入口 + 一个可改的身份（改套餐 / 改档位就是改这里）。"""

    def __init__(self, client: TestClient, holder: dict[str, KeyContext]) -> None:
        self.client = client
        self._holder = holder

    def use(self, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
        self._holder["context"] = KeyContext(
            tenant_id=TENANT_ID, key_id="key-under-test", policy=policy, plan=plan
        )

    def post(self, *, kb_ids: list[str] | None = None) -> dict:
        response = self.client.post(
            RETRIEVE_PATH, json={"kb_ids": kb_ids or [KB], "query": "E1024"}
        )
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entry]:
    """离线 ASGI 客户端；身份由测试直接指定（不经过 api_keys 表，那是 #76 的事）。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    monkeypatch.setattr(service, "get_embedding_provider", _FakeEmbedding)
    monkeypatch.setattr(service, "get_vector_store", _FakeVectorStore)
    monkeypatch.setattr(service, "get_keyword_search_provider", _FakeKeyword)
    # 可见性走异步仓储（第 11 步票 #127）：替身因此也得是 ``async def``。
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    from app.api.deps import get_current_key

    holder: dict[str, KeyContext] = {
        "context": KeyContext(tenant_id=TENANT_ID, policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN)
    }
    # 检索链路走异步依赖（第 11 步票 #127）：同步那条留给身份判定与其他路由。
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: (
        AsyncSessionAdapter(EmptySession())
    )

    main.app.dependency_overrides[get_current_key] = lambda: holder["context"]
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield _Entry(client, holder)
    finally:
        main.app.dependency_overrides.clear()


def test_a_key_configured_with_a_forbidden_profile_fails_at_the_boundary(entry: _Entry) -> None:
    """免费档的 Key 挂着 ``quality``：调用当场 ``20013``（不降级、不执行）。"""

    entry.use(policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_FREE))

    body = entry.post()

    assert body["code"] == ErrorCode.FEATURE_NOT_ALLOWED
    assert body["data"] is None


def test_a_plan_downgrade_fails_the_next_call(entry: _Entry) -> None:
    """管理员事后把套餐调低 → 下一次调用就按新套餐判，不必等缓存过期、不必重签 Key。"""

    entry.use(policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO))
    assert entry.post()["code"] == ErrorCode.SUCCESS

    entry.use(policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_FREE))

    assert entry.post()["code"] == ErrorCode.FEATURE_NOT_ALLOWED


def test_the_response_carries_the_tenant_policy_it_actually_ran(entry: _Entry) -> None:
    """成功响应里带 ``tenant_policy``：套餐、要求档位、实际链路、库数、裁剪列表。"""

    entry.use(policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO))

    data = entry.post()["data"]

    assert data["metadata"]["tenant_policy"] == {
        "plan": PLAN_PRO,
        "requested_profile": PROFILE_BALANCED,
        "mode": "hybrid",
        "top_k": 5,
        "rerank": False,
        "rewrite": False,
        "kb_count": 1,
        "clipped": [],
    }


def test_a_clipped_custom_call_still_succeeds_and_says_what_was_cut(entry: _Entry) -> None:
    """``custom`` 档超限：**跑得通**，但响应里明确列出被裁掉的字段。

    裁剪而不回显等于欺骗——调用方付了「10 条 + 重排」，拿到 50 条上限、没有重排，
    却以为一切照配置执行。
    """

    entry.use(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "hybrid"), ("top_k", MAX_TOP_K + 10), ("rerank", True)),
            rerank_allowed=True,
        ),
        plan=_plan(PLAN_STANDARD),
    )

    body = entry.post()

    assert body["code"] == ErrorCode.SUCCESS
    echo = body["data"]["metadata"]["tenant_policy"]
    assert echo["clipped"] == ["top_k", "rerank"]
    assert echo["top_k"] == MAX_TOP_K
    assert echo["rerank"] is False
    assert body["data"]["metadata"]["top_k"] == MAX_TOP_K, "回显与生效值同一个来源"


def test_a_request_that_asks_for_too_many_knowledge_bases_is_rejected(
    entry: _Entry,
) -> None:
    """库数超上限在边界处拒绝：作用域不许被悄悄收窄。"""

    entry.use(policy=_policy(profile=PROFILE_SPEED), plan=_plan(PLAN_FREE))

    body = entry.post(kb_ids=["kb-a", "kb-b"])

    assert body["code"] == ErrorCode.FEATURE_NOT_ALLOWED
    assert body["data"] is None


# ───────────────────────────────────────────── 档位意图真的决定了跑不跑那一步


class _RecordingRerank:
    """「本机装好了重排」的替身：可用，并记下被调了几次。"""

    available = True
    provider_name = "fake"
    model_name = None

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        self.calls.append([str(chunk["chunk_id"]) for chunk in chunks])
        return list(reversed(chunks))[:top_n]


def _install_rerank(monkeypatch: pytest.MonkeyPatch) -> _RecordingRerank:
    """把「这台机器装了重排」装进去：开关打开 + 一个真的可用的 Provider。"""

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    provider = _RecordingRerank()
    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: provider)
    return provider


def test_a_profile_that_does_not_ask_for_rerank_leaves_it_off_even_when_installed(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``balanced`` 在装了重排的机器上**也不许**重排（意图 ∧ 能力，不是「能力就够」）。

    这是本票最容易漏的一条：重排阶段过去只问「Provider 可不可用」，于是同一台机器上
    ``balanced`` 与 ``quality`` 跑出来是同一条链路，而回显里还写着 ``rerank: false``
    ——那正是「回显是谎言」的样子。
    """

    provider = _install_rerank(monkeypatch)
    entry.use(policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO))

    body = entry.post()

    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is False
    assert body["data"]["metadata"]["retrieval"]["rerank"]["applied"] is False
    assert provider.calls == [], "档位没要重排，装了也不许用"


def test_a_profile_that_asks_for_rerank_uses_the_installed_one(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照组：同一台机器、同一个假 Provider，``quality`` 档的重排真的跑了。"""

    provider = _install_rerank(monkeypatch)
    entry.use(policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO))

    body = entry.post()

    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is True
    assert body["data"]["metadata"]["retrieval"]["rerank"]["applied"] is True
    assert len(provider.calls) == 1, "要重排的档位真的调了一次重排"
