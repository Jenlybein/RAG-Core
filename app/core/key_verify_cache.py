"""Key 校验缓存的接缝：命中 = 「这个 token 刚被 scrypt 验过，它属于哪一行」。

为什么要有它：库里只存 ``scrypt`` 哈希（:mod:`app.core.passwords` 同一套规则），一次校验
50~100ms、16 MiB 内存；而数据面的**每一次调用**都要过鉴权。缓存让「同一把 Key 在 60 秒内
反复调用」这件事不必每次都重算，仅此而已。

**它不负责撤销。** 缓存里只有 ``sha256(token) → key_id``；行、``status``、租户、密钥配置
仍然**每次读库**（见 :func:`app.core.auth._identity_from_key`）。所以删 Key、停用 Key、
改配置一律立即生效——缓存最多让一个已经不在库里的 token 白省一次 scrypt。这也是
「删 Key 时要不要清缓存」这个问题的答案：不需要，也不该假装需要（按 ``key_id`` 反向清
是做不到的，缓存键里没有 ``key_id``）。

**键是原文的 ``sha256``，这一点要说实话**：它等于把一个「离线验证这个猜测对不对」的
oracle 搬进了 Redis。可以接受的理由只有一条——密钥本体是 ``secrets.token_hex(16)`` 的
128 位随机值，反推与穷举都不成立，而「能读 Redis 的人」本来就能读库与库里的其它数据。
由此得到的纪律也一样硬：这个摘要不许再出现在第三个地方（日志、浏览器、队列）。

TTL 只决定「多久重算一次密码学校验」，不决定任何权限。
"""

from __future__ import annotations

import hashlib
from typing import Any, Final, Protocol

from app.core.config import settings

#: 缓存键前缀。「同一把 Key 在这 60 秒里验过」——清理时按它就能一次扫干净。
KEY_PREFIX: Final[str] = "api_key_verified:"


def cache_key(token: str) -> str:
    """Key 原文 → 缓存键。用 sha256 而不是原文：Redis 里不出现可用凭证。"""

    return f"{KEY_PREFIX}{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


class ExpiringKeyValueClient(Protocol):
    """``redis.asyncio`` 里这个缓存真正用到的两条命令。

    刻意不写成 ``Redis``：本模块因此不 import redis 包，测试传一个字典伪装的对象就够，
    也不会有人顺手在这里加一条只有真 Redis 才认的命令。
    """

    async def get(self, key: str) -> str | bytes | None: ...

    async def setex(self, key: str, ttl: int, value: str) -> Any: ...


class KeyVerifyCache(Protocol):
    """判定内核与 Redis 之间的契约：两个方法，不接受第三个。"""

    async def key_id_of(self, token: str) -> str | None: ...

    async def remember(self, token: str, key_id: str) -> None: ...


class RedisKeyVerifyCache:
    """生产实现：``sha256(token) → key_id``，TTL 到期自然消失（不靠删）。"""

    def __init__(
        self,
        client: ExpiringKeyValueClient,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._ttl = (
            settings.KEY_VERIFY_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        )

    async def key_id_of(self, token: str) -> str | None:
        raw = await self._client.get(cache_key(token))
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    async def remember(self, token: str, key_id: str) -> None:
        await self._client.setex(cache_key(token), self._ttl, key_id)


def build_redis_key_verify_cache() -> KeyVerifyCache:
    """生产用的实现：Redis（多 worker 才一致，进程内字典不行）。

    真正的连接池在 :func:`app.core.redis_client.get_redis_client` 里，而且客户端是**惰性**的：
    构造这个对象不发任何命令，所以「离线套件不碰 Redis」这条仍然成立。
    """

    from app.core.redis_client import get_redis_client

    return RedisKeyVerifyCache(get_redis_client())


#: 进程级注入点：默认 ``None`` 表示用生产实现（Redis）。测试注入内存假实现，
#: 于是「没有 Redis 也能跑鉴别内核的全部用例」不靠 monkeypatch 全局，也不靠"本机恰好开着"。
_default_cache: KeyVerifyCache | None = None


def set_default_key_verify_cache(cache: KeyVerifyCache | None) -> None:
    """注入 / 复位进程级默认缓存（用例用它换成内存假实现，退出时复位成 ``None``）。"""

    global _default_cache
    _default_cache = cache


def get_default_key_verify_cache() -> KeyVerifyCache:
    """判定内核缺省取的缓存：优先注入点，否则 Redis。

    生产实现**不缓存这个对象**（与 :func:`app.services.plan_service.get_default_plan_cache`
    同一个理由）：Redis 客户端是按事件循环注册的，把它钉在模块级会留下一个绑在
    「上一个已关闭的循环」上的对象。
    """

    if _default_cache is not None:
        return _default_cache
    return build_redis_key_verify_cache()
