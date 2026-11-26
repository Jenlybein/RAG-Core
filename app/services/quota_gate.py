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

## 三本账：检索、重排、大模型（第 13 步，ADR-0032）

**形状完全同构，键前缀不同**。按**资源**开户，不按端点开户：

| 这本账 | 记什么 | 上限来自 | 键前缀 |
| --- | --- | --- | --- |
| retrieve | 内核调用次数（一次 decompose 记 N） | ``retrieve_qps`` / ``retrieve_daily`` | ``quota`` |
| rerank | 重排调用次数 | ``rerank_qps`` / ``rerank_daily`` | ``quota:rerank`` |
| llm | 实际发生的大模型调用次数 | ``llm_qps`` / ``llm_daily`` | ``quota:llm`` |

按端点开户会造出一个虚构单位（「一次 decompose」），而按资源开户，成本表里的每一行都能
在链路上找到真实的对应动作。三本账共用同一份实现（:class:`QuotaLedger` +
:class:`QuotaLimits`），差别只有「读哪两列」与「写哪个前缀」——所以「两级同记、只有成功
才记、两个错误码」这三条纪律不会在某一天只落到一条链路上。

**Research 没有自己的账**：它的成本本来就是那几次大模型调用，另开一本等于把同一份成本
记两遍（票 #150 的 ``quota:research`` 已随第 13 步退役）。它的入口只认 ``research``
授权位与 ``llm`` 账。

新账的**四格全 0 表示「没配额度」= no-op**（不判、不记）：从旧版本升级上来的 Key 与套餐
在那几列上是迁移缺省 0，若把它当成「一次都不许用」，升级当天就会把所有人拦下来。

加工能力那两列的 **0 表示关闭**（与位为假同一个答复码 ``20013``），这个判定不在本模块：
它在门禁（``app.services.retrieval_gate`` / ``research_gate``）的「能力位」那一步，因为
它是配置事实、与计数开关无关。

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
from app.services.quota_limits import ensure_under_limit, has_room

#: 两级计数的两个名字。直接用字面量拼进键里（``quota:qps:tenant:...``），
#: 运维一眼能看出这个键属于哪一级，而不必回来读代码。
LEVEL_KEY: Final[str] = "key"
LEVEL_TENANT: Final[str] = "tenant"

#: 三本账的**名字**（不带窗口）。它是「按资源计量」这条决定的词表：一次分解检索会跑 N 次
#: 内核、可能各重排一次、再加一次大模型调用，三本账各自对应链路上一类真实动作。
#: 列名 = ``账本_窗口``（见 :class:`QuotaLimits` 的三个 ``for_*`` 工厂）。
METER_BOOKS: Final[tuple[str, ...]] = ("retrieve", "rerank", "llm")
#: 两级计数：Key 级卡住单个外部应用，租户级把同一租户下所有 Key 聚合起来。
METER_LEVELS: Final[tuple[str, ...]] = (LEVEL_KEY, LEVEL_TENANT)
#: 两格：每秒与每日。
METER_WINDOWS: Final[tuple[str, ...]] = ("qps", "daily")

#: 三本账的键前缀。检索那本**逐字不变**（``quota``）——它已经在生产上跑着，改前缀等于
#: 把当天所有人的用量清零；重排与大模型各一本，键前缀互不相干。
#
#: 票 #150 的 ``quota:research`` 那本账已退役（第 13 步）：Research 的成本本来就是「几次
#: 大模型调用」，另开一本账等于把同一份成本记两遍；现在它读 ``llm`` 那本。旧键靠日桶
#: TTL 自然过期，不需要清理动作。
KEY_NAMESPACE: Final[str] = "quota"
RERANK_NAMESPACE: Final[str] = "quota:rerank"
LLM_NAMESPACE: Final[str] = "quota:llm"

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

    ``namespace`` 缺省是检索那本账，所以既有调用点与用例里的键**逐字不变**；重排与大模型
    各传 :data:`RERANK_NAMESPACE` / :data:`LLM_NAMESPACE`。
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

    四个上限按「两级」摆开（Key 级卡单个应用、租户级聚合），三本账各有一份来源：

    * retrieve：密钥配置的 ``retrieve_qps`` / ``retrieve_daily`` 与套餐的同名列；
    * rerank：``rerank_*`` 四列；
    * llm：``llm_*`` 四列。

    ``namespace`` 决定写哪本账（见模块文档「三本账」）；``label`` 只进报错文案，
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
    def for_rerank(cls, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> QuotaLimits:
        """第 13 步三本账之一：重排调用次数，读 ``rerank_*`` 四列。"""

        return cls(
            key_qps=policy.rerank_qps,
            tenant_qps=plan.rerank_qps,
            key_daily=policy.rerank_daily,
            tenant_daily=plan.rerank_daily,
            namespace=RERANK_NAMESPACE,
            label="rerank ",
        )

    @classmethod
    def for_llm(cls, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> QuotaLimits:
        """第 13 步三本账之一：大模型调用次数，读 ``llm_*`` 四列。"""

        return cls(
            key_qps=policy.llm_qps,
            tenant_qps=plan.llm_qps,
            key_daily=policy.llm_daily,
            tenant_daily=plan.llm_daily,
            namespace=LLM_NAMESPACE,
            label="llm ",
        )


@dataclass(frozen=True)
class _QuotaCell:
    """一格额度：已经记了多少、上限多少、判失败时报什么码、话怎么说。

    ``code`` 与 ``what`` 只被拒绝路径用（探测路径只关心 ``has_room``），但两格放在一起
    才能保证「四格」这件事只写一遍——分开写时新增一本账就得改两个方法。
    """

    used: int
    limit: int
    code: ErrorCode
    what: str


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

        # 判据只有一份（``quota_limits.ensure_under_limit``），四格的读数也只有一份
        # （``_cells``）——顺序即判定顺序：Key 级秒级在前，它更窄、更可能是调用方自己
        # 要修的那件事（「我这个应用打得太快」），而租户级超限往往要另一个应用的负责人
        # 一起看。日配额**比的是当前额度**，不是某个记下来的快照：管理员把日配额改小，
        # 下一次调用就按新值判（AC「改小日配额后立刻生效，不需要重启」）。
        for cell in await self._cells():
            ensure_under_limit(
                used=cell.used,
                limit=cell.limit,
                code=cell.code,
                # 这一本是**按天 / 按秒的调用次数**，处置是「等重置或提额」，所以文案
                # 保持英文骨架（运维日志语境）。容量型上限（库数 / 文档数）另有专属码与
                # 中文人话，两者不再共用一句（#219，ADR-0035 决策七）。
                message=f"{cell.what} exceeded: {cell.used} used of {cell.limit} per window",
            )

    async def has_capacity(self) -> bool:
        """还能不能再跑一次？（只读，不记账、不抛错）

        :meth:`ensure_available` 的**问句式**版本，只给「这一步是可选的」那种调用点用：
        重排额度用尽时正确行为是**跳过重排并如实标原因**，不是拒绝整单（ADR-0032）。
        有了这个探针，那条纪律就只有一处实现，而不是在调用点里 ``try/except`` 一个
        ``AppError`` —— 后者会把「这把 Key 压根没买重排」和「今天排太多次了」混成一件
        事，而它们的正确处置恰好相反（前者该拒绝，后者该跳过）。

        与 :meth:`ensure_available` 读同一份 ``_cells``、走同一条判据
        （``quota_limits.has_room``），所以两者**不可能**给出相反的结论。
        """

        if not self.enabled or self.counters is None:
            return True

        return all(has_room(used=cell.used, limit=cell.limit) for cell in await self._cells())

    async def record_success(self) -> None:
        """业务成功之后才调它：两级各记一次（n=1）。

        「一次跨多库检索 / 一次跨多条子查询的 Research 只计一次」不是这里的判断——本对象
        根本不知道这次查了几个库、跑了几条子查询。正因为不知道，它才不可能按库 / 按子查询
        记账（票 #150 的 AC「一次 Research 只记一次」在结构上就成立）。
        """

        await self.record_n(1)

    async def record_n(self, n: int) -> None:
        """按**实际发生的次数**记账（第 13 步）：llm/rerank 一本账里一次调用可能跑 N 次。

        ``n <= 0`` 是 no-op：没发生就不记。记的位置仍是「业务成功之后」——被拒与
        失败的请求不扣额度这条纪律不因为支持多次而松动。调用方给几就记几，本对象
        不替它判断「这次到底跑了几步」。
        """

        if n <= 0 or not self.enabled or self.counters is None:
            return

        for _ in range(n):
            for level, ident in ((LEVEL_KEY, self.key_id), (LEVEL_TENANT, self.tenant_id)):
                await self.counters.add_qps(level=level, ident=ident, window=self.window)
                await self.counters.add_daily(level=level, ident=ident, window=self.window)

    async def _cells(self) -> tuple[_QuotaCell, ...]:
        """四格「已用 / 上限」：Key 秒级、租户秒级、Key 日额、租户日额（顺序即判定顺序）。

        两个公开方法都只读这一份：读数一次、判据一处，所以「先拦一道」与「先问一句」
        不可能因为某处漏改一格而给出相反的结论。
        """

        return (
            _QuotaCell(
                used=await self._read_qps(LEVEL_KEY, self.key_id),
                limit=self.limits.key_qps,
                code=ErrorCode.API_RATE_LIMIT,
                what=f"{self.limits.label}qps of key {self.key_id}",
            ),
            _QuotaCell(
                used=await self._read_qps(LEVEL_TENANT, self.tenant_id),
                limit=self.limits.tenant_qps,
                code=ErrorCode.API_RATE_LIMIT,
                what=f"{self.limits.label}qps of tenant {self.tenant_id}",
            ),
            _QuotaCell(
                used=await self._read_daily(LEVEL_KEY, self.key_id),
                limit=self.limits.key_daily,
                code=ErrorCode.QUOTA_EXCEEDED,
                what=f"{self.limits.label}daily quota of key {self.key_id}",
            ),
            _QuotaCell(
                used=await self._read_daily(LEVEL_TENANT, self.tenant_id),
                limit=self.limits.tenant_daily,
                code=ErrorCode.QUOTA_EXCEEDED,
                what=f"{self.limits.label}daily quota of tenant {self.tenant_id}",
            ),
        )

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
    去探测键是否存在。这里与 :func:`retrieval_quota` / :func:`rerank_quota` /
    :func:`llm_quota` 共用同一个窗口口径（UTC、按天分桶），所以控制台看到的数与门禁判的
    数一定是同一个桶。

    ``namespace`` 选读哪本账：控制面与 ``/auth/me`` 各报几栏（检索 / 重排 / 大模型），
    几栏的形状相同（``used`` + ``limit`` + ``source=daily_counter``），只是桶不同。

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


def _new_book_enabled(limits: QuotaLimits, enabled: bool | None) -> bool:
    """第 13 步新账的启用判据：四格全 0 = 没给这项能力配额度 → 账本 no-op。

    ``retrieve_qps`` 必须 ≥ 1（0 = 停服，创建时就拒），但 rerank / llm 这两本新账的
    0 是**正常缺省**（旧 Key 升级上来就是 0），不该让这些 Key 连加工端点都调不了。
    所以全 0 视为「这本账没启用」：不查、不记、不拒；配了任一格才真正开始限流计数。
    """

    if enabled is False:
        return False
    configured = any(
        (limits.key_qps, limits.tenant_qps, limits.key_daily, limits.tenant_daily)
    )
    if not configured:
        return False
    return settings.RATE_LIMIT_ENABLED if enabled is None else enabled


def rerank_quota(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    tenant_id: str,
    key_id: str,
    counters: QuotaCounters | None = None,
    enabled: bool | None = None,
) -> QuotaLedger:
    """第 13 步三本账之一：重排调用次数（读 ``rerank_*`` 四列，独立键前缀）。"""

    limits = QuotaLimits.for_rerank(policy=policy, plan=plan)
    return _open_ledger(
        limits=limits,
        tenant_id=tenant_id,
        key_id=key_id,
        counters=counters,
        enabled=_new_book_enabled(limits, enabled),
    )


def llm_quota(
    *,
    policy: KeyPolicySnapshot,
    plan: PlanSpec,
    tenant_id: str,
    key_id: str,
    counters: QuotaCounters | None = None,
    enabled: bool | None = None,
) -> QuotaLedger:
    """第 13 步三本账之一：大模型调用次数（读 ``llm_*`` 四列，独立键前缀）。"""

    limits = QuotaLimits.for_llm(policy=policy, plan=plan)
    return _open_ledger(
        limits=limits,
        tenant_id=tenant_id,
        key_id=key_id,
        counters=counters,
        enabled=_new_book_enabled(limits, enabled),
    )


