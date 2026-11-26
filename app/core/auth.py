"""唯一一份判定内核：给一个请求对象与一个数据库会话，它解出「这次是谁」。

**这个模块不 import 任何 Web 或 MCP 框架**，判断依据只有「一个带 ``headers`` 的对象」。
理由很具体：HTTP 路由走 FastAPI 的依赖注入，MCP 工具走 fastmcp 的请求上下文，两条入口
的「怎么拿到请求」天生不同；但只要判定只有这一份实现，两边就不会随时间漂移出一套
「HTTP 认 A、MCP 认 B」的规则。两个入口各自只剩一层薄适配器（``app/api/deps.py`` 与
``app/mcp_server.py``），它们唯一的工作是把请求对象递进来。

解析顺序（顺序本身是判定的一部分）：

1. 读出 Bearer token —— 没有就是「未授权」；
2. 拆 Key 串：``rc_live_<key_id>.<32 位十六进制>``，形状不对就是「格式不对」；
3. 用串里的 ``key_id`` 一次主键命中 ``api_keys`` —— 库里只存哈希，没有可以做等值索引的
   原文，行定位只能靠 Key 自己携带的身份证；
4. 密钥本体过 ``scrypt`` 校验（:func:`app.core.passwords.verify_password`）；同一个 token
   在 60 秒内验过的走校验缓存，跳过这一步；
5. Key 必须存在且 ``active``；
6. Key 归属的租户必须存在且 ``active``；
7. 组装上下文（租户、Key、密钥配置快照、套餐快照）。

五种失败全部映射到 :attr:`ErrorCode.UNAUTHORIZED`（``20010``），**消息可区分**：调用方
需要知道「是少了 token、格式粘错了、还是 Key 被停了」，但对外不区分「Key 不存在」与
「Key 属于别人」——业务失败不是协议错误，HTTP 入口统一翻成 200 + 非零 ``code``
（ADR-0007），这不是 401。

**开发开关**（``AUTH_ENABLED``）关闭时落进内置开发身份，但仍然要求那把固定的占位 token：
退回「谁都不用带」意味着一个被意外暴露的开发实例对整个网络敞开，而要求这把固定 token
对开发者零成本（它本来就写在 ``.env`` 里），同时让既有测试继续有意义——它们断言的正是
「不带 token 会失败」。开关打开后，开发身份不可达：它不在 ``api_keys`` 表里，因此会
像任何一把陌生 Key 一样被判为未授权。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.core.key_verify_cache import KeyVerifyCache, get_default_key_verify_cache
from app.core.plan_cache import PlanCache
from app.core.passwords import verify_password
from app.models.key_policy import KeyPolicySnapshot, policy_snapshot_of
from app.models.plan import PLAN_PRO, PLAN_PRESETS, PlanSpec
from app.providers.rerank import rerank_capability
from app.repositories import api_key_repository, key_policy_repository, tenant_repository
from app.services.plan_service import resolve_plan
from app.utils.id_generator import split_api_key_value


class HttpRequestLike(Protocol):
    """内核眼里的「请求」：只要有大小写不敏感的 ``headers`` 就够了。

    Starlette 的 ``Request``、测试里的替身都满足这个形状。写成 Protocol 而不是导入
    ``fastapi.Request``，是「内核不认识 Web 框架」这条约束的落地方式——不是靠自觉。
    """

    @property
    def headers(self) -> Mapping[str, str]: ...


#: 内置开发身份的 Key 标识与备注名：它是合成的，不来自 ``api_keys`` 表。
DEV_KEY_ID: Final[str] = "dev"
DEV_KEY_LABEL: Final[str] = "内置开发身份"

#: 内置开发身份的密钥配置快照：**全能力 + custom 档 + 两种形状都开**。
#:
#: - 五个授权位全开：开发身份不因配置层的开关改变现行为——重排是否真的跑，仍由服务端
#:   配置（``RERANK_ENABLED``）与本机是否装了重排端点决定；
#: - 三项参数照抄部署缺省（``RETRIEVAL_MODE`` / ``TOP_K`` / 重排能力）：开发身份唯一的
#:   正当语义是「跟部署配置完全一致」。从前这里钉在 ``custom`` + 空 ``custom_options``
#:   上表达同一件事，档位退役后直接写值即可，不必再借一个名字；
#: - 两种形状偏好都开：与既有响应默认一致（折叠成整节、表格行折回整表）；
#: - 限额照抄最高档：开发身份没有套餐主体，额度判定不该在本地把开发自己限住。
#:
#: 套餐仍取最高档（``DEV_KEY_PLAN``）：它是「本地演示要看完整能力」的选择，同时保证了
#: 各道门禁在本地永不会把开发自己拦下来。
#:
#: ``allowed_kb_ids`` 是空元组：仓库里的约定是「空 = 还没圈定，不是全部」，而内置开发
#: 身份没有白名单可言。空元组在这里**只表示这一件事**——「不做范围收窄」由
#: ``unrestricted_scope=True`` 显式声明（数据范围关卡读的就是它）。若靠空列表兼职表达
#: 「全部」，真实配置清空白名单就会变成开闸，而那是要失败关闭的一侧。
DEV_KEY_POLICY: Final[KeyPolicySnapshot] = KeyPolicySnapshot(
    allowed_kb_ids=(),
    retrieve_allowed=True,
    rewrite_allowed=True,
    decompose_allowed=True,
    research_allowed=True,
    rerank_allowed=True,
    retrieve_qps=PLAN_PRESETS[PLAN_PRO].retrieve_qps,
    retrieve_daily=PLAN_PRESETS[PLAN_PRO].retrieve_daily,
    # 加工能力的两本账照抄最高档：开发身份要能演示整条链路，而「0 = 未启用」会让
    # 本地开发里的重排与改写直接撞额度。
    rerank_qps=PLAN_PRESETS[PLAN_PRO].rerank_qps,
    rerank_daily=PLAN_PRESETS[PLAN_PRO].rerank_daily,
    llm_qps=PLAN_PRESETS[PLAN_PRO].llm_qps,
    llm_daily=PLAN_PRESETS[PLAN_PRO].llm_daily,
    retrieval_mode=settings.RETRIEVAL_MODE,
    retrieval_top_k=settings.TOP_K,
    retrieval_rerank=rerank_capability(),
    include_parent=True,
    max_unit_chars=2000,
    expand_table_rows=True,
    unrestricted_scope=True,
)

#: 内置开发身份的套餐快照：最高档（演示要看的是完整能力）。
DEV_KEY_PLAN: Final[PlanSpec] = PLAN_PRESETS[PLAN_PRO]


@dataclass(frozen=True)
class KeyContext:
    """判定结论：这次是谁、能用哪套密钥配置、买的是哪一档。

    ``tenant_id`` 之后的字段**全部有默认值**，默认值就是内置开发身份的那一份快照。
    这不是为了好看：16 个既有测试文件用 ``DevKeyContext(tenant_id=TENANT_ID)`` 造上下文
    来覆盖依赖，构造点只给一个租户标识。依赖函数名已经收口成 ``get_current_key``，但
    构造点不变，所以这些默认值继续留着——删它们要另改 16 处，收益只是「字段都得显式写」。

    展示字段（``tenant_name`` / ``key_label``）默认为空：只给了一个租户标识的构造点
    并不知道这些值，填一个「看起来合理」的假名字比留空更糟。
    """

    tenant_id: str
    tenant_name: str = ""
    key_id: str = ""
    key_label: str | None = None
    policy: KeyPolicySnapshot = DEV_KEY_POLICY
    plan: PlanSpec = DEV_KEY_PLAN


#: 兼容别名：路由与既有测试按 ``DevKeyContext`` 引用这个上下文类型。
#: 本票收口的是**依赖函数名**（``get_current_key``，可调用对象身份）；类型名不在其中——
#: 改它要连带 16 个测试文件的构造点，收益只是名字更好听。
DevKeyContext = KeyContext

# 五种未授权情形各自的提示。**同一档错误码、不同消息**：调用方分得清是哪一步拦下的，
# 运维看日志也不用靠猜。措辞刻意避开「Key 不属于这个租户」之类的信息——那等于告诉
# 攻击者「这把 Key 是真的，只是不属于你」。
MSG_MISSING_TOKEN = "请求没带 Bearer 令牌：把 API Key 放进 Authorization: Bearer <key>"
#: 格式这一条是「粘贴」这种交付方式带来的：串被截断、少了一位、或者粘进来的是半截
#: ``key_masked``（``rc_live_…4f3a2``）。不单列的话，它会被当成「Key 不存在」，
#: 而那个提示会让人去核对一把其实完全正常的 Key。
MSG_KEY_MALFORMED = (
    "这把 API Key 的格式不对：它应当是 rc_live_<key_id>.<32 位密钥>，"
    "从 Key 管理页整条复制一次（列表里显示的是打码形态，不能拿来调用）"
)
MSG_UNKNOWN_KEY = "这把 API Key 不存在：去 Key 管理页确认它是否已被删除，或重新签发一把"
MSG_KEY_DISABLED = "这把 API Key 已被停用：去 Key 管理页把它启用，或换一把"
MSG_TENANT_DISABLED = "这把 Key 所属的租户已被停用：联系管理员恢复租户后再用"
#: 开发身份模式下收到了一把**真实 Key**。它与「Key 不存在」是两件事，必须分开说。
#:
#: 踩过的坑：控制台能签发 Key、能显示、能选中，但 `AUTH_ENABLED=false` 时服务端只认内置
#: 占位 Key，于是真实 Key 一律 `20010`。原来的文案是 `invalid api key`，界面把它翻成
#: 「去 Key 管理页换一把」——**而换多少把都没用**，因为开关不在 Key 上。这条消息把原因
#: 直接点名，省掉一整圈无效排查。
#:
#: 不含占位 Key 的**值**：那是凭据，不该由一条错误消息发出去。
MSG_DEV_MODE_REJECTS_REAL_KEY = (
    "服务端处于开发身份模式（AUTH_ENABLED=false）：只接受内置占位 Key，"
    "控制台签发的 Key 在此模式下不参与鉴权。要用真实 Key，请把 AUTH_ENABLED 设为 true 并重启 API"
)


async def resolve_identity(
    request: HttpRequestLike,
    db: AsyncSession,
    *,
    auth_enabled: bool | None = None,
    plan_cache: PlanCache | None = None,
    verify_cache: KeyVerifyCache | None = None,
) -> KeyContext:
    """判定这次请求是谁；失败一律抛 ``AppError(UNAUTHORIZED)``。

    ``auth_enabled`` 缺省读服务端配置。做成入参是为了让「开关开 / 关」两种行为都能被
    纯函数直接测到——不必 monkeypatch 全局配置，也就不会在测试之间留下污染。

    ``plan_cache`` / ``verify_cache`` 是两个缓存的注入口（生产传 Redis 实现，测试传内存
    假实现）；缺省时各自取进程级默认实现。两个缓存的职责完全不同：一个省「读套餐行」，
    一个省「重算 scrypt」，谁也补不了对方的洞。

    **``async`` 是票 #130 的收尾**：套餐缓存是 ``redis.asyncio``，读库是
    ``AsyncSession``，两者都只能在事件循环上 ``await``。迁移期那个「把协程交回主循环」
    的过渡桥因此随本函数一起删掉了——判定内核现在是 HTTP 与 MCP 两条入口共用的**同一个**
    协程，两个薄适配器直接 ``await`` 它。
    """

    token = _bearer_token(request)
    if token is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_MISSING_TOKEN)

    enabled = settings.AUTH_ENABLED if auth_enabled is None else auth_enabled
    if not enabled:
        if token != settings.DEV_API_KEY:
            # 开关关着时**任何**真实 Key 都到不了 `api_keys` 那张表，所以不能回
            # `MSG_UNKNOWN_KEY`：那句话会让调用方以为 Key 本身有问题，于是反复重签——
            # 而真正要改的是服务端开关。这条消息是本轮根据真实反馈补的。
            raise AppError(ErrorCode.UNAUTHORIZED, MSG_DEV_MODE_REJECTS_REAL_KEY)
        return _dev_identity()

    return await _identity_from_key(db, token, plan_cache=plan_cache, verify_cache=verify_cache)


def _dev_identity() -> KeyContext:
    """内置开发身份：身份、配置、套餐都由服务端配置合成。"""

    return KeyContext(
        tenant_id=settings.DEV_TENANT_ID,
        tenant_name=settings.DEV_TENANT_ID,
        key_id=DEV_KEY_ID,
        key_label=DEV_KEY_LABEL,
        policy=DEV_KEY_POLICY,
        plan=DEV_KEY_PLAN,
    )


async def _identity_from_key(
    db: AsyncSession,
    token: str,
    *,
    plan_cache: PlanCache | None,
    verify_cache: KeyVerifyCache | None,
) -> KeyContext:
    """开关打开后的真实路径：Key 串 → 行 → 密钥校验 → 租户 → 密钥配置 → 套餐。"""

    parsed = split_api_key_value(token)
    if parsed is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_KEY_MALFORMED)
    key_id, secret = parsed

    # 行定位靠串里的身份证，不靠原文——库里只有哈希，没有可做等值索引的东西。
    key = await api_key_repository.get_by_id(db, key_id)
    if key is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_UNKNOWN_KEY)

    await _ensure_secret_matches(
        token, secret, key_id=key_id, key_hash=key.key_hash, verify_cache=verify_cache
    )

    if key.status != "active":
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_KEY_DISABLED)

    tenant = await tenant_repository.get_by_id_async(db, key.tenant_id)
    if tenant is None or tenant.status != "active":
        # 缺行与停用同一个消息：对调用方来说都是「这个租户现在不能用」，而多一种消息
        # 只会让人以为租户被删了、去查一张根本不该手动删的表。
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_TENANT_DISABLED)

    policy = await key_policy_repository.get_by_id_async(db, key.policy_id)
    if policy is None:
        # 删掉配置时引用它的 Key 级联失效（ADR-0018）：Key 本身还是 active，但它已经
        # 没有一套可用的配置，判定上等同于停用。
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_KEY_DISABLED)

    plan = await resolve_plan(db, tenant.plan_id, cache=plan_cache)

    return KeyContext(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        key_id=key.id,
        key_label=key.label,
        policy=policy_snapshot_of(policy),
        plan=plan,
    )


async def _ensure_secret_matches(
    token: str,
    secret: str,
    *,
    key_id: str,
    key_hash: str,
    verify_cache: KeyVerifyCache | None,
) -> None:
    """密钥本体过 scrypt 校验，同一个 token 在缓存窗口内只算一次。

    **先校验、后看 ``status``**：反过来会让「只是一串 key_id、没有密钥」的人也能问出
    「这一行是不是被停用了」。代价是停用后的请求每次都要白烧一次 scrypt，这正是缓存要
    摊掉的那部分。

    校验失败回的是 :data:`MSG_UNKNOWN_KEY`（不是「密钥不对」）：对调用方来说效果一样
    ——换一把 Key；而这个措辞不告诉任何人「这个 key_id 是真的」。
    """

    cache = verify_cache if verify_cache is not None else get_default_key_verify_cache()
    if await cache.key_id_of(token) == key_id:
        return
    if not verify_password(secret, key_hash):
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_UNKNOWN_KEY)
    await cache.remember(token, key_id)


def _bearer_token(request: HttpRequestLike) -> str | None:
    """从 ``Authorization: Bearer <token>`` 里取 token；取不到返回 ``None``。

    方案名大小写不敏感（RFC 7235），token 两侧空白一律去掉。空 token 当成「没带」：
    它可以过掉「有没有这个头」的判断，却永远匹配不上任何一把 Key。
    """

    header = request.headers.get("authorization", "")
    scheme, _, token = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
