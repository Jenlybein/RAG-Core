"""Gap 3（#72「登录接口要限流」）：登录专用限流的单元接缝。

接缝是 :class:`app.services.login_throttle.LoginThrottle` 本身：注入一个只认
``incr / get / expire / delete`` 的**异步**内存假实现与一个可注入的时钟，默认套件因此
**不碰 Redis**、也不需要真的等五分钟。

假实现是异步的（票 #133）：生产客户端换成 ``redis.asyncio``，四条命令都是协程。同步
假实现会让「忘了 await」的写法照样读到值，于是那条 bug 在套件里看不见。

这里钉住的四件事都是设计决定，不是实现细节：

* **键 = 账号 + 客户端 IP + 五分钟桶**。单按 IP 会被「一个地址轮着试很多账号」绕过；
  单按账号会被「一个账号换很多地址」绕过（见模块文档）。
* **窗口是固定桶，不是滑动窗口**：登录是低频动作，五分钟的固定桶足够挡暴力猜测，而
  滑动窗口要维护一个列表、多 worker 下还得原子地裁剪。
* **先查后记、被拒的那次不记**：被限流后的重试不延长窗口，否则攻击者可以靠持续重试
  把受害者锁死一整天。
* **成功清除这一对的计数**：合法用户输错几次后登录成功，不该在下一个窗口继续被惩罚；
  而暴力破解的定义是「猜不中」，所以这个重置拿不到任何额外尝试机会。它**不**解决
  credential stuffing（每个账号各自计数，换账号仍然各有额度），这个取舍写在模块文档里。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.services.login_throttle import (
    LOGIN_KEY_TEMPLATE,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_TTL_SECONDS,
    LOGIN_WINDOW_SECONDS,
    MSG_LOGIN_RATE_LIMITED,
    LoginThrottle,
    login_throttle,
)

MOMENT = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc)
#: 1794276000 = 2026-11-10T02:00:00Z 的 epoch 秒；桶号独立算出来，不回算被测代码。
EPOCH = 1794276000
BUCKET = EPOCH // LOGIN_WINDOW_SECONDS
IP = "203.0.113.7"

ALICE = "alice"
BOB = "bob"


def _run(coroutine: Coroutine[Any, Any, None]) -> None:
    """跑一条异步用例：套件没有 pytest-asyncio（那属于票 #126），沿用 ``asyncio.run``。"""

    asyncio.run(coroutine)


class _Store:
    """登录限流真正用到的四条命令：一个 dict 加一份调用账本。"""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []

    async def incr(self, key: str) -> int:
        self.calls.append(f"incr:{key}")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get:{key}")
        value = self.values.get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, ttl: int) -> None:
        self.calls.append(f"expire:{key}")
        self.ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self.calls.append(f"delete:{key}")
        self.values.pop(key, None)
        self.ttls.pop(key, None)


class _ExplodingStore:
    """任何命令都炸：用来证明开关关着时一次都没碰 Redis。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def incr(self, key: str) -> int:  # pragma: no cover - 访问即失败
        self.calls.append(f"incr:{key}")
        raise AssertionError(f"不该访问 Redis：incr({key})")

    async def get(self, key: str) -> str | None:  # pragma: no cover - 访问即失败
        self.calls.append(f"get:{key}")
        raise AssertionError(f"不该访问 Redis：get({key})")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"expire:{key}")
        raise AssertionError(f"不该访问 Redis：expire({key}, {ttl})")

    async def delete(self, key: str) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"delete:{key}")
        raise AssertionError(f"不该访问 Redis：delete({key})")


def _throttle(store: _Store, *, moment: datetime = MOMENT) -> LoginThrottle:
    return LoginThrottle(store, clock=lambda: moment)


def _expected_key(username: str = ALICE, client_ip: str = IP, bucket: int = BUCKET) -> str:
    """按**模板常量**拼出期望键：改模板时这条断言跟着一起变，不会被悄悄绕过。"""

    return LOGIN_KEY_TEMPLATE.format(username=username, client_ip=client_ip, window=bucket)


# ───────────────────────────────────────────── 键与窗口


def test_the_key_is_scoped_to_the_account_ip_pair_and_the_window() -> None:
    async def scenario() -> None:
        store = _Store()
        throttle = _throttle(store)

        await throttle.register_attempt(username=ALICE, client_ip=IP)

        key = "login:attempts:alice:203.0.113.7:5980920"
        assert list(store.values) == [key]
        assert store.ttls[key] == LOGIN_TTL_SECONDS
        assert LOGIN_TTL_SECONDS > LOGIN_WINDOW_SECONDS, "桶要活过自己的窗口，覆盖在途请求"

    _run(scenario())


def test_a_new_window_starts_with_a_fresh_allowance() -> None:
    async def scenario() -> None:
        store = _Store()
        early = _throttle(store)
        await early.register_attempt(username=ALICE, client_ip=IP)
        assert await early.attempts(username=ALICE, client_ip=IP) == 1

        later = _throttle(store, moment=MOMENT + timedelta(seconds=LOGIN_WINDOW_SECONDS))
        assert await later.attempts(username=ALICE, client_ip=IP) == 0
        assert list(store.values) == ["login:attempts:alice:203.0.113.7:5980920"]

    _run(scenario())


def test_two_accounts_or_two_addresses_do_not_share_a_bucket() -> None:
    """「账号 + IP」是键的两半：换任意一半都是另一份额度。"""

    async def scenario() -> None:
        store = _Store()
        throttle = _throttle(store)

        for _ in range(LOGIN_MAX_ATTEMPTS):
            await throttle.register_attempt(username=ALICE, client_ip=IP)

        assert await throttle.attempts(username=ALICE, client_ip=IP) == LOGIN_MAX_ATTEMPTS
        assert await throttle.attempts(username=ALICE, client_ip="198.51.100.9") == 0
        assert await throttle.attempts(username=BOB, client_ip=IP) == 0

    _run(scenario())


# ───────────────────────────────────────────── 阈值与错误码


def test_the_attempt_after_the_limit_is_rejected_with_20005() -> None:
    """额度 N 表示「本窗口最多 N 次尝试」：第 N 次记上，第 N+1 次才拒绝。"""

    async def scenario() -> None:
        store = _Store()
        throttle = _throttle(store)

        for _ in range(LOGIN_MAX_ATTEMPTS):
            await throttle.register_attempt(username=ALICE, client_ip=IP)
        assert await throttle.attempts(username=ALICE, client_ip=IP) == LOGIN_MAX_ATTEMPTS

        with pytest.raises(AppError) as raised:
            await throttle.register_attempt(username=ALICE, client_ip=IP)

        assert raised.value.code is ErrorCode.API_RATE_LIMIT
        assert raised.value.msg == MSG_LOGIN_RATE_LIMITED
        assert await throttle.attempts(username=ALICE, client_ip=IP) == LOGIN_MAX_ATTEMPTS, (
            "被拒的那次不记：否则持续重试会把受害者的窗口无限延长"
        )

    _run(scenario())


def test_the_rate_limit_message_names_neither_the_account_nor_the_outcome() -> None:
    """同一句话覆盖「账号存在」与「账号不存在」，不构成用户名枚举。"""

    async def scenario() -> None:
        store = _Store()
        throttle = _throttle(store)
        for username in (ALICE, "ghost-user"):
            for _ in range(LOGIN_MAX_ATTEMPTS):
                await throttle.register_attempt(username=username, client_ip=IP)

        messages = []
        for username in (ALICE, "ghost-user"):
            with pytest.raises(AppError) as raised:
                await throttle.register_attempt(username=username, client_ip=IP)
            messages.append(raised.value.msg)

        assert messages[0] == messages[1] == MSG_LOGIN_RATE_LIMITED
        lowered = MSG_LOGIN_RATE_LIMITED.lower()
        assert ALICE not in MSG_LOGIN_RATE_LIMITED
        assert "ghost-user" not in MSG_LOGIN_RATE_LIMITED
        assert "not found" not in lowered and "unknown" not in lowered
        assert "invalid username" not in lowered

    _run(scenario())


# ───────────────────────────────────────────── 成功清除


def test_a_successful_login_clears_the_pairs_counter() -> None:
    """输错几次后登进来，不该在同一个窗口里继续被惩罚。"""

    async def scenario() -> None:
        store = _Store()
        throttle = _throttle(store)
        for _ in range(LOGIN_MAX_ATTEMPTS - 2):
            await throttle.register_attempt(username=ALICE, client_ip=IP)
        assert await throttle.attempts(username=ALICE, client_ip=IP) == LOGIN_MAX_ATTEMPTS - 2

        await throttle.reset(username=ALICE, client_ip=IP)

        assert await throttle.attempts(username=ALICE, client_ip=IP) == 0
        assert store.values == {}
        assert f"delete:{_expected_key()}" in store.calls

    _run(scenario())


# ───────────────────────────────────────────── 开关


def test_the_factory_is_a_no_op_when_the_switch_is_off() -> None:
    """``RATE_LIMIT_ENABLED`` 关着时返回 ``None``：路由层因此连客户端都不构造。"""

    exploding = _ExplodingStore()

    assert login_throttle(client=exploding, enabled=False) is None
    assert exploding.calls == []
