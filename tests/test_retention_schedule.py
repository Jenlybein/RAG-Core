"""保留期清理的调度侧（第 17 步现场八）：每天一次、两个动作互不拖累、失败只留 warning。

两个**人工脚本**转成自动：``purge_usage_events.py``（明细 90 天 / 汇总 395 天）与
``purge_document_versions.py``（每篇文档窗口外的归档稿）。它们的失败方式都是安静地慢慢变坏
（磁盘涨、检索变慢），而「记得敲命令」不是一种可靠性。

这里钉的是**调度纪律**，不是删除规则本身：

1. 24 小时节流：一天只做一次；
2. 两个动作互不拖累：清版本炸了，清用量照做；
3. 返回值分开记账：运维能从日志看出「这一晚到底清了多少」。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.retention import UsageRetentionOutcome, VersionRetentionOutcome
from app.tasks import index_sweeper


class _Throttle:
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

    async def __getattr__(self, name: str) -> Any:
        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


def _factory() -> Any:
    """``factory()()`` 两层才有会话——与生产 ``async_session_factory`` 的形状一致。"""

    class _Factory:
        def __call__(self) -> Any:
            return lambda: _Session()

    return _Factory()


def _patched_redis(monkeypatch: pytest.MonkeyPatch, throttle: _Throttle) -> None:
    class _Client:
        @staticmethod
        def get_redis_client() -> _Throttle:
            return throttle

    monkeypatch.setattr(index_sweeper, "redis_client", _Client())


def _noop_tx() -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx(_db: Any):
        yield

    return _tx


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把两个清理动作换成记录器：这一层只关心「做没做、谁失败、账怎么记」。"""

    state: dict[str, Any] = {"versions_calls": 0, "usage_calls": 0, "fail_versions": False}

    async def _purge_versions(_db: Any, **_kwargs: Any) -> VersionRetentionOutcome:
        state["versions_calls"] += 1
        if state["fail_versions"]:
            raise RuntimeError("es down")
        return VersionRetentionOutcome(documents=3, purged_versions=5, failed_documents=0)

    async def _purge_usage(_db: Any, **_kwargs: Any) -> UsageRetentionOutcome:
        state["usage_calls"] += 1
        return UsageRetentionOutcome(events_deleted=120, daily_deleted=7, skipped_days=[])

    monkeypatch.setattr(index_sweeper.retention, "purge_versions", _purge_versions)
    monkeypatch.setattr(index_sweeper.retention, "purge_usage", _purge_usage)
    monkeypatch.setattr(index_sweeper, "async_transaction", _noop_tx())
    return state


@pytest.mark.asyncio
async def test_one_round_purges_both_kinds_and_reports_them_separately(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]
) -> None:
    """一次清理：先版本、再用量，两边的数字分开回，别合成一个「清了多少」。"""

    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.retention_once(session_factory=_factory(), now=date(2026, 11, 27))

    assert outcomes["versions_calls"] == 1
    assert outcomes["usage_calls"] == 1
    assert stats == {"events_deleted": 120, "daily_deleted": 7, "versions_purged": 5}


@pytest.mark.asyncio
async def test_the_daily_throttle_makes_it_run_once_a_day(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]
) -> None:
    """第二次唤醒（同一天的另一轮）：抢不到日节流锁，两个动作都不做。"""

    _patched_redis(monkeypatch, _Throttle(grant=False))

    stats = await index_sweeper.retention_once(session_factory=_factory(), now=date(2026, 11, 27))

    assert outcomes["versions_calls"] == 0
    assert outcomes["usage_calls"] == 0
    assert stats == {"events_deleted": 0, "daily_deleted": 0, "versions_purged": 0}


@pytest.mark.asyncio
async def test_a_broken_redis_skips_the_night_without_raising(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]
) -> None:
    """Redis 抽风：这一晚跳过，但不许抛异常——清理是每天的事，明天再来。"""

    _patched_redis(monkeypatch, _Throttle(explode=True))

    stats = await index_sweeper.retention_once(session_factory=_factory(), now=date(2026, 11, 27))

    assert outcomes["versions_calls"] == 0
    assert stats["events_deleted"] == 0


@pytest.mark.asyncio
async def test_failing_version_purge_does_not_stop_the_usage_purge(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]
) -> None:
    """清归档稿时 ES 炸了：只丢那一半，用量明细照清。

    两件事互不相干，放在同一晚只是「都在夜里做」；让一件的失败拖累另一件，等于一个外部
    依赖抽风就把明细账无限增长这件事又交回给人。
    """

    outcomes["fail_versions"] = True
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.retention_once(session_factory=_factory(), now=date(2026, 11, 27))

    assert outcomes["usage_calls"] == 1
    assert stats["versions_purged"] == 0
    assert stats["events_deleted"] == 120


@pytest.mark.asyncio
async def test_failing_usage_purge_keeps_the_version_numbers(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]
) -> None:
    """反过来的那一半同理：清用量失败，已经清掉的稿数照报。"""

    async def _boom(_db: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(index_sweeper.retention, "purge_usage", _boom)
    _patched_redis(monkeypatch, _Throttle())

    stats = await index_sweeper.retention_once(session_factory=_factory(), now=date(2026, 11, 27))

    assert stats["versions_purged"] == 5
    assert stats["events_deleted"] == 0


# ─────────────────────────────── 版本清理：单篇失败不拖累整晚


@pytest.mark.asyncio
async def test_one_broken_document_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一篇文档清理失败只记 ``failed_documents``，其余篇照清。

    失败隔离放在**每篇一笔事务**这一层：一次清理要遍历整库，一篇坏掉的文档（外部向量库不认
    它的切片、或某稿已经被别处删了）不该让「其余几百篇」的清理一起等明天。
    """

    from types import SimpleNamespace

    from app.services import retention as retention_module

    documents = [
        SimpleNamespace(id="doc-ok-1"),
        SimpleNamespace(id="doc-bad"),
        SimpleNamespace(id="doc-ok-2"),
    ]
    cleaned: list[str] = []

    async def _list(_db: Any, *, retain: int, limit: int | None = None) -> Any:
        return documents

    async def _purge(_db: Any, *, document: Any, retain: int) -> Any:
        if str(document.id) == "doc-bad":
            raise RuntimeError("vector store refused")
        cleaned.append(str(document.id))
        return SimpleNamespace(expired_versions=(1, 2))

    class _Repo:
        list_with_expired_versions = staticmethod(_list)

    monkeypatch.setattr(retention_module, "document_repository", _Repo())
    monkeypatch.setattr(retention_module, "purge_expired_versions", _purge)
    # 事务边界也在 ``retention`` 模块自己的命名空间里（它 import 的是那个名字）。
    monkeypatch.setattr(retention_module, "async_transaction", _noop_tx())

    outcome = await retention_module.purge_versions(_Session())

    assert cleaned == ["doc-ok-1", "doc-ok-2"]
    assert outcome.documents == 3
    assert outcome.purged_versions == 4, "两篇各清 2 稿"
    assert outcome.failed_documents == 1
