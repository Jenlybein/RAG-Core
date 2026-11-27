"""每日切块额度（第四本账）的**两道门**：入口预检与 Worker 硬拦。

## 为什么这个文件现在才出现

这两条分支此前**没有任何用例**：`ensure_chunk_daily_quota` 与
`charge_chunk_tokens` 只在真跑一次索引时才被走到，而额度用尽那一格需要把一天的额度烧完
——所以它从来没被执行过。于是里面藏着一个必然抛出的写法：

```python
raise AppError(code, msg, {...})   # data 是**仅关键字**参数 → TypeError
```

后果不是「额度用尽」，而是「额度用尽 → 500」。mypy 先抓到它（`Too many positional
arguments for "AppError"`），这里补上行为用例把它钉死。

## 为什么这些用例是离线的

两条门对外的可观察面只有两样：**抛不抛**（异常码 + 文案 + data）与**记了多少账**。
账写在 Redis 计数里，而 `QuotaCounters` 是可注入的协议——给一个记账的内存实现即可，
不需要 Redis、也不需要数据库。真正需要真库的是「记账那一行落到 `usage_events`」，
那属于 `tests/test_console_usage.py` 与真跑验证的事。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.services import chunk_ledger
from app.services.chunk_ledger import settings


@pytest.fixture(autouse=True)
def _quota_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``RATE_LIMIT_ENABLED`` 打开。

    默认套件里它是关的（那条取舍见 `quota_gate` 模块文档：关着时整段 no-op，默认套件因此
    不需要 Redis）。而这两条门**只在打开时存在**——所以这里的用例显式打开它，
    但计数仍然走注入的内存实现，不碰 Redis。
    """

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)


class _FakeCounters:
    """``QuotaCounters`` 的内存实现：只回答「今天用了多少」，并在记账时累加。"""

    def __init__(self, used: int = 0) -> None:
        self.used = used
        self.bumped: list[int] = []

    def window(self) -> Any:
        return _FakeWindow()

    async def daily(self, *, level: str, ident: str, window: Any) -> int:
        assert level == "tenant", "第四本账只有租户级：索引任务里没有密钥身份"
        return self.used

    async def bump(self, *, level: str, ident: str, window: Any, amount: int) -> int:
        assert level == "tenant"
        self.bumped.append(amount)
        self.used += amount
        return self.used


class _FakeWindow:
    """窗口替身：两条门都只把它原样传给计数实现，自己不读字段。"""


class _NoCounters:
    """被误用时炸掉：断言「这条路径不该读计数」。"""

    def window(self) -> Any:  # pragma: no cover - 不许被调用
        raise AssertionError("不该读窗口")

    async def daily(self, **_kwargs: Any) -> int:  # pragma: no cover - 不许被调用
        raise AssertionError("不该读日计数")


@pytest.mark.asyncio
async def test_the_entry_precheck_passes_when_the_quota_is_not_used_up() -> None:
    """没用完就放行，且**不预扣**（解析前不知道这一篇会花多少，预扣等于编数字）。"""

    counters = _FakeCounters(used=1_000)

    await chunk_ledger.ensure_chunk_daily_quota(
        tenant_id="tenant-a",
        limit=1_000_000,
        plan_name="pro",
        counters=counters,  # type: ignore[arg-type]
    )

    assert counters.bumped == [], "入口预检不许预扣"


@pytest.mark.asyncio
async def test_the_entry_precheck_raises_20037_with_a_readable_message() -> None:
    """用尽 → **20037**（不是 500），文案带「用掉多少 / 上限多少 / 哪档套餐」。

    这条用例的存在本身就说明了一件事：这条分支此前从未被执行过，而它里面曾经有一个
    会让 20037 变成 500 的写法（`data` 传成了位置参数）。
    """

    counters = _FakeCounters(used=1_000_000)

    with pytest.raises(AppError) as raised:
        await chunk_ledger.ensure_chunk_daily_quota(
            tenant_id="tenant-a",
            limit=1_000_000,
            plan_name="pro",
            counters=counters,  # type: ignore[arg-type]
        )

    error = raised.value
    assert error.code == ErrorCode.CHUNK_DAILY_QUOTA_EXCEEDED
    # 量级写法（「100.0 万」），不是裸数字：对着 1000000 数零没人愿意数。
    assert "100.0 万" in str(error)
    assert "pro" in str(error)
    assert error.data == {
        "reason": "chunk_daily_tokens",
        "used": 1_000_000,
        "limit": 1_000_000,
        "plan": "pro",
    }, "data 必须落成字典（前端按字段给下一步动作）"


@pytest.mark.asyncio
async def test_the_worker_hard_stop_refuses_before_charging() -> None:
    """Worker 硬拦：**先判后记**——超额那一次一分钱都不许记进账。

    与入口预检的分工：预检只答「今天是不是已经用尽」，硬拦才是真判据（它手里有这一篇
    的确切 token 数）。所以这里验的是「不许先记后判」。
    """

    counters = _FakeCounters(used=900)

    class _NoDb:
        async def execute(self, _statement: Any) -> Any:  # pragma: no cover - 不许被调用
            raise AssertionError("被拒的那一次不该写账")

    with pytest.raises(AppError) as raised:
        await chunk_ledger.charge_chunk_tokens(
            _NoDb(),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            tokens=200,
            limit=1_000,
            plan_name="pro",
            plan_code="pro",
            counters=counters,  # type: ignore[arg-type]
        )

    assert raised.value.code == ErrorCode.CHUNK_DAILY_QUOTA_EXCEEDED
    assert counters.bumped == [], "先判后记：被判拒的那一次没有任何记账动作"


@pytest.mark.asyncio
async def test_charging_zero_tokens_does_not_touch_the_ledger() -> None:
    """空正文（amount=0）不记账：账上不该出现一条 0。"""

    counters = _FakeCounters(used=0)

    class _NoDb:
        async def execute(self, _statement: Any) -> Any:  # pragma: no cover - 不许被调用
            raise AssertionError("amount=0 不该写账")

    charged = await chunk_ledger.charge_chunk_tokens(
        _NoDb(),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        tokens=0,
        limit=1_000,
        plan_name="pro",
        plan_code="pro",
        counters=counters,  # type: ignore[arg-type]
    )

    assert charged == 0
    assert counters.bumped == []
