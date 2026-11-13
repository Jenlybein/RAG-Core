"""两级配额与限流：一次检索「用多少」的**唯一**判定处（票 #79）。

六道关卡里，插在本模块两道关卡中间的是：鉴权 → 白名单 → 套餐解析 → 档位门禁 →
**限流** → **日配额** → 业务执行 → 成功后才计数（ADR-0018 第 4 条）。本模块同时提供
「进门查一次」与「成功后记一次」两半，因为它们是同一本账的两端——分成两个模块，
迟早有一处忘了带同样的 ``tenant_id`` / ``key_id``。

## 两级同时记，缺一不可

| 层级 | 卡住什么 | 额度来源 |
| --- | --- | --- |
| **Key 级** | 单个外部应用跑飞 | 密钥配置的 ``retrieve_qps`` / ``retrieve_daily`` |
| **租户级** | 同一租户下所有 Key 的聚合 | 套餐的 ``retrieve_qps`` / ``retrieve_daily`` |

只记 Key 级，一个应用多签几把 Key 就绕过了上限；只记租户级，一个应用跑飞会吃掉整个
租户的日额度，让其他应用陪跑（GLOSSARY「配额两级」）。所以每次调用在两级各记一份。

## 两种计数形状，刻意不同

* **QPS 是秒级桶**：键里嵌着 epoch 秒，``INCR`` 之后设一个很短（:data:`QPS_TTL_SECONDS`）
  的过期。窗口因此自己往前滑——不需要维护一个列表，也不会因为「上一个窗口的残留」
  把下一秒的额度提前吃掉。
* **日配额是按天分桶**：键里嵌着 ``YYYYMMDD``，过期时间**略长于 24 小时**
  （:data:`DAILY_TTL_SECONDS`）。理由只有一个：跨零点的那几秒里，一次进门在 23:59:59、
  记账在 00:00:0x 的调用必须落回**它进门那一天**的桶；桶要是正好在零点消失，那次调用
  要么白记，要么在新的一天重新开一个桶（等于凭空多一份额度）。

**桶在进门时定死**（:class:`QuotaWindow`），一次请求从头到尾属于同一秒、同一天。换句
话说，「查」与「记」看的是同一个键——否则进门查 A 秒、记账写 B 秒，窗口的正确性就取决
于请求跑得多快。

## 两本账：检索一本，Research 一本（票 #150）

**形状完全同构，键前缀不同**。检索用 ``quota:*``，Research 用 ``quota:research:*``：

| 这本账 | 上限来自 | 键前缀 |
| --- | --- | --- |
| 检索 | ``retrieve_qps`` / ``retrieve_daily`` | ``quota`` |
| 受控 Research | ``research_qps`` / ``research_daily`` | ``quota:research`` |

分成两本账是因为成本相差一个量级：一次 Research 约 30 秒、3 次模型调用，与普通检索共用
一份额度会让「偶尔问一次大问题」把当天的检索额度吃掉。两本账共用同一份实现
（:class:`QuotaLedger` + :class:`QuotaLimits`），差别只有「读哪两列」与「写哪个前缀」——
所以「两级同记、只有成功才记、两个错误码」这三条纪律不会在某一天只落到一条链路上。

Research 那两列的**0 表示关闭**（与 ``research_allowed=False`` 同一个答复码 ``20013``），
这个判定不在本模块：它在门禁（``app.services.research_gate``）的「能力位」那一步，
因为它是配置事实、与计数开关无关。

## 只有成功才记账，所以「查」与「记」必须分开

ADR-0018：被拒绝与失败的请求不扣额度。于是门禁**不能**在进门时 ``INCR``（那是「先扣
再干」），只能读、判、放行，等业务真的成功了再 ``INCR``（``record_success``）。代价是
并发下的秒级读-判-写不是原子的：同一秒里 N 个请求可能一起看到「还剩 1 次」并全部放行。
这个偏差方向是可接受的——**它是「放行」而不是「误拒」**，而另一种做法（进门就扣）会把
「请求失败也扣额度」写进系统里，那正是本票明确禁止的。

## 两个错误码，不能混

* 秒级超限 → :attr:`~app.core.exceptions.ErrorCode.API_RATE_LIMIT`（``20005``），
  调用方该做的是**退避重试**；
* 任何额度用尽 → :attr:`~app.core.exceptions.ErrorCode.QUOTA_EXCEEDED`（``20014``），
  重试没有意义，该做的是**提额或换套餐**。

混成一个码，调用方就没法写重试策略；而「两种超限都返回 20014」这种合并是最常见的
抄近路。

## 为什么是一个开关，而不是「连不上 Redis 就自动关掉」

``RATE_LIMIT_ENABLED`` 默认**关闭**，与 ``AUTH_ENABLED`` 同一个精神：本地开发与默认
测试套件全程离线，一条真实 Redis 都不需要。

**这个开关是刻意做成配置项的，不是「Redis 不可达时自动降级」**：自动降级是本票最坏的
失败模式——Redis 一挂，所有租户的额度同时变成无限，平台看起来完全健康（每一次调用都
成功），直到某天账单或 Worker 队列把真相捅出来。「现在到底有没有在限流」必须是一个
**有人显式配过的决定**，而不是一次网络抖动的副产品。生产必须打开它。

静态资源配额（库数 / 单库文档数 / 并发处理数）不走这里：它们是「库里现在有多少行」，
在写路径上直接数库，见 :mod:`app.services.static_quota`。

## 计数命令是异步的（票 #133）

门禁在检索热路径上，而那条路径已经是 ``async def``：同步 Redis 调用会把整个事件循环
按住，于是一次慢的 Redis 就变成所有并发请求一起慢。所以计数命令全部 ``await``，
客户端来自 :func:`app.core.redis_client.get_redis_client`（``redis.asyncio``）。

**换成异步不改任何判定**：窗口口径（UTC）、两级同时记、只有成功才记账、两个错误码、
``RATE_LIMIT_ENABLED`` 这个显式开关（而不是「连不上就自动降级」）全部原样保留。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Protocol

from app.core import redis_client
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.models.key_policy import KeyPolicySnapshot
from app.models.plan import PlanSpec
from app.services.quota_limits import ensure_under_limit

#: 两级计数的两个名字。直接用字面量拼进键里（``quota:qps:tenant:...``），
#: 运维一眼能看出这个键属于哪一级，而不必回来读代码。
LEVEL_KEY: Final[str] = "key"
LEVEL_TENANT: Final[str] = "tenant"

#: 两本账的键前缀。检索那本**逐字不变**（``quota``）——它已经在生产上跑着，改前缀等于
#: 把当天所有人的用量清零；Research 那本多一段 ``research``，于是两本账互不相干。
KEY_NAMESPACE: Final[str] = "quota"
RESEARCH_NAMESPACE: Final[str] = "quota:research"

#: 键形状。``quota:`` 前缀与套餐缓存（``plan:``）、Celery 的 0/1 号库都分开——
#: 同一个 Redis 实例上，前缀是唯一能让人分清「哪些键可以清」的东西。
QPS_KEY_TEMPLATE: Final[str] = "{namespace}:qps:{level}:{ident}:{window}"
DAILY_KEY_TEMPLATE: Final[str] = "{namespace}:daily:{level}:{ident}:{window}"

#: QPS 桶的存活时间：只需要覆盖它自己那一秒，再给多 worker 之间不到 1 秒的时钟偏差留
#: 一点余量。取 2 秒而不是 1 秒也顺手覆盖了「进门查、成功后才记」这段间隔——一次慢
#: 请求（>2 秒）记账时桶已过期也没关系：它记的是**自己进门那一秒**的键，重建出来依然
#: 属于那个窗口，只是 TTL 重新开始算。
QPS_TTL_SECONDS: Final[int] = 2

#: 日桶的存活时间：24 小时 + 2 小时余量。余量不是给时区的，是给「跨零点的在途请求」
#: 与多 worker 的时钟偏差的（见模块文档「两种计数形状」）。
DAILY_TTL_SECONDS: Final[int] = 26 * 3600


def _utc_now() -> datetime:
    """计数窗口按 UTC 算：多 worker 共享同一个 Redis，窗口口径必须不依赖本地时区。

    换成「服务器本地日期」的话，同一份额度会在跨时区的机器之间被切成两个桶——那不是
    「一天的额度」，而是「一片机器的额度」的并集。
    """

    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuotaWindow:
    """这次调用属于哪个窗口：一次请求进门时定死，查与记共用同一份。

    ``qps`` 是 epoch 秒（字符串），``daily`` 是 ``YYYYMMDD``。存成字符串是因为它们
    唯一的作用就是拼进键里；存成 ``datetime`` 只会让每个调用点多一次格式化。
    """

    qps: str
    daily: str


class CounterClient(Protocol):
    """计数器真正用到的三条 Redis 命令（异步形状，见模块文档「计数命令是异步的」）。

    刻意不写成 ``Redis``：本模块因此不 import redis 包，测试传一个 dict 伪装的**异步**
    对象就够（见 ``tests/test_quota_gate.py``），也不会有人顺手在这里加一条只有真 Redis
    才认的命令。
    """

    async def incr(self, key: str) -> Any: ...

    async def get(self, key: str) -> str | bytes | None: ...

    async def expire(self, key: str, ttl: int) -> Any: ...


def qps_key(
    *, level: str, ident: str, window: QuotaWindow, namespace: str = KEY_NAMESPACE
) -> str:
    """QPS 桶的键：``quota:qps:key:<key_id>:<epoch_second>``。

    ``namespace`` 缺省是检索那本账，所以既有调用点与用例里的键**逐字不变**；Research 传
    :data:`RESEARCH_NAMESPACE` 拿到 ``quota:research:qps:...``。
    """

    return QPS_KEY_TEMPLATE.format(
        namespace=namespace, level=level, ident=ident, window=window.qps
    )


def daily_key(
    *, level: str, ident: str, window: QuotaWindow, namespace: str = KEY_NAMESPACE
) -> str:
    """日配额桶的键：``quota:daily:tenant:<tenant_id>:<YYYYMMDD>``。"""

    return DAILY_KEY_TEMPLATE.format(
        namespace=namespace, level=level, ident=ident, window=window.daily
    )


class QuotaCounters:
    """两种计数形状的读写门面：注入客户端、注入时钟，其余全是纯逻辑。

    为什么把它做成一个对象而不是几个自由函数：键的拼法、TTL 的取值、``incr`` 之后必须
    ``expire`` 这三件事必须**一起**出现。一个忘了 ``expire`` 的调用会在 Redis 里留下一
    个永不过期的键——那个租户从此被自己上个月用掉的额度永久限住，而且没有任何报错。
    """

    def __init__(
        self,
        client: CounterClient,
        *,
        clock: Callable[[], datetime] | None = None,
        namespace: str = KEY_NAMESPACE,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else _utc_now
        #: 这本账写哪个前缀。一个计数器对象只服务一本账：两本账混在一个对象里，
        #: 「读检索、写 Research」这种错位不会有任何报错。
        self._namespace = namespace

    def window(self) -> QuotaWindow:
        """取当前窗口。一次请求只调一次，之后一路传下去（见模块文档）。"""

        moment = self._clock()
        return QuotaWindow(
            qps=str(int(moment.timestamp())),
            daily=moment.strftime("%Y%m%d"),
        )

    async def qps(self, *, level: str, ident: str, window: QuotaWindow) -> int:
        """读这一秒已经记了几次；没记过就是 0（``GET`` 的 ``None`` 不当成异常）。"""

        return _as_count(
            await self._client.get(
                qps_key(level=level, ident=ident, window=window, namespace=self._namespace)
            )
        )

    async def daily(self, *, level: str, ident: str, window: QuotaWindow) -> int:
        return _as_count(
            await self._client.get(
                daily_key(level=level, ident=ident, window=window, namespace=self._namespace)
            )
        )

    async def add_qps(self, *, level: str, ident: str, window: QuotaWindow) -> None:
        await self._add(
            qps_key(level=level, ident=ident, window=window, namespace=self._namespace),
            QPS_TTL_SECONDS,
        )

    async def add_daily(self, *, level: str, ident: str, window: QuotaWindow) -> None:
        await self._add(
            daily_key(level=level, ident=ident, window=window, namespace=self._namespace),
            DAILY_TTL_SECONDS,
        )

    async def _add(self, key: str, ttl: int) -> None:
        """``INCR`` 之后**立刻**续期：每次写都重置 TTL，桶的寿命从最后一次写算起。

        不写成「键不存在时设过期」的两步检查：那需要一条只有真 Redis 才原子的
        ``SET NX`` 语义，而这里对一个「本来就是短期窗口」的键，重复续期完全无害。
        """

        await self._client.incr(key)
        await self._client.expire(key, ttl)


def _as_count(raw: str | bytes | None) -> int:
    """把 ``GET`` 的结果读成计数：``None`` = 0，坏值也当 0。

    坏值（有人手工往这个键里写了非数字）不抛异常：限流函数在热路径上，让它因为一个
    脏键炸成 500 会把整条检索链路一起拖下去。当作 0 的方向是「放行」，与上面
    「读-判-写不是原子」的取舍一致。
    """

    if raw is None:
        return 0
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class QuotaLimits:
    """一次调用的四个上限 + 这本账的键前缀与报错口径。

    四个上限按「两级」摆开（Key 级卡单个应用、租户级聚合），两条链路各有一份来源：

    * 检索：密钥配置的 ``retrieve_qps`` / ``retrieve_daily`` 与套餐的同名列；
    * Research：密钥配置与套餐的 ``research_qps`` / ``research_daily``。

    ``namespace`` 决定写哪本账（见模块文档「两本账」）；``label`` 只进报错文案，
    于是「哪条链路超限了」在日志里可读，而判据与错误码仍然只有一份。
    """

    key_qps: int
    tenant_qps: int
    key_daily: int
    tenant_daily: int
    namespace: str = KEY_NAMESPACE
    label: str = ""

    @classmethod
    def for_retrieval(cls, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> QuotaLimits:
        """检索那本账：读 ``retrieve_*`` 四列，键前缀保持 ``quota``（逐字不变）。"""

        return cls(
            key_qps=policy.retrieve_qps,
            tenant_qps=plan.retrieve_qps,
            key_daily=policy.retrieve_daily,
            tenant_daily=plan.retrieve_daily,
        )

    @classmethod
    def for_research(cls, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> QuotaLimits:
        """Research 那本账：读 ``research_*`` 四列，键前缀多一段 ``research``。

        0 在这里**不该出现**：门禁的「能力位」那一步已经把 0 当成「关闭」拒绝掉了
        （``20013``）。真走到这里还带着 0，说明有人绕过了门禁，而此时按「额度已用尽」
        （``20014``）拒绝是更保守的一侧——它不会让一次调用白跑。
        """

        return cls(
            key_qps=policy.research_qps,
            tenant_qps=plan.research_qps,
            key_daily=policy.research_daily,
            tenant_daily=plan.research_daily,
            namespace=RESEARCH_NAMESPACE,
            label="research ",
        )


@dataclass(frozen=True)
class QuotaLedger:
    """一条链路的额度账本：进门查一次（:meth:`ensure_available`）、成功后记一次。

    两半必须成对出现——只查不记，额度永远用不完；只记不查，额度没有上限。调用方
    （``retrieval_gate.gated_retrieve`` / ``research_gate.gated_research``）的写法固定是：
    进门先 ``await ensure_available()``，业务返回之后**再** ``await record_success()``。
    业务失败或抛错时**不调**后者，被拒与失败的请求因此都不会扣额度。

    检索与 Research 共用这一个类，差别全在构造时传进来的 :class:`QuotaLimits` 里：
    一份实现意味着「两级同记」与「只有成功才记」这两条纪律不可能只落到一条链路上。
    """

    tenant_id: str
    key_id: str
    window: QuotaWindow
    limits: QuotaLimits
    counters: QuotaCounters | None = None
    enabled: bool = True

    async def ensure_available(self) -> None:
        """限流 → 日配额，任一层超限就抛错；**不记账**（记账要等成功）。"""

        if not self.enabled:
            return

        # 第一道：秒级限流。Key 级在前——它更窄、更可能是调用方自己要修的那件事
        # （「我这个应用打得太快」），而租户级超限往往要另一个应用的负责人一起看。
        # 判据只有一份（``quota_limits.ensure_under_limit``），静态配额用的也是它。
        ensure_under_limit(
            used=await self._read_qps(LEVEL_KEY, self.key_id),
            limit=self.limits.key_qps,
            code=ErrorCode.API_RATE_LIMIT,
            what=f"{self.limits.label}qps of key {self.key_id}",
            suffix="per window",
        )
        ensure_under_limit(
            used=await self._read_qps(LEVEL_TENANT, self.tenant_id),
            limit=self.limits.tenant_qps,
            code=ErrorCode.API_RATE_LIMIT,
            what=f"{self.limits.label}qps of tenant {self.tenant_id}",
            suffix="per window",
        )

        # 第二道：日配额。注意**比的是当前额度**，不是某个记下来的快照：管理员把日
        # 配额改小，下一次调用就按新值判（AC「改小日配额后立刻生效，不需要重启」）。
        ensure_under_limit(
            used=await self._read_daily(LEVEL_KEY, self.key_id),
            limit=self.limits.key_daily,
            code=ErrorCode.QUOTA_EXCEEDED,
            what=f"{self.limits.label}daily quota of key {self.key_id}",
            suffix="per window",
        )
        ensure_under_limit(
            used=await self._read_daily(LEVEL_TENANT, self.tenant_id),
            limit=self.limits.tenant_daily,
            code=ErrorCode.QUOTA_EXCEEDED,
            what=f"{self.limits.label}daily quota of tenant {self.tenant_id}",
            suffix="per window",
        )

    async def record_success(self) -> None:
        """业务成功之后才调它：两级各记一次，**一次请求只记一次**。

        「一次跨多库检索 / 一次跨多条子查询的 Research 只计一次」不是这里的判断——本对象
        根本不知道这次查了几个库、跑了几条子查询。正因为不知道，它才不可能按库 / 按子查询
        记账（票 #150 的 AC「一次 Research 只记一次」在结构上就成立）。
        """

        if not self.enabled or self.counters is None:
            return

        for level, ident in ((LEVEL_KEY, self.key_id), (LEVEL_TENANT, self.tenant_id)):
            await self.counters.add_qps(level=level, ident=ident, window=self.window)
            await self.counters.add_daily(level=level, ident=ident, window=self.window)

    async def _read_qps(self, level: str, ident: str) -> int:
        assert self.counters is not None  # 只有 enabled 时才会走到这里
        return await self.counters.qps(level=level, ident=ident, window=self.window)

    async def _read_daily(self, level: str, ident: str) -> int:
        assert self.counters is not None
        return await self.counters.daily(level=level, ident=ident, window=self.window)


async def read_tenant_daily_usage(
    *,
    tenant_id: str,
    counters: QuotaCounters | None = None,
    enabled: bool | None = None,
    namespace: str = KEY_NAMESPACE,
) -> int | None:
    """读租户级「今日已用」：**只发一条 GET**，绝不 ``INCR`` / ``EXPIRE``。

    面板刷新不是一次检索，读路径因此不能借「顺手初始化」把计数 +1，也不能靠写操作
    去探测键是否存在。这里与 :func:`retrieval_quota` / :func:`research_quota` 共用同一个
    窗口口径（UTC、按天分桶），所以控制台看到的数与门禁判的数一定是同一个桶。

    ``namespace`` 选读哪本账：控制面与 ``/auth/me`` 各报一栏（检索 / Research），两栏的
    形状相同（``used`` + ``limit`` + ``source=daily_counter``），只是桶不同。

    返回 ``None`` 表示**平台现在没有在数**：``RATE_LIMIT_ENABLED`` 关着时
    ``record_success`` 整段是 no-op，计数键根本不会被写出来。此时报 ``0`` 是编造——
    面板会显示「今日 0 次」，而实际用量可能已经几千次、只是没人统计。``0`` 只在开关
    打开、键确实不存在时出现，那才是「今天还没有一次成功调用」，是**已知的 0**。

    开关关着时连客户端都不构造（与 :func:`retrieval_quota` 同一条理由）：默认套件因此
    不需要 Redis，也不会因为一条「反正不用也先连上」的路径在无 Redis 的机器上卡住。
    """

    limiter_on = settings.RATE_LIMIT_ENABLED if enabled is None else enabled
    if not limiter_on:
        return None
    active = (
        counters
        if counters is not None
        else QuotaCounters(redis_client.get_redis_client(), namespace=namespace)
    )
    window = active.window()
    return await active.daily(level=LEVEL_TENANT, ident=tenant_id, window=window)


def _open_ledger(
    *,
    limits: QuotaLimits,
    tenant_id: str,
    key_id: str,
    counters: QuotaCounters | None,
    enabled: bool | None,
) -> QuotaLedger:
    """两个工厂的共用部分：定窗口、按开关决定要不要真的去数。

    ``enabled`` 缺省读服务端配置（``RATE_LIMIT_ENABLED``，默认关闭）。做成入参是为了让
    开 / 关两种行为都能被直接测到，不必 monkeypatch 全局配置。

    **关闭时一次都不碰 Redis**：连客户端都不构造。这不是省一行代码——一条「反正不用也
    先连上」的路径会让默认测试套件在没装 Redis 的机器上莫名其妙地卡住，而真正的问题
    （开关该由部署决定）反而被掩盖了。
    """

    limiter_on = settings.RATE_LIMIT_ENABLED if enabled is None else enabled
    if not limiter_on:
        return QuotaLedger(
            tenant_id=tenant_id,
            key_id=key_id,
            limits=limits,
            # 关掉时窗口没有意义；填一个空窗口比让字段变成 None 少一层 Optional 判断。
            window=QuotaWindow(qps="", daily=""),
            counters=None,
            enabled=False,
        )

    active = (
        counters
        if counters is not None
        else QuotaCounters(redis_client.get_redis_client(), namespace=limits.namespace)
    )
    return QuotaLedger(
        tenant_id=tenant_id,
        key_id=key_id,
        limits=limits,
        window=active.window(),
        counters=active,
        enabled=True,
    )


def retrieval_quota(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    tenant_id: str,
    key_id: str,
    counters: QuotaCounters | None = None,
    enabled: bool | None = None,
) -> QuotaLedger:
    """开检索那本额度账（行为与 #79 落地时逐项相同，只是账本类型通用化了）。"""

    return _open_ledger(
        limits=QuotaLimits.for_retrieval(policy=policy, plan=plan),
        tenant_id=tenant_id,
        key_id=key_id,
        counters=counters,
        enabled=enabled,
    )


def research_quota(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    tenant_id: str,
    key_id: str,
    counters: QuotaCounters | None = None,
    enabled: bool | None = None,
) -> QuotaLedger:
    """开 Research 那本额度账（票 #150）：同一份实现、另一组列、另一个键前缀。

    与检索那本**同构**——两级同记、进门只查、成功后记一次、两个错误码——但不共享桶。
    调用方是 ``research_gate.gated_research``：它在能力位那一步已经拒绝了「配额为 0」
    （答 ``20013``），所以这里拿到的上限都是正的。
    """

    return _open_ledger(
        limits=QuotaLimits.for_research(policy=policy, plan=plan),
        tenant_id=tenant_id,
        key_id=key_id,
        counters=counters,
        enabled=enabled,
    )
