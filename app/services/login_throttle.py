"""登录接口的专用限流（票 #80 / #72「登录接口要限流」）。

数据面的 QPS 限流（:mod:`app.services.quota_gate`）按「秒」计，是为检索的突发流量设计
的；登录是**低频、高价值**的动作，一秒五次的窗口等于没有——密码猜错再快也来得及。这里
因此是一本独立的帐：五分钟固定桶、账号 + 客户端 IP 作为身份、成功登录清除这一对的计数。

## 键为什么是「账号 + 客户端 IP」

- **只按 IP**：攻击者在一个地址上轮着试很多账号（credential stuffing 的典型形态，字典
  来自别的站点的泄露），每个账号都只试几次；只按 IP 固然能挡住「一个地址狂猜一个账号」，
  却挡不住这种广撒网，而且一个公司出口 IP 后面的所有正常用户会共享同一份额度。
- **只按账号**：攻击者换地址继续猜同一个账号（住宅代理、僵尸网络）；只按账号能挡住，
  但分布式的请求会绕开。
- **账号 + IP**：把「谁在猜哪个账号」当作一次攻击的单位。代价是**两种绕过都还剩一点**
  ——同一地址对每个账号各有一份额度（广撒网仍可能慢慢试），同一账号换地址也有新额度。
  这是刻意接受的取舍：控制台登录不是公开注册入口，而再收紧就要么按 IP 聚合（会误伤
  NAT 后的整间办公室）、要么按账号聚合（会让攻击者能锁死任意已知账号）。两者都不是
  这个规模的面板该做的决定。

客户端 IP 取**传输层对端地址**（``request.client.host``），不读 ``X-Forwarded-For``：
后者在没配置可信代理拓扑时是请求方可控的，拿它做限流键等于把「换一个头就换一份额度」
交给攻击者。部署在反向代理后面时，对端是代理地址，于是所有用户共享同一个 IP 分量——
键里还有账号，所以「一个账号的暴力猜测」仍然被单独计数，这正是本票要防的主线。

## 失败与成功怎么计数

**每次尝试进门先记一次**（在验口令之前），因此失败与「账号不存在」的尝试都会被数到；
不这样做的话，一个不存在的用户名就是免费的无限重试。**成功登录清除这一对的计数**：

- 攻击它的是「猜不中」的暴力破解——成功意味着攻击者已经知道口令，清除计数不会给他
  任何额外机会；不清除反而会让输错两三次的合法用户在下一次登录继续被惩罚。
- 它**不**解决 credential stuffing：那种攻击每个账号各有自己的桶，换账号仍然各有额度
  （见上面的取舍）。

被拒的那一次**不记账**：窗口不会因为被拒后的重试而延长，否则攻击者可以靠持续重试
把某个账号锁死一整天（拒绝服务）。代价与 :mod:`app.services.quota_gate` 相同：查与记
不是原子的，同一瞬间的并发请求可能一起看到「还剩一次」；这个偏差的方向是**放行**，
不是误拒。

## 开关

``RATE_LIMIT_ENABLED`` 默认关闭，与检索限流同一个开关、同一个精神：本地开发与默认
测试套件全程离线。关着时 :func:`login_throttle` 返回 ``None``，路由连客户端都不构造，
因此一次 Redis 命令都不会发。生产必须打开它。

## 计数命令是异步的（票 #133）

四条命令（``get`` / ``incr`` / ``expire`` / ``delete``）全部 ``await``，客户端来自
:func:`app.core.redis_client.get_redis_client`（``redis.asyncio``）。登录路由已经
``async def``，同步 Redis 会在验口令之前就把事件循环按住——而这里恰好是**未授权流量**
最集中的一处，被按住的不只是正常用户。

**换成异步不改任何判定**：键（账号 + 客户端 IP）、五分钟固定桶、UTC 口径、阈值
（:data:`LOGIN_MAX_ATTEMPTS`）、超限错误码（``20005``）、「被拒的那次不记」、
「成功才清除」全部原样保留。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Final, Protocol

from app.core import redis_client
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.services.quota_gate import _as_count

#: 一个窗口内最多允许的登录尝试次数（含失败与最终成功的那一次）。
#: 五个是「人有几次输错机会」与「猜测成本」之间的折中：五分钟窗口里猜五次，一天也
#: 只有 1440 次，远低于任何可用的在线暴力破解速率。
LOGIN_MAX_ATTEMPTS: Final[int] = 5
#: 固定窗口长度（秒）。登录不是高频动作，固定桶比滑动窗口简单且足够。
LOGIN_WINDOW_SECONDS: Final[int] = 5 * 60
#: 键的存活时间：比窗口略长，覆盖跨桶边界时在途的那次请求（与日配额 TTL 同一理由）。
LOGIN_TTL_SECONDS: Final[int] = LOGIN_WINDOW_SECONDS + 60

#: 独立的键前缀：运维一眼能看出这是登录限流，而不是数据面配额（``quota:*``）。
LOGIN_KEY_TEMPLATE: Final[str] = "login:attempts:{username}:{client_ip}:{window}"

#: 被限流时的**唯一**对外提示。不提账号、不提「存在 / 不存在」，也不提还剩多久。
MSG_LOGIN_RATE_LIMITED: Final[str] = "too many login attempts; try again later"


class LoginCounterClient(Protocol):
    """登录限流真正用到的四条 Redis 命令（异步形状，见模块文档「计数命令是异步的」）。

    比 :class:`~app.services.quota_gate.CounterClient` 多一条 ``delete``（成功登录要清
    计数）。测试传一个 dict 伪装的**异步**对象就够；这里不 import redis 包。
    """

    async def incr(self, key: str) -> Any: ...

    async def get(self, key: str) -> str | bytes | None: ...

    async def expire(self, key: str, ttl: int) -> Any: ...

    async def delete(self, key: str) -> Any: ...


def _utc_now() -> datetime:
    """窗口按 UTC 算：多 worker 共享 Redis，桶的边界不能取决于某台机器的本地时区。"""

    return datetime.now(timezone.utc)


def login_key(*, username: str, client_ip: str, window: int) -> str:
    """这一对「账号 + IP」在某个五分钟桶里的键。"""

    return LOGIN_KEY_TEMPLATE.format(username=username, client_ip=client_ip, window=window)


class LoginThrottle:
    """一本登录尝试账：``register_attempt`` 查+记，``reset`` 在成功后清。

    做成对象（而不是几个自由函数）的理由与 ``QuotaCounters`` 相同：键的拼法、窗口的
    算法、``incr`` 之后必须 ``expire`` 这三件事必须一起出现；漏了 ``expire`` 会让一对
    账号 + IP 被永久限住，而且没有任何报错。
    """

    def __init__(
        self,
        client: LoginCounterClient,
        *,
        clock: Callable[[], datetime] | None = None,
        limit: int = LOGIN_MAX_ATTEMPTS,
        window_seconds: int = LOGIN_WINDOW_SECONDS,
        ttl_seconds: int = LOGIN_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else _utc_now
        self._limit = limit
        self._window_seconds = window_seconds
        self._ttl_seconds = ttl_seconds

    def _key(self, *, username: str, client_ip: str) -> str:
        bucket = int(self._clock().timestamp()) // self._window_seconds
        return login_key(username=username, client_ip=client_ip, window=bucket)

    async def attempts(self, *, username: str, client_ip: str) -> int:
        """本窗口已经记了几次；没记过就是 0（坏值也当 0，与配额计数同一口径）。"""

        return _as_count(await self._client.get(self._key(username=username, client_ip=client_ip)))

    async def register_attempt(self, *, username: str, client_ip: str) -> None:
        """进门前调用：超限抛 ``20005``；否则把这次尝试记上（**失败与成功都先记**）。"""

        key = self._key(username=username, client_ip=client_ip)
        if _as_count(await self._client.get(key)) >= self._limit:
            # 被拒的这一次不写：持续重试不能把窗口往后推（见模块文档）。
            raise AppError(ErrorCode.API_RATE_LIMIT, MSG_LOGIN_RATE_LIMITED)
        await self._client.incr(key)
        await self._client.expire(key, self._ttl_seconds)

    async def reset(self, *, username: str, client_ip: str) -> None:
        """登录成功后清除这一对的计数：合法用户不该为输错几次付出下个窗口的代价。"""

        await self._client.delete(self._key(username=username, client_ip=client_ip))


def login_throttle(
    *,
    client: LoginCounterClient | None = None,
    enabled: bool | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LoginThrottle | None:
    """按开关造一本登录账；关着时返回 ``None``（调用方因此直接跳过，连客户端都不建）。

    ``enabled`` 缺省读 ``RATE_LIMIT_ENABLED``；做成入参是为了让开关两态都能被直接测到，
    不必 monkeypatch 全局配置。``client`` 缺省走 ``app.core.redis_client`` 这唯一构造点。
    """

    limiter_on = settings.RATE_LIMIT_ENABLED if enabled is None else enabled
    if not limiter_on:
        return None
    active = client if client is not None else redis_client.get_redis_client()
    return LoginThrottle(active, clock=clock)
