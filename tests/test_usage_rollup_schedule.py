"""用量汇总挂在清扫器上（第 17 步现场七）：节流、只做近两天、失败不拖累清扫。

## 为什么这件事值得单独钉

看板的长窗口（7d / 30d）读的是按天汇总表 ``usage_daily``，而它只有一个**人工脚本**会填。
没人跑脚本，「最近 7 天」就永远是空的——而用户看不出「缺了一个运维步骤」，只会以为统计坏了
（现场原话：「我在调试台用过几次，结果数据没显示出来」）。

修法是把汇总挂到本来就每 60 秒醒一次的守护线程上。这带来三条必须钉住的纪律：

1. **节流**：汇总幂等，但没必要每 60 秒扫一遍今天；同一次汇总 5 分钟内只做一次。
2. **只做近两天**：昨天的格过了今天不会再变，更早的天重算没有意义。
3. **失败隔离**：汇总失败只留 warning，不许取消一次清扫（否则「看板晚 5 分钟」会升级成
   「卡住的文档没人救」）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.tasks import index_sweeper

TODAY = date(2026, 11, 27)


class _Throttle:
    """``SET NX EX`` 的内存实现：第一次返回真，之后返回假（模拟「刚有人做过」）。"""

    def __init__(self, *, grant: bool = True, explode: bool = False) -> None:
        self.grant = grant
        self.explode = explode
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def set(self, key: str, value: str, **kwargs: Any) -> Any:
        self.calls.append((key, kwargs))
        if self.explode:
            raise RuntimeError("redis down")
        granted, self.grant = self.grant, False
        return True if granted else None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _factory() -> Any:
    """一个能 ``factory()()`` 的最小会话工厂（汇总那一步只用它开事务）。

    形状要跟生产一致：``async_session_factory`` **调用一次**拿到 sessionmaker、**再调一次**
    才拿到会话。少一层就会撞上一个很难读的 ``TypeError: object is not callable``——而这个
    假工厂的困惑程度正好等于「写账顺序」那次缺陷的隐蔽程度，所以照原样模仿。
    """

    class _Factory:
        def __call__(self) -> Any:
            return lambda: _Session()

    return _Factory()


@pytest.fixture
def rolled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把 ``rollup_day`` 换成记录器：这一层要断言的是「压了哪几天」，不是聚合逻辑。"""

    recorded: list[str] = []

    async def _rollup_day(_db: Any, day: date) -> list[dict[str, Any]]:
        recorded.append(str(day))
        return []

    monkeypatch.setattr(index_sweeper, "rollup_day", _rollup_day)
    monkeypatch.setattr(index_sweeper, "async_transaction", _noop_transaction())
    # 补射默认查不到洞（真实实现要打库），各用例按需覆盖。
    monkeypatch.setattr(index_sweeper, "days_missing_rollup", _no_gaps())
    return recorded


def _no_gaps() -> Any:
    async def _probe(_db: Any, *, days: int, today: date | None = None) -> list[date]:
        return []

    return _probe


def _noop_transaction() -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx(_db: Any):
        yield

    return _tx


def _patched_redis(monkeypatch: pytest.MonkeyPatch, throttle: _Throttle) -> None:
    class _Client:
        @staticmethod
        def get_redis_client() -> _Throttle:
            return throttle

    monkeypatch.setattr(index_sweeper, "redis_client", _Client())


@pytest.mark.asyncio
async def test_a_round_rolls_up_today_and_yesterday_only(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """汇总的范围是「昨天 + 今天」：更早的天不会再变，重算没有意义也不会发生。"""

    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == [str(TODAY - timedelta(days=1)), str(TODAY)]
    assert stats == {"rolled": 2}


@pytest.mark.asyncio
async def test_the_throttle_keeps_a_second_round_from_doing_it_again(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """5 分钟内第二次唤醒：抢不到节流锁，就一次聚合都不做。"""

    _patched_redis(monkeypatch, _Throttle(grant=False))

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == []
    assert stats == {"rolled": 0}


@pytest.mark.asyncio
async def test_a_broken_redis_skips_the_round_without_raising(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """Redis 抽风只跳过这一轮：汇总晚几分钟最坏，让清扫抛异常最坏（卡住的文档没人救）。"""

    _patched_redis(monkeypatch, _Throttle(explode=True))

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == []
    assert stats == {"rolled": 0}


@pytest.mark.asyncio
async def test_one_failing_day_does_not_stop_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """昨天那天的聚合炸了，今天照样做；返回值如实说「只压成了一天」。"""

    done: list[str] = []

    async def _rollup_day(_db: Any, day: date) -> list[dict[str, Any]]:
        if day == TODAY - timedelta(days=1):
            raise RuntimeError("bad aggregate")
        done.append(str(day))
        return []

    monkeypatch.setattr(index_sweeper, "rollup_day", _rollup_day)
    monkeypatch.setattr(index_sweeper, "async_transaction", _noop_transaction())
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert done == [str(TODAY)]
    assert stats == {"rolled": 1}


@pytest.mark.asyncio
async def test_a_missed_day_is_rolled_up_as_a_catch_up(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """「有明细、没汇总格」的日子会被顺手补上——这是整栈停过一天之后的唯一补救。

    只做「今天与昨天」的话，那一天永远不会长出来，看板的 7d / 30d 就永远少一块，而界面上
    只会说「最近 7 天是 2 次调用」这种半真话。
    """

    gap = date(2026, 11, 17)

    async def _probe(_db: Any, *, days: int, today: date | None = None) -> list[date]:
        return [gap]

    monkeypatch.setattr(index_sweeper, "days_missing_rollup", _probe)
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == [str(TODAY - timedelta(days=1)), str(TODAY), str(gap)]
    assert stats == {"rolled": 3}


@pytest.mark.asyncio
async def test_a_catch_up_never_duplicates_today_or_yesterday(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """补射名单里如果混进了今天/昨天（不该发生），也不许压两遍。

    重复压不会错（幂等），但会把「这一轮压了几天」这个读数弄成假的；而读数正是运维判断
    「补射到底干没干活」的唯一依据。
    """

    async def _probe(_db: Any, *, days: int, today: date | None = None) -> list[date]:
        return [TODAY, TODAY - timedelta(days=1), date(2026, 11, 10)]

    monkeypatch.setattr(index_sweeper, "days_missing_rollup", _probe)
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == [str(TODAY - timedelta(days=1)), str(TODAY), "2026-11-10"]
    assert stats == {"rolled": 3}


@pytest.mark.asyncio
async def test_a_broken_gap_scan_still_rolls_up_today_and_yesterday(
    monkeypatch: pytest.MonkeyPatch, rolled: list[str]
) -> None:
    """补射的「探洞」炸了，不该挡住今天与昨天——最坏情况只是洞晚 5 分钟再补。"""

    async def _probe(_db: Any, *, days: int, today: date | None = None) -> list[date]:
        raise RuntimeError("bad probe")

    monkeypatch.setattr(index_sweeper, "days_missing_rollup", _probe)
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.rollup_usage_once(session_factory=_factory(), today=TODAY)

    assert rolled == [str(TODAY - timedelta(days=1)), str(TODAY)]
    assert stats == {"rolled": 2}
