"""票 #75：判定内核（唯一的「这次是谁」）与开发身份。

接缝是**纯函数**：``app.core.auth.resolve_identity`` 收一个「有 ``headers`` 的请求对象」
加一个数据库会话，直接调用、直接断言返回的上下文或抛出的业务失败。不经过任何 Web 框架
——这正是这张票存在的理由：HTTP 与 MCP 各自只剩一层薄适配器，规则只有这一份。

数据库换成 ``tests/fake_db.RecordingSession``（既有假会话助手），套餐缓存换成模块内的
内存假实现：默认套件不碰 Redis、不碰 PostgreSQL。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.api_key import ApiKey
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_FREE, PLAN_PRO, PLAN_PLUS, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from tests.fake_db import AsyncRecordingSession, MemoryPlanCache, RecordingSession
from tests.support.keys import mint_key

TENANT_ID = "tenant_acme"
OTHER_TENANT_ID = "tenant_other"
POLICY_ID = "22222222-2222-4222-8222-222222222222"
KEY_ID = "33333333-3333-4333-8333-333333333333"
#: 这一组用例共用的那一把 Key：行与原文一起造，原文进 Bearer 头、哈希落进行里。
#: 只造一次（scrypt 一次 50ms 量级），各用例按需拿它的行或原文。
_SESSION_KEY = mint_key(key_id=KEY_ID, tenant_id=TENANT_ID, policy_id=POLICY_ID, label="控制台调试")
KEY_VALUE = _SESSION_KEY.plaintext
PLAN_IDS = {PLAN_FREE: 1, PLAN_PLUS: 2, PLAN_PRO: 3}


class _Request:
    """最小请求替身：内核只认 ``headers``，不认任何框架类型。"""

    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {} if authorization is None else {"authorization": authorization}


def _plan_rows() -> list[Plan]:
    """三档套餐行；主键在假会话里手工给（与 test_identity_seeding 的约定一致）。"""

    rows: list[Plan] = []
    for code, plan_id in PLAN_IDS.items():
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = plan_id
        rows.append(row)
    return rows


def _tenant_row(
    tenant_id: str = TENANT_ID, *, plan_id: int = PLAN_IDS[PLAN_PRO], status: str = "active"
) -> Tenant:
    return Tenant(id=tenant_id, name="Acme 公司", plan_id=plan_id, status=status)


def _policy_row(
    *,
    tenant_id: str = TENANT_ID,
    include_parent: bool = False,
    expand_table_rows: bool = True,
) -> KeyPolicy:
    return KeyPolicy(
        id=POLICY_ID,
        tenant_id=tenant_id,
        name="console",
        allowed_kb_ids=["kb-1"],
        research_allowed=True,
        rerank_allowed=False,
        retrieve_qps=7,
        retrieve_daily=70,
        include_parent=include_parent,
        expand_table_rows=expand_table_rows,
        max_unit_chars=2000,
        # 第 13 步：三项参数走真列；profile/custom_options 已物理删除。
        retrieval_mode="vector",
        retrieval_top_k=3,
        retrieval_rerank=False,
        # 内存假行不经过 INSERT，列的 default= 不会生效：其余位与账逐列显式写，
        # 与迁移 server_default 回填后的真实行同值。
        retrieve_allowed=True,
        rewrite_allowed=False,
        decompose_allowed=False,
        rerank_qps=0,
        rerank_daily=0,
        llm_qps=0,
        llm_daily=0,
    )


def _key_row(*, status: str = "active") -> ApiKey:
    """这一组用例的那把 Key（同一个原文，两种状态各造一次）。

    只造两次而不是每次调用都造：一次 ``hash_password`` 在这台机器上约 200ms，而这个
    辅助函数在一个文件里被调用十来次——那会把「这条断言失败了吗」的反馈时间拖成分钟级。
    """

    return _KEY_ROWS[status]


_KEY_ROWS = {
    status: mint_key(
        key_id=KEY_ID,
        tenant_id=TENANT_ID,
        policy_id=POLICY_ID,
        label="控制台调试",
        status=status,
        plaintext=KEY_VALUE,
    ).api_key
    for status in ("active", "disabled")
}


def _session(
    *,
    keys: list[ApiKey] | None = None,
    tenants: list[Tenant] | None = None,
    policies: list[KeyPolicy] | None = None,
    plans: list[Plan] | None = None,
) -> AsyncRecordingSession:
    """判定内核的会话替身：**异步面**。

    票 #130 起 ``resolve_identity`` 与 ``resolve_plan`` 都是协程，读的也是
    ``AsyncSession``（``await db.get(...)`` / ``await db.execute(...)``）。这里返回
    :class:`AsyncRecordingSession`——它就是 ``RecordingSession`` 的薄异步面，行与记录
    都还是同一份语义，只是那几个方法得被 ``await``。
    """

    rows: dict[type, list[Any]] = {Plan: _plan_rows() if plans is None else list(plans)}
    if keys is not None:
        rows[ApiKey] = list(keys)
    if tenants is not None:
        rows[Tenant] = list(tenants)
    if policies is not None:
        rows[KeyPolicy] = list(policies)
    return AsyncRecordingSession(rows=rows)


def _dev_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {settings.DEV_API_KEY}"}


# ───────────────────────────────────────────── 上下文对象：15 处既有构造点的兼容


def test_context_is_constructible_from_a_tenant_id_alone() -> None:
    """15 个既有测试只传 ``tenant_id``：上下文必须能只用一个标识造出来。

    这些构造点之所以不能改，是因为 ``dependency_overrides`` 认的是可调用对象本身；
    名单一改，测试就在无提示的情况下退化成「真的走了鉴权」。
    """

    from app.core.auth import DEV_KEY_PLAN, DEV_KEY_POLICY, DevKeyContext, KeyContext

    context = DevKeyContext(tenant_id=TENANT_ID)

    assert DevKeyContext is KeyContext
    assert context.tenant_id == TENANT_ID
    assert context.policy == DEV_KEY_POLICY
    assert context.plan == DEV_KEY_PLAN


def test_development_snapshot_is_a_full_pro_policy_following_the_deployment() -> None:
    """开发身份的配置=「五个授权位全开 + 三项参数跟部署缺省 + 两种形状都开」，套餐=最高档。

    能力全开是为了「开发身份不因配置层的开关改变现行为」：重排 / 改写是否真的跑，
    仍由服务端配置决定。三项参数直取部署缺省（``RETRIEVAL_MODE`` / ``TOP_K`` /
    重排能力）而**不是**钉死某个组合别名：钉在别名上会让本地默认模式悄悄漂移，
    别名取值将来再改一次，开发身份又漂一次。
    """

    from app.core.auth import DEV_KEY_POLICY

    assert DEV_KEY_POLICY.retrieval_mode == settings.RETRIEVAL_MODE
    assert DEV_KEY_POLICY.retrieval_top_k == settings.TOP_K
    assert (
        DEV_KEY_POLICY.retrieve_allowed,
        DEV_KEY_POLICY.rewrite_allowed,
        DEV_KEY_POLICY.decompose_allowed,
        DEV_KEY_POLICY.research_allowed,
        DEV_KEY_POLICY.rerank_allowed,
    ) == (True, True, True, True, True)
    assert DEV_KEY_POLICY.include_parent is True
    assert DEV_KEY_POLICY.expand_table_rows is True
    assert DEV_KEY_POLICY.retrieve_qps == PLAN_PRESETS[PLAN_PRO].retrieve_qps
    assert DEV_KEY_POLICY.retrieve_daily == PLAN_PRESETS[PLAN_PRO].retrieve_daily


# ───────────────────────────────────────────── 开发开关：关着也不许裸奔


@pytest.mark.asyncio
async def test_dev_mode_still_requires_the_placeholder_token() -> None:
    """开关关闭时也不要 Key？不行：占位 token 仍然要带。

    退回「谁都不用带」意味着一个被意外暴露的开发实例对整个网络敞开；而要求这把固定
    token 对开发者零成本（它本来就写在 ``.env`` 里），还让既有测试保持有意义。
    """

    from app.core.auth import resolve_identity

    missing = _Request()
    with pytest.raises(AppError) as missing_exc:
        await resolve_identity(missing, None, auth_enabled=False)
    assert missing_exc.value.code is ErrorCode.UNAUTHORIZED

    wrong = _Request("Bearer not-the-dev-key")
    with pytest.raises(AppError) as wrong_exc:
        await resolve_identity(wrong, None, auth_enabled=False)
    assert wrong_exc.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_dev_mode_says_why_a_real_key_is_rejected() -> None:
    """关掉开关时收到一把真实 Key：消息必须**点名「开发身份模式」**，不能只说「Key 无效」。

    踩过的坑（真实反馈）：控制台能签发 Key、能选中、能显示，但 ``AUTH_ENABLED=false`` 时
    服务端只认内置占位 Key，于是真实 Key 一律 ``20010``。原来回的是 ``MSG_UNKNOWN_KEY``
    （当时的原文是 ``invalid api key``），界面又把它翻成「去 Key 管理页换一把」——**换多少把
    都没用**，因为开关不在 Key 上，而在服务端。这条断言钉住「消息要说出真正的原因」。
    """

    from app.core.auth import MSG_DEV_MODE_REJECTS_REAL_KEY, resolve_identity

    with pytest.raises(AppError) as exc:
        await resolve_identity(_Request("Bearer rc_live_0000_9188"), None, auth_enabled=False)

    assert exc.value.code is ErrorCode.UNAUTHORIZED
    assert exc.value.msg == MSG_DEV_MODE_REJECTS_REAL_KEY
    # 消息里不能出现占位 Key 的**值**：那是凭据，不该由一条错误消息发出去。
    assert settings.DEV_API_KEY not in exc.value.msg


def test_unauthorized_messages_each_name_a_next_step() -> None:
    """四种 20010 的下一步**完全不同**，每条消息都要自带那一步。

    为什么值得单独钉：这些消息现在会**原样显示在控制台**（前端对数据面 20010 照用后端
    原文，见 `frontend/src/lib/envelope.ts` 的 `humanMessage`）。所以「只回一个状态词」
    等于让使用者自己去猜该改哪里——而四种原因的下一步是四件事：补请求头 / 重新签发 /
    启用 Key / 找管理员。断言的不是措辞，是「有没有指向一个具体动作」。
    """

    from app.core import auth

    messages = {
        "missing": auth.MSG_MISSING_TOKEN,
        "unknown": auth.MSG_UNKNOWN_KEY,
        "disabled": auth.MSG_KEY_DISABLED,
        "tenant": auth.MSG_TENANT_DISABLED,
    }

    # 四种原因不能共用一句话：共用就意味着界面再也分不出该做哪件事。
    assert len(set(messages.values())) == 4
    for name, message in messages.items():
        assert any(hint in message for hint in ("Authorization", "Key 管理页", "管理员")), (
            f"{name} 的消息没有指向任何具体动作：{message}"
        )


@pytest.mark.asyncio
async def test_dev_mode_returns_the_built_in_identity_without_touching_the_database() -> None:
    """开关关闭：占位 token 直接换成内置开发身份，连一次查询都不发。"""

    from app.core.auth import KeyContext, resolve_identity

    class _ExplodingSession:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"开发身份不该碰数据库：{name}")

    context = await resolve_identity(_Request(_dev_headers()["authorization"]), _ExplodingSession())

    assert isinstance(context, KeyContext)
    assert context.tenant_id == settings.DEV_TENANT_ID
    assert context.key_label == "内置开发身份"


# ───────────────────────────────────────────── 开关打开：五种未授权，同一个码


#: 粘了一半的串：少了行身份证与密钥本体。它最容易发生在「复制时只选中了前一段」，
#: 格式当场就不成立，而「Key 不存在」那条提示会让人去核对一把其实完好的 Key。
MALFORMED_KEY = "rc_live_0123"


async def _failures(*, session: RecordingSession) -> dict[str, AppError]:
    """跑出五种未授权情形，按情形命名返回异常，供「同一个码、不同消息」比对。"""

    from app.core.auth import resolve_identity

    cases = {
        "missing": _Request(None),
        "malformed": _Request(f"Bearer {MALFORMED_KEY}"),
        "unknown": _Request(f"Bearer {KEY_VALUE}"),
        "key_disabled": _Request(f"Bearer {KEY_VALUE}"),
        "tenant_disabled": _Request(f"Bearer {KEY_VALUE}"),
    }
    sessions = {
        "missing": session,
        "malformed": session,
        "unknown": _session(keys=[], tenants=[_tenant_row()], policies=[_policy_row()]),
        "key_disabled": _session(
            keys=[_key_row(status="disabled")],
            tenants=[_tenant_row()],
            policies=[_policy_row()],
        ),
        "tenant_disabled": _session(
            keys=[_key_row()],
            tenants=[_tenant_row(status="disabled")],
            policies=[_policy_row()],
        ),
    }

    caught: dict[str, AppError] = {}
    for name, request in cases.items():
        with pytest.raises(AppError) as excinfo:
            await resolve_identity(request, sessions[name], auth_enabled=True)
        caught[name] = excinfo.value
    return caught


@pytest.mark.asyncio
async def test_switch_on_rejects_a_missing_token() -> None:
    from app.core.auth import resolve_identity

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(_Request(None), _session(), auth_enabled=True)

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_switch_on_rejects_an_unknown_token() -> None:
    from app.core.auth import resolve_identity

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            _session(keys=[], tenants=[_tenant_row()], policies=[_policy_row()]),
            auth_enabled=True,
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_all_five_unauthorized_cases_share_the_code_and_stay_distinguishable() -> None:
    """五种情形**同一个**错误码，消息可区分——交付判据就是这么写的。"""

    caught = await _failures(session=_session(keys=[_key_row()], tenants=[_tenant_row()]))

    assert {exc.code for exc in caught.values()} == {ErrorCode.UNAUTHORIZED}
    messages = {name: exc.msg for name, exc in caught.items()}
    assert len(set(messages.values())) == 5, f"五种情形必须能分辨，实际是 {messages}"


@pytest.mark.asyncio
async def test_a_wrong_secret_is_indistinguishable_from_an_unknown_key() -> None:
    """key_id 对、密钥本体错：回的是「Key 不存在」，不是「密钥不对」。

    后者会告诉任何人「这个 key_id 是真的」——而 key_id 就印在面板的 URL 与列表里。
    """

    from app.core.auth import MSG_UNKNOWN_KEY, resolve_identity

    wrong = "rc_live_" + KEY_ID + "." + "0" * 32
    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()])

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(_Request(f"Bearer {wrong}"), session, auth_enabled=True)

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED
    assert excinfo.value.msg == MSG_UNKNOWN_KEY


@pytest.mark.asyncio
async def test_a_verified_key_is_not_hashed_again_within_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验缓存命中：同一个 token 第二次不再付一次 scrypt。"""

    from app.core import auth as auth_module
    from app.core.auth import resolve_identity
    from tests.fake_db import MemoryKeyVerifyCache

    calls: list[str] = []
    real_verify = auth_module.verify_password

    def _counting(secret: str, encoded: str) -> bool:
        calls.append(secret)
        return real_verify(secret, encoded)

    monkeypatch.setattr(auth_module, "verify_password", _counting)

    cache = MemoryKeyVerifyCache()
    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()])

    for _ in range(2):
        await resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            session,
            auth_enabled=True,
            plan_cache=MemoryPlanCache(),
            verify_cache=cache,
        )

    assert len(calls) == 1, "第二次必须走缓存，不再算一遍 scrypt"
    assert cache.remembered == [(KEY_VALUE, KEY_ID)]
    # 缓存键是原文，值才是 key_id：Redis 里不出现 token，也不会出现「按 key_id 反查」的索引。
    assert list(cache.items) == [KEY_VALUE]


@pytest.mark.asyncio
async def test_a_cached_token_still_needs_the_row() -> None:
    """缓存只省 scrypt，**不省读库**：Key 被删之后那串立刻不可用。

    这条是「缓存不负责撤销」的证据：命中缓存也照样按 key_id 读一次行，读不到就是未授权，
    所以删 Key 不必等 60 秒。
    """

    from app.core.auth import resolve_identity
    from tests.fake_db import MemoryKeyVerifyCache

    cache = MemoryKeyVerifyCache()
    cache.items[KEY_VALUE] = KEY_ID
    session = _session(keys=[], tenants=[_tenant_row()], policies=[_policy_row()])

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            session,
            auth_enabled=True,
            plan_cache=MemoryPlanCache(),
            verify_cache=cache,
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_the_unauthorized_error_is_a_business_failure_not_a_401() -> None:
    """内核抛的是业务异常，不是 HTTP 异常：能不能变 401 由入口适配器决定（ADR-0007）。"""

    caught = await _failures(session=_session(keys=[_key_row()], tenants=[_tenant_row()]))

    for exc in caught.values():
        assert isinstance(exc, AppError)
        assert not hasattr(exc, "status_code")


@pytest.mark.asyncio
async def test_switch_on_makes_the_development_identity_unreachable() -> None:
    """开关打开后，占位 token 不再是身份来源：它不在 api_keys 表里，一律未授权。

    这条与「关闭时仍要求占位 token」是同一枚硬币的两面：开发身份只存在于开关关闭的
    那条分支，打开后连它自己都得凭真 Key 说话。
    """

    from app.core.auth import resolve_identity

    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()])

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(
            _Request(_dev_headers()["authorization"]), session, auth_enabled=True
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


# ───────────────────────────────────────────── 开关打开：真 Key 解出完整上下文


@pytest.mark.asyncio
async def test_an_active_key_resolves_to_its_tenant_policy_and_plan() -> None:
    """一把能用的 Key 解出租户、Key 备注名、配置快照、套餐快照四样东西。"""

    from app.core.auth import KeyContext, resolve_identity
    from app.models.key_policy import KeyPolicySnapshot

    cache = MemoryPlanCache()
    context = await resolve_identity(
        _Request(f"Bearer {KEY_VALUE}"),
        _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[_policy_row()]),
        auth_enabled=True,
        plan_cache=cache,
    )

    assert isinstance(context, KeyContext)
    assert (context.tenant_id, context.tenant_name) == (TENANT_ID, "Acme 公司")
    assert (context.key_id, context.key_label) == (KEY_ID, "控制台调试")
    # 期望值逐字段抄自假行（独立来源），不是把映射函数再算一遍。
    assert context.policy == KeyPolicySnapshot(
        allowed_kb_ids=("kb-1",),
        research_allowed=True,
        rerank_allowed=False,
        retrieve_qps=7,
        retrieve_daily=70,
        include_parent=False,
        expand_table_rows=True,
        # 第 16 步 #207：假行没显式写这一列，按迁移的 server_default 回填读出 2000。
        max_unit_chars=2000,
        # 第 13 步：三项参数从假行的真列读（_policy_row 显式写了 mode=vector/top_k=3）。
        retrieval_mode="vector",
        retrieval_top_k=3,
        retrieval_rerank=False,
        # 授权位与三本账：假行没显式配，按迁移的 server_default 回填读出的值。
        retrieve_allowed=True,
        rewrite_allowed=False,
        decompose_allowed=False,
        rerank_qps=0,
        rerank_daily=0,
        llm_qps=0,
        llm_daily=0,
    )
    assert context.plan == PLAN_PRESETS[PLAN_PRO]
    assert cache.set_calls == [PLAN_IDS[PLAN_PRO]], "套餐缓存的键必须是 plan id"


@pytest.mark.asyncio
async def test_disabling_a_tenant_takes_effect_on_the_next_call() -> None:
    """停用租户立刻生效：判定每次都重新读状态，身份不做长缓存。"""

    from app.core.auth import resolve_identity

    tenant = _tenant_row()
    session = _session(keys=[_key_row()], tenants=[tenant], policies=[_policy_row()])
    cache = MemoryPlanCache()

    first = await resolve_identity(
        _Request(f"Bearer {KEY_VALUE}"), session, auth_enabled=True, plan_cache=cache
    )
    assert first.tenant_id == TENANT_ID

    tenant.status = "disabled"

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"), session, auth_enabled=True, plan_cache=cache
        )
    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_a_key_pointing_at_a_deleted_policy_is_unauthorized() -> None:
    """删掉密钥配置 → 引用它的 Key 级联失效（ADR-0018），判定上等同于停用。"""

    from app.core.auth import resolve_identity

    session = _session(keys=[_key_row()], tenants=[_tenant_row()], policies=[])

    with pytest.raises(AppError) as excinfo:
        await resolve_identity(
            _Request(f"Bearer {KEY_VALUE}"),
            session,
            auth_enabled=True,
            plan_cache=MemoryPlanCache(),
        )

    assert excinfo.value.code is ErrorCode.UNAUTHORIZED


# ───────────────────────────────────────────── 套餐解析：按 plan id 缓存


@pytest.mark.asyncio
async def test_resolve_plan_reads_the_row_on_a_miss_and_caches_it() -> None:
    from app.services.plan_service import resolve_plan

    cache = MemoryPlanCache()

    plan = await resolve_plan(_session(), PLAN_IDS[PLAN_PLUS], cache=cache)

    assert plan == PLAN_PRESETS[PLAN_PLUS]
    assert cache.set_calls == [PLAN_IDS[PLAN_PLUS]]


@pytest.mark.asyncio
async def test_resolve_plan_serves_from_the_cache_without_touching_the_database() -> None:
    from app.services.plan_service import resolve_plan

    class _ExplodingSession:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"命中缓存就不该读库：{name}")

    cache = MemoryPlanCache()
    cache.items[PLAN_IDS[PLAN_PRO]] = PLAN_PRESETS[PLAN_PRO]

    plan = await resolve_plan(_ExplodingSession(), PLAN_IDS[PLAN_PRO], cache=cache)

    assert plan == PLAN_PRESETS[PLAN_PRO]
    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_resolve_plan_reports_a_missing_plan_as_plan_not_found() -> None:
    from app.services.plan_service import resolve_plan

    with pytest.raises(AppError) as excinfo:
        await resolve_plan(_session(), 99, cache=MemoryPlanCache())

    assert excinfo.value.code is ErrorCode.PLAN_NOT_FOUND


@pytest.mark.asyncio
async def test_invalidation_deletes_the_cache_entry_instead_of_writing_through() -> None:
    """改完套餐只删缓存、不写缓存：写缓存要求每处修改都记得同步，删则由下一次读兜住。"""

    from app.services.plan_service import invalidate_plan

    cache = MemoryPlanCache()
    cache.items[PLAN_IDS[PLAN_PRO]] = PLAN_PRESETS[PLAN_PRO]

    await invalidate_plan(cache, PLAN_IDS[PLAN_PRO])

    assert cache.deleted == [PLAN_IDS[PLAN_PRO]]
    assert cache.set_calls == []
    assert cache.items == {}


class _FakeRedis:
    """``redis.asyncio`` 的最小替身：只实现套餐缓存用到的那三条命令。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.expiries[key] = ttl

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.expiries.pop(key, None)


@pytest.mark.asyncio
async def test_redis_plan_cache_keys_by_plan_id_and_round_trips_the_snapshot() -> None:
    """缓存键是 **plan id**：管理员改个 code 不该让半个缓存失联。"""

    from app.core.plan_cache import RedisPlanCache

    client = _FakeRedis()
    cache = RedisPlanCache(client, ttl_seconds=60)

    await cache.set(PLAN_IDS[PLAN_PLUS], PLAN_PRESETS[PLAN_PLUS])

    assert list(client.values) == [f"plan:{PLAN_IDS[PLAN_PLUS]}"]
    assert client.expiries == {f"plan:{PLAN_IDS[PLAN_PLUS]}": 60}
    assert await cache.get(PLAN_IDS[PLAN_PLUS]) == PLAN_PRESETS[PLAN_PLUS]
    assert await cache.get(PLAN_IDS[PLAN_PRO]) is None


@pytest.mark.asyncio
async def test_renaming_a_plan_code_keeps_the_same_cache_key() -> None:
    from dataclasses import replace

    from app.core.plan_cache import RedisPlanCache

    client = _FakeRedis()
    cache = RedisPlanCache(client, ttl_seconds=60)
    renamed = replace(PLAN_PRESETS[PLAN_PRO], code="pro-max")

    await cache.set(PLAN_IDS[PLAN_PRO], PLAN_PRESETS[PLAN_PRO])
    await cache.set(PLAN_IDS[PLAN_PRO], renamed)

    assert list(client.values) == [f"plan:{PLAN_IDS[PLAN_PRO]}"], (
        "键里没有 code，改名不产生第二个键"
    )
    assert await cache.get(PLAN_IDS[PLAN_PRO]) == renamed


# ───────────────────────────────────────────── 守卫：内核不许认识框架


def test_kernel_module_imports_no_web_or_mcp_framework() -> None:
    """源码级守卫：这条约束不能只靠自觉，改起来太容易（一次补类型标注就够了）。"""

    import app.core.auth as kernel

    tree = ast.parse(Path(kernel.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"fastapi", "fastmcp", "starlette", "flask", "django"}
