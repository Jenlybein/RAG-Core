"""票 #78：档位门禁与裁剪回显（五道门 + 配置自洽 + 部署能力合取）。

三条接缝，与票里的口径一致：

1. **纯函数直测**（上半）：``retrieval_params_of`` 把档位展开成一整条链路；
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
    KeyPolicySnapshot,
)
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PLAN_PLUS, PlanSpec
from app.services.retrieval_options import (
    MAX_TOP_K,
    RetrievalOptions,
    RetrievalParams,
    tenant_policy_echo,
    retrieval_params_of,
)
from app.services.profile_policy import ensure_policy_consistent, resolve_retrieval_options
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb
from tests.support.policy import PROFILE_BALANCED, PROFILE_CUSTOM, PROFILE_QUALITY, PROFILE_SPEED
from tests.support.policy import policy as support_policy

TENANT_ID = "tenant_demo"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
KB = "kb-a"


# ───────────────────────────────────────────── 展开：命名档位是完整替换


def _policy(**kwargs: Any) -> KeyPolicySnapshot:
    """一份只关心参数与授权位的快照；夹具词汇（档位名 / 已退役列名）由 tests.support 翻译。"""

    return support_policy(**kwargs)
def _plan(code: str, **overrides: Any) -> PlanSpec:
    """三档种子之一，按需改几列（模拟管理员在控制台把某一档调低）。"""

    return dataclasses.replace(PLAN_PRESETS[code], **overrides)


@pytest.mark.parametrize("profile", [PROFILE_SPEED, PROFILE_BALANCED, PROFILE_QUALITY])
def test_the_declared_parameters_win_over_the_profile_name(profile: str) -> None:
    """第 13 步：链路三项参数直存，档位名不再决定任何东西。

    同一批参数、三个不同的档位名，拿到的是同一条链路——这是「后端无档位」的可执行定义。
    """

    chain = retrieval_params_of(
        _policy(
            profile=profile,
            custom_options=(("mode", "bm25"), ("top_k", 1), ("rerank", False)),
        )
    )

    assert chain == RetrievalParams(mode="bm25", top_k=1, rerank=False)


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

    chain = retrieval_params_of(
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

    assert chain == RetrievalParams(mode="bm25", top_k=3, rerank=True)


def test_missing_parameters_fall_back_per_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """第 13 步：mode/top_k 缺键逐键回落部署缺省；rerank 缺键 = 不要。"""

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "hybrid")
    monkeypatch.setattr(settings, "TOP_K", 7)

    chain = retrieval_params_of(_policy(custom_options=(("mode", "vector"),)))

    assert chain == RetrievalParams(mode="vector", top_k=7, rerank=False)


# ───────────────────────────────────── 门禁：一律裁剪回显，没有档位白名单


def test_the_profile_name_never_gates_a_call() -> None:
    """第 13 步：档位名不参与任何门禁。同样的参数，任何 profile 都跑得通。"""

    for profile in (PROFILE_SPEED, PROFILE_BALANCED, PROFILE_QUALITY, PROFILE_CUSTOM):
        options = resolve_retrieval_options(
            policy=_policy(profile=profile, custom_options=(("mode", "vector"),)),
            plan=_plan(PLAN_FREE),
            kb_count=1,
        )
        assert options.mode == "vector", profile


def test_only_top_k_is_clipped_and_the_clip_list_says_so() -> None:
    """第 4 道：条数超套餐封顶 → 裁剪 + 回显，而且**只有条数会被裁**。

    第 13 步之前这里还会裁 ``mode`` / ``rerank`` / ``rewrite``；现在那三样都不是「套餐
    上限」的问题了：模式是普通参数，重排是授权位（位为假就是 ``20013``），改写是端点。
    把它们继续悄悄裁掉，正是「看不见的降级」。
    """

    options = resolve_retrieval_options(
        policy=_policy(
            custom_options=(("mode", "hybrid"), ("top_k", MAX_TOP_K + 20), ("rerank", True)),
            rerank_allowed=True,
        ),
        # max_top_k=0 = 套餐不额外封顶，这条测的是平台硬上限那一层。
        plan=_plan(PLAN_PLUS, max_top_k=0),
        kb_count=1,
    )

    assert options.top_k == MAX_TOP_K, "条数夹到平台硬上限"
    assert options.mode == "hybrid", "模式不再被套餐裁剪"
    assert "rerank" not in options.tenant_policy.clipped, "重排是授权位的事，不是裁剪的事"
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("top_k",), "clipped 只剩一种来源"


def test_the_plan_max_top_k_wins_over_the_platform_cap() -> None:
    """套餐封顶比平台硬上限更紧时，夹到套餐那一档并回显。"""

    options = resolve_retrieval_options(
        policy=_policy(custom_options=(("top_k", 40),)),
        plan=_plan(PLAN_PLUS),  # max_top_k = 10
        kb_count=1,
    )

    assert options.top_k == PLAN_PRESETS[PLAN_PLUS].max_top_k
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("top_k",)


def test_nothing_is_clipped_when_the_parameters_fit_the_plan() -> None:
    """裁了才回显：一次没裁的调用里 ``clipped`` 必须是空的，否则「被裁过」就无法判断。"""

    options = resolve_retrieval_options(
        policy=_policy(
            custom_options=(("mode", "vector"), ("top_k", 3)),
            rerank_allowed=False,
        ),
        plan=_plan(PLAN_PLUS),
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


def test_too_many_kb_is_rejected_regardless_of_the_profile_name() -> None:
    """库数上限这道门与档位名无关：名字不再参与判定，库数照旧是硬拒绝。"""

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_QUALITY, custom_options=(("mode", "vector"),)),
            plan=_plan(PLAN_FREE),
            kb_count=3,
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert "knowledge bases" in excinfo.value.msg


# ───────────────────────────────────────────── 部署能力 vs 档位意图


def test_the_deployment_switch_holds_rerank_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """参数表达**意图**，部署开关表达**这台机器装没装**，生效值是两者的合取。

    本机没配重排（``RERANK_ENABLED`` 关着）时，即使配置直存 ``rerank=true``，
    生效值也必须是 ``False``——配置绝不能凭空变出一个部署没配的 Provider。
    改写不再是配置项（它是 /rag/rewrite 端点），这里只验重排。
    """

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    options = resolve_retrieval_options(
        policy=_policy(custom_options=(("rerank", True),), rerank_allowed=True),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    assert options.rerank is False
    # 意图没被抹掉：展开那一层仍写着要重排，只是这台机器给不了。
    intent = retrieval_params_of(_policy(custom_options=(("rerank", True),)))
    assert intent.rerank is True


def test_an_installed_capability_lets_the_profile_run_it() -> None:
    """合取的另一个方向：意图关着时，本机装了也别跑（speed 不该被顺手升级）。"""

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_SPEED),
        plan=_plan(PLAN_PRO),
        kb_count=1,
        rerank_capable=True,
    )

    assert options.rerank is False


def test_with_the_switches_off_a_quality_profile_runs_exactly_the_balanced_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署开关关着时，``quality`` 与 ``balanced`` 的链路参数**逐项相同**。

    这就是「开关关着时行为逐字节不变」的可断言形态：多买的一档在这台机器上带不来
    任何额外动作，回显里也如实写着 rerank 为 false（改写已是端点，不再是参数）。
    """

    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    quality = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_PRO), kb_count=1
    )
    balanced = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO), kb_count=1
    )

    chain = ("mode", "top_k", "rerank", "fold_to_sections", "expand_table_rows")
    assert tuple(getattr(quality, name) for name in chain) == tuple(
        getattr(balanced, name) for name in chain
    )


# ───────────────────────────────────────────── 回显：本次实际生效了什么


def test_the_echo_reports_what_actually_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tenant_policy`` 的字段表：套餐名、要求档位、模式、条数、重排、改写、编排、库数、裁剪。"""

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    options = resolve_retrieval_options(
        policy=_policy(
            custom_options=(("mode", "hybrid"), ("top_k", 5), ("rerank", True)),
            rerank_allowed=True,
        ),
        plan=_plan(PLAN_PRO),
        kb_count=2,
    )

    # 第 13 步：回显改报参数本身，不再有 requested_profile / rewrite。
    assert tenant_policy_echo(options, kb_count=2) == {
        "plan": PLAN_PRO,
        "mode": "hybrid",
        "top_k": 5,
        "rerank": True,
        "kb_count": 2,
        "clipped": [],
    }


def test_the_echo_carries_the_clipped_fields() -> None:
    """被裁过的调用里，回显要能回答「哪些字段被裁掉了」。"""

    options = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM, custom_options=(("top_k", MAX_TOP_K + 1), ("rerank", True))
        ),
        plan=_plan(PLAN_PLUS),
        kb_count=1,
    )

    echo = tenant_policy_echo(options, kb_count=1)

    assert echo is not None
    assert echo["clipped"] == ["top_k"]
    assert echo["plan"] == PLAN_PLUS
    assert "requested_profile" not in echo


def test_an_unresolved_options_object_has_no_echo() -> None:
    """不经套餐门禁构造的 options（评测脚本 / 内部调用）不回显套餐事实。

    与其编一份「看起来合理」的套餐名，不如承认这一次没有套餐参与判定。
    """

    options = RetrievalOptions(
        mode="vector",
        top_k=5,
        fold_to_sections=True,
        expand_table_rows=True,
        rerank=False,
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


def test_an_old_profile_name_on_a_free_plan_is_ignored_at_creation() -> None:
    """第 13 步：档位名已废弃，不再做白名单校验（开关不超套餐即可）。"""

    # 档位名不再参与判定；只要五个授权位与额度不超套餐就通过。注意 ``retrieval_rerank``
    # 跟着 ``quality`` 的组合别名是 true，所以重排位也得关掉才自洽。
    ensure_policy_consistent(
        policy=_policy(
            profile=PROFILE_QUALITY,
            retrieval_rerank=False,
            rewrite_allowed=False,
            decompose_allowed=False,
            rerank_allowed=False,
            research_allowed=False,
        ),
        plan=_plan(PLAN_FREE),
    )


def test_policy_switches_below_the_plan_are_still_rejected() -> None:
    """第 13 步：档位所需能力校验已删，但「配置开关 ⊆ 套餐能力」这条仍守。"""

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(
                rewrite_allowed=False,
                decompose_allowed=False,
                research_allowed=False,
                rerank_allowed=True,
            ),
            plan=_plan(PLAN_FREE, rerank_allowed=False, research_allowed=False),
        )
    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert "rerank" in excinfo.value.msg


def test_a_policy_switch_beyond_the_plan_is_rejected() -> None:
    """第二段包含：配置自己的能力开关 ⊆ 套餐能力——配置不能凭空造能力。"""

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(
                rewrite_allowed=False,
                decompose_allowed=False,
                research_allowed=False,
                rerank_allowed=True,
            ),
            plan=_plan(PLAN_FREE),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieve_qps": PLAN_PRESETS[PLAN_PLUS].retrieve_qps + 1},
        {"retrieve_daily": PLAN_PRESETS[PLAN_PLUS].retrieve_daily + 1},
        {"allowed_kb_ids": ("kb-1", "kb-2", "kb-3", "kb-4", "kb-5", "kb-6")},
    ],
)
def test_a_policy_limit_beyond_the_plan_is_rejected(overrides: dict[str, Any]) -> None:
    """数值同理：QPS / 日配额 / 授权库数不得超过套餐上限。"""

    policy = dataclasses.replace(_policy(profile=PROFILE_SPEED), **overrides)

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(policy=policy, plan=_plan(PLAN_PLUS))

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


# ─────────────────────────────────── 三本账的额度不得超过套餐（第 13 步）


@pytest.mark.parametrize(
    "overrides",
    [
        {"rerank_qps": PLAN_PRESETS[PLAN_PRO].rerank_qps + 1},
        {"rerank_daily": PLAN_PRESETS[PLAN_PRO].rerank_daily + 1},
        {"llm_qps": PLAN_PRESETS[PLAN_PRO].llm_qps + 1},
        {"llm_daily": PLAN_PRESETS[PLAN_PRO].llm_daily + 1},
    ],
)
def test_a_quota_beyond_the_plan_is_rejected(overrides: dict[str, str | int]) -> None:
    """配置的加工能力额度不得超过套餐：这套集成只能在买到的额度里分配。"""

    policy = dataclasses.replace(_policy(), **overrides)

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(policy=policy, plan=_plan(PLAN_PRO))

    assert excinfo.value.code is ErrorCode.PARAM_ERROR


def test_a_zero_processing_quota_is_a_legal_configuration() -> None:
    """0 是「这套集成不用这项能力」，不是数值错误——与 ``retrieve_qps`` 的 0 语义相反。"""

    ensure_policy_consistent(
        policy=_policy(rerank_qps=0, rerank_daily=0, llm_qps=0, llm_daily=0),
        plan=_plan(PLAN_PRO),
    )


def test_wanting_rerank_without_the_bit_is_rejected_at_write_time() -> None:
    """「参数要重排」而「重排位关着」是一份自相矛盾的配置：拒绝，而不是每次静默跳过。

    第 13 步把重排从「裁剪」改成「授权位」之后，这一对必须自洽——否则调用方会拿到
    ``rerank: false`` 却不知道为什么，而配置里明明写着 ``true``。
    """

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(rerank_allowed=False, retrieval_rerank=True),
            plan=_plan(PLAN_PRO),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert "rerank" in excinfo.value.msg


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
        plan=_plan(PLAN_PLUS),
    )


def test_an_unknown_profile_name_is_ignored_at_creation() -> None:
    """第 13 步：档位名不再参与判定，手写错的名字也不影响任何行为。"""

    # 不再抛错：profile 是 deprecated 兼容壳，配置是否自洽只看真列与数值上限。
    ensure_policy_consistent(policy=_policy(profile="qualty"), plan=_plan(PLAN_PRO))


# ───────────────────────────────────────────── 开发身份跟着部署缺省走


def test_the_development_identity_is_a_snapshot_of_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置开发身份把部署缺省**快照**在启动时，三项参数就是它，不再二次回落到 settings。

    第 13 步之前开发身份用 ``custom`` + 空 ``custom_options`` 表达「跟 .env 走」，那是
    因为参数存的是「缺键」。现在参数是**存下来的值**，所以这层回落不存在了：改 ``.env``
    要重启，与真实密钥配置同一条规律——这比「本地悄悄和 CI 跑不同的链路」更可预测。
    """

    # 快照在 import 时就定型了，之后再改 settings 不会影响它。
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "bm25")
    monkeypatch.setattr(settings, "TOP_K", 9)

    options = resolve_retrieval_options(policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN, kb_count=1)

    assert options.mode == DEV_KEY_POLICY.retrieval_mode
    assert options.top_k == DEV_KEY_POLICY.retrieval_top_k
    assert options.mode != "bm25" or settings.RETRIEVAL_MODE == "bm25"
    # 开发身份不声明重排（要重排就在快照里写 true），所以这次不重排。
    assert options.rerank is False


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
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        EmptySession()
    )

    main.app.dependency_overrides[get_current_key] = lambda: holder["context"]
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield _Entry(client, holder)
    finally:
        main.app.dependency_overrides.clear()


def test_an_old_profile_name_does_not_block_a_call(entry: _Entry) -> None:
    """第 13 步：旧缓存里挂着 ``quality`` 的 Key 不再被拒——档位名不是门禁。"""

    entry.use(policy=_policy(profile=PROFILE_QUALITY), plan=_plan(PLAN_FREE))

    body = entry.post()

    assert body["code"] == ErrorCode.SUCCESS, "档位名不再决定能否调用"


def test_a_plan_downgrade_takes_effect_on_the_next_call(entry: _Entry) -> None:
    """管理员事后把套餐调低 → 下一次调用按新套餐生效，不必等缓存过期。

    第 13 步起「被收回的东西」有两种，各按自己的规矩处理：

    * ``max_top_k`` 变小 → **裁剪 + 回显**（``clipped`` 里出现 ``top_k``）；
    * 授权位被收回 → 重排的生效值折成 false（位 ∧ 位 的合取），而这份配置本身已经
      **不自洽**了：下一次改配置会被 ``20015`` 拒，管理员必须先调配置再降套餐。
    """

    policy = _policy(
        custom_options=(("mode", "hybrid"), ("rerank", True), ("top_k", 40)),
        rerank_allowed=True,
    )
    entry.use(policy=policy, plan=_plan(PLAN_PRO))
    assert entry.post()["data"]["metadata"]["retrieval"]["rerank"] is not None

    downgraded = _plan(PLAN_FREE, rerank_allowed=False)
    entry.use(policy=policy, plan=downgraded)
    body = entry.post()

    assert body["code"] == ErrorCode.SUCCESS, "降级不该让进行中的集成当场失败"
    metadata = body["data"]["metadata"]
    assert metadata["tenant_policy"]["rerank"] is False, "位被收回 → 这次不重排"
    assert "top_k" in metadata["tenant_policy"]["clipped"], "条数按新的封顶裁剪"
    with pytest.raises(AppError):
        ensure_policy_consistent(policy=policy, plan=downgraded)


def test_the_response_carries_the_tenant_policy_it_actually_ran(entry: _Entry) -> None:
    """成功响应里带 ``tenant_policy``：套餐、要求档位、实际链路、库数、裁剪列表。"""

    entry.use(policy=_policy(profile=PROFILE_BALANCED), plan=_plan(PLAN_PRO))

    data = entry.post()["data"]

    assert data["metadata"]["tenant_policy"] == {
        "plan": PLAN_PRO,
        "mode": "hybrid",
        "top_k": 5,
        "rerank": False,
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
        plan=_plan(PLAN_PLUS, max_top_k=0),  # 这条测平台硬上限那一层
    )

    body = entry.post()

    assert body["code"] == ErrorCode.SUCCESS
    echo = body["data"]["metadata"]["tenant_policy"]
    assert echo["clipped"] == ["top_k"]
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


def test_rerank_stays_off_when_not_requested_even_when_installed(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置没直存 rerank=true 时，装了重排也不许用（意图 ∧ 能力，不是「能力就够」）。"""

    provider = _install_rerank(monkeypatch)
    entry.use(policy=_policy(rerank_allowed=True), plan=_plan(PLAN_PRO))

    body = entry.post()

    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is False
    assert body["data"]["metadata"]["retrieval"]["rerank"]["applied"] is False
    assert provider.calls == [], "参数没要重排，装了也不许用"


def test_rerank_runs_when_requested_and_installed(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照组：同一台机器、同一个假 Provider，配置直存 rerank=true 时重排真的跑了。"""

    provider = _install_rerank(monkeypatch)
    entry.use(
        policy=_policy(custom_options=(("rerank", True),), rerank_allowed=True),
        plan=_plan(PLAN_PRO),
    )

    body = entry.post()

    assert body["data"]["metadata"]["tenant_policy"]["rerank"] is True
    assert body["data"]["metadata"]["retrieval"]["rerank"]["applied"] is True
    assert len(provider.calls) == 1, "要重排的档位真的调了一次重排"
