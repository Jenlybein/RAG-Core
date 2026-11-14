"""#155 · 回滚：``archived → active``，与激活**共用同一段实现**。

spec 用户故事 33（「把某个库切回上一个版本，以便新切法出问题时能退」）在这里落地。
ADR-0025 把「可回滚」写成这套方案的收益之一，而回滚要成立，光有一条状态迁移不够：

* 目标版本的数据必须**还在**（物理索引在、切片还在）——否则切过去就是「库说查 v1、
  数据已经没了」，那是一次不报错的空结果；
* 动作必须与激活**一模一样**（别名 → 一笔事务 → 失败回切），否则回滚会以另一种方式
  留下中间态。

接缝与 #142 一致，不新开缝：**构建服务本身** + 注入的关键词 Provider / 向量存储；
数据库是异步替身。整套不连 PostgreSQL、不连 ES、不发网络。

五条验收对应用例：

| 验收 | 用例 |
| --- | --- |
| ``archived → active`` 合法；``building / failed`` 仍被拒 | ``test_rollback_requires_an_archived_version``（真值表见 ``test_index_build_state.py``） |
| 回滚后检索命中旧版本、别名指向旧物理索引 | ``test_retrieval_pins_back_to_the_old_version_after_a_rollback`` |
| 物理索引不在 / 切片为 0 → 拒绝，库列与别名都不动 | ``test_rollback_is_refused_when_the_keyword_physical_index_is_gone``、``test_rollback_is_refused_when_the_version_has_no_chunks`` |
| 回滚与激活共用同一段实现 | ``test_activation_and_rollback_take_the_same_state_paths`` |
| 并发时只有一方成功 | ``test_a_commit_conflict_names_the_same_error_on_both_doors`` |
| 输掉的一方回切别名时**重读**「此刻谁生效」 | ``test_a_losing_switch_restores_the_alias_to_the_version_that_is_live_now``（#156） |
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import Chunk
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
    resolve_index_versions,
)
from app.models.knowledge_base import KnowledgeBase
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.repositories import chunk_repository
from app.services.index_build_service import (
    ROLLBACK_DATA_MISSING_MESSAGE,
    SWITCH_CONFLICT_MESSAGE,
    IndexBuildInvalidTransition,
    IndexBuildRollbackRefused,
    IndexBuildService,
    IndexBuildSwitchConflict,
)
from tests.fake_db import AsyncRecordingSession, AsyncTransactionalSession

TENANT_ID = "tenant_rollback"
KB_ID = "kb-1"
#: 上一版（回滚目标）：此刻是 ``archived``。
OLD = "v1"
#: 当前生效的那一版：回滚成功之后它转 ``archived``。
LIVE = "v2"
#: 并发里**赢家**切到的那一版：输家手里的 ``previous``（``v2``）在它提交之后已经过期。
WINNER = "v3"


# ──────────────────────────────────────────────────────────────── 替身


class _RecordingKeywordProvider(NoopKeywordSearchProvider):
    """关键词 Provider 替身：回答「这一版的物理索引还在不在」，并记录别名切换。"""

    def __init__(self, *, indexes: set[str], available: bool = True) -> None:
        self.indexes = set(indexes)
        self._available = available
        self.activate_calls: list[str] = []
        self.exists_calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def physical_index_exists(self, version: str) -> bool:
        self.exists_calls.append(version)
        return version in self.indexes

    async def activate_version(self, version: str) -> None:
        self.activate_calls.append(version)


class _FailingCommitSession(AsyncTransactionalSession):
    """提交必炸的会话：验「别名先切、提交失败就切回去」这条收尾路径。"""

    async def commit(self) -> None:
        raise RuntimeError("commit failed")


class _ConflictingCommitSession(AsyncTransactionalSession):
    """提交时撞上**部分唯一索引**的会话：并发切换里「后手」的那一笔就长这样。"""

    async def commit(self) -> None:
        raise IntegrityError(
            "UPDATE index_build_versions SET status='active' ...",
            {},
            Exception("duplicate key value violates uq_index_build_versions_active"),
        )


class _RacedCommitSession(AsyncTransactionalSession):
    """提交时**赢家已经落地**：库列指向另一版，输家读到的 ``previous`` 已经过期。

    构造方式：``commit`` 先改库列再抛并发冲突（与真实并发里「赢家先提交、输家在部分唯一
    索引上撞」同一个顺序）；``rollback`` 只撤销**这一笔**的写入，赢家那一笔是别人提交的，
    撤不掉——所以回滚之后库列是赢家的版本名。
    """

    def __init__(self, *, kb: Any, winner: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._kb = kb
        self._winner = winner

    async def commit(self) -> None:
        self._kb.active_index_version = self._winner
        raise IntegrityError(
            "UPDATE index_build_versions SET status='active' ...",
            {},
            Exception("duplicate key value violates uq_index_build_versions_active"),
        )

    async def rollback(self) -> None:
        await super().rollback()
        self._kb.active_index_version = self._winner


# ──────────────────────────────────────────────────────────────── 夹具


def _kb(active: str | None = LIVE) -> SimpleNamespace:
    return SimpleNamespace(id=KB_ID, tenant_id=TENANT_ID, active_index_version=active)


def _row(
    version: str,
    status: str,
    *,
    build_id: str | None = None,
    archived_at: datetime | None = None,
) -> IndexBuildVersion:
    return IndexBuildVersion(
        id=build_id or f"build-{version}",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=version,
        status=status,
        created_at=datetime(2026, 11, 1, 9, 0, 0),
        archived_at=archived_at,
    )


def _chunks(count: int, *, version: str = OLD) -> list[Chunk]:
    """该版本的切片行（回滚守卫要数它们：0 片 = 数据已经没了）。"""

    return [
        Chunk(
            id=f"{version}-c{index}",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            document_id="doc-1",
            content="正文",
            embedding=[0.0] * 1536,
            meta={},
            index_version=version,
        )
        for index in range(count)
    ]


def _session(
    *,
    versions: list[IndexBuildVersion],
    kb: Any,
    chunk_rows: list[Any] | None = None,
    session_type: type[AsyncRecordingSession] = AsyncRecordingSession,
) -> AsyncRecordingSession:
    return session_type(
        rows={
            KnowledgeBase: [kb],
            IndexBuildVersion: list(versions),
            Chunk: list(chunk_rows or []),
        }
    )


def _service(
    *, indexes: set[str] | None = None, available: bool = True
) -> tuple[IndexBuildService, _RecordingKeywordProvider]:
    keyword = _RecordingKeywordProvider(
        indexes={OLD, LIVE} if indexes is None else indexes, available=available
    )
    service = IndexBuildService(
        indexing=SimpleNamespace(),
        vector_store=SimpleNamespace(),
        keyword_search=keyword,
    )
    return service, keyword


def _state() -> tuple[IndexBuildVersion, IndexBuildVersion, SimpleNamespace]:
    """当前线上是 v2、上一版 v1 已归档——回滚的起点。"""

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    old = _row(OLD, INDEX_BUILD_ARCHIVED, build_id="build-v1")
    return live, old, _kb(LIVE)


# ───────────────────────────────────── 同一段实现：回滚 = 从另一版出发的激活


def test_a_rollback_moves_the_alias_and_archives_the_live_version() -> None:
    """回滚：别名切到 old 的物理索引、v2 转 archived、库列更新，一次提交。"""

    live, old, kb = _state()
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(2))
    service, keyword = _service()

    row = asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert row.status == INDEX_BUILD_ACTIVE
    assert row.activated_at is not None
    assert live.status == INDEX_BUILD_ARCHIVED
    assert live.archived_at is not None
    assert kb.active_index_version == OLD
    assert session.commits == 1, "一次提交 = 三处同成同败"
    assert keyword.activate_calls == [OLD], "关键词别名切到 old 的**物理索引**"


@pytest.mark.parametrize(
    "door, origin_status",
    [("activate", INDEX_BUILD_READY), ("rollback", INDEX_BUILD_ARCHIVED)],
)
def test_activation_and_rollback_take_the_same_state_paths(door: str, origin_status: str) -> None:
    """两条入口的差别只有「从哪个版本出发」：落库的状态流转一模一样。

    这条用例是「共用同一段实现」的**行为证据**（不是检查调用图）：同一个库、同一个
    目标版本，只有起点不同，断言的那一组事实必须逐条相同。抄一份实现时，两边总会在
    「归档旧版本 / 更新库列 / 提交几次」中的某一项上漂移。
    """

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    target = _row(OLD, origin_status, build_id="build-v1")
    kb = _kb(LIVE)
    session = _session(versions=[live, target], kb=kb, chunk_rows=_chunks(2))
    service, keyword = _service()

    row = asyncio.run(getattr(service, door)(session, "build-v1", tenant_id=TENANT_ID))

    assert (row.status, live.status) == (INDEX_BUILD_ACTIVE, INDEX_BUILD_ARCHIVED)
    assert row.activated_at is not None and live.archived_at is not None
    assert kb.active_index_version == OLD
    assert session.commits == 1
    assert keyword.activate_calls == [OLD]


def test_a_rolled_back_version_carries_no_stale_archive_stamp() -> None:
    """重新上线的版本不该继续显示「归档于……」——那会让「最近归档了哪几版」查错。"""

    live, old, kb = _state()
    old.archived_at = datetime(2026, 11, 2, 9, 0, 0)
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(1))
    service, _ = _service()

    row = asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert row.archived_at is None
    assert row.activated_at is not None


# ──────────────────────────────────────────────── 守卫一：状态必须可上线


@pytest.mark.parametrize("status", [INDEX_BUILD_BUILDING, INDEX_BUILD_FAILED])
def test_rollback_requires_an_archived_version(status: str) -> None:
    """``building`` / ``failed`` 不许从回滚这条路直接上线，且拒绝要点名两端。"""

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    target = _row(OLD, status, build_id="build-v1")
    kb = _kb(LIVE)
    session = _session(versions=[live, target], kb=kb, chunk_rows=_chunks(2))
    service, keyword = _service()

    with pytest.raises(IndexBuildInvalidTransition) as error:
        asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert (error.value.current, error.value.target) == (status, INDEX_BUILD_ACTIVE)
    assert kb.active_index_version == LIVE, "被拒的回滚不动库列"
    assert keyword.activate_calls == [], "被拒的回滚不动别名"
    assert session.commits == 0


def test_activation_refuses_an_archived_version_so_it_cannot_skip_the_guard() -> None:
    """``activate`` 不兼任回滚：两条门各自认一个起点，回滚的守卫没有旁路可绕。"""

    live, old, kb = _state()
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(2))
    service, keyword = _service()

    with pytest.raises(IndexBuildInvalidTransition) as error:
        asyncio.run(service.activate(session, "build-v1", tenant_id=TENANT_ID))

    assert (error.value.current, error.value.target) == (INDEX_BUILD_ARCHIVED, INDEX_BUILD_ACTIVE)
    assert kb.active_index_version == LIVE
    assert keyword.activate_calls == []


def test_rollback_on_an_unknown_build_is_a_not_found() -> None:
    """跨租户 / 不存在的构建 id：一个 20016，不泄露存在性（ADR-0006）。"""

    _, _, kb = _state()
    session = _session(versions=[], kb=kb, chunk_rows=_chunks(1))
    service, keyword = _service()

    with pytest.raises(AppError) as error:
        asyncio.run(service.rollback(session, "build-missing", tenant_id=TENANT_ID))

    assert error.value.code is ErrorCode.NOT_FOUND
    assert keyword.activate_calls == []
    assert session.commits == 0


def test_rolling_back_the_version_that_is_already_live_is_refused() -> None:
    """``active → active`` 不在真值表里：重复回滚不必再发一次别名写（也挡住「切到自己」）。"""

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(versions=[live], kb=kb, chunk_rows=_chunks(2, version=LIVE))
    service, keyword = _service()

    with pytest.raises(IndexBuildInvalidTransition) as error:
        asyncio.run(service.rollback(session, "build-v2", tenant_id=TENANT_ID))

    assert (error.value.current, error.value.target) == (INDEX_BUILD_ACTIVE, INDEX_BUILD_ACTIVE)
    assert keyword.activate_calls == []
    assert session.commits == 0


# ──────────────────────────────────────────── 守卫二：物理数据必须还在


def test_rollback_is_refused_when_the_keyword_physical_index_is_gone() -> None:
    """物理索引被删了：拒绝回滚，库列与别名都不动（切过去就是静默空结果）。"""

    live, old, kb = _state()
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(2))
    service, keyword = _service(indexes={LIVE})

    with pytest.raises(IndexBuildRollbackRefused) as error:
        asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert error.value.msg == ROLLBACK_DATA_MISSING_MESSAGE
    assert (error.value.version, error.value.keyword_index, error.value.chunks) == (OLD, False, 2)
    assert kb.active_index_version == LIVE
    assert keyword.activate_calls == [], "守卫在切别名之前就把这一笔记否决了"
    assert session.commits == 0
    assert live.status == INDEX_BUILD_ACTIVE, "线上那一版不许被一次被拒的回滚改掉"


def test_rollback_is_refused_when_the_version_has_no_chunks() -> None:
    """切片为 0：库会说要查 v1，而 v1 一个字都没有——同样是拒绝。"""

    live, old, kb = _state()
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(0))
    service, keyword = _service()

    with pytest.raises(IndexBuildRollbackRefused) as error:
        asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert (error.value.version, error.value.keyword_index, error.value.chunks) == (OLD, True, 0)
    assert kb.active_index_version == LIVE
    assert keyword.activate_calls == []
    assert session.commits == 0


def test_a_missing_keyword_capability_only_checks_the_chunks() -> None:
    """没部署 ES 的部署里，物理索引这一维不参与（与 #142 的四项计数同一口径）。"""

    live, old, kb = _state()
    session = _session(versions=[live, old], kb=kb, chunk_rows=_chunks(1))
    service, keyword = _service(available=False)

    row = asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert row.status == INDEX_BUILD_ACTIVE
    assert keyword.exists_calls == [], "能力不存在就不去问它索引在不在"
    assert keyword.activate_calls == [OLD]
    assert kb.active_index_version == OLD


# ───────────────────────────────── 失败要干净 / 并发只有一方成功（同一句错误）


def test_a_failed_commit_switches_the_alias_back_when_rolling_back() -> None:
    """提交失败：别名切回**回滚前**那一版（否则关键词一直查旧索引、库列说新版本）。"""

    live, old, kb = _state()
    session = _session(
        versions=[live, old],
        kb=kb,
        chunk_rows=_chunks(2),
        session_type=_FailingCommitSession,
    )
    service, keyword = _service()

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))

    assert keyword.activate_calls == [OLD, LIVE], "切到 old 失败后切回 LIVE"
    # 替身不撤销内存里的属性赋值，所以这里断言的是「没有一笔提交落地」——库列因此不可能
    # 被改动（与 #142 的 `test_a_failed_commit_switches_the_alias_back` 同一口径）。
    assert session.commits == 0, "提交失败的这一笔什么都没落下"


@pytest.mark.parametrize("door", ["activate", "rollback"])
def test_a_commit_conflict_names_the_same_error_on_both_doors(door: str) -> None:
    """并发切换相撞：两条入口都把 ``IntegrityError`` 翻成**同一个**可读拒绝。

    真正裁决并发的是两条部分唯一索引（同一库最多一个 active / 一个 building）；这里
    要验的是「撞上之后调用方看到的不是一次 500」——而且两条入口看到的是同一句话。
    """

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    target = _row(
        OLD, INDEX_BUILD_ARCHIVED if door == "rollback" else INDEX_BUILD_READY, build_id="build-v1"
    )
    kb = _kb(LIVE)
    session = _session(
        versions=[live, target],
        kb=kb,
        chunk_rows=_chunks(2),
        session_type=_ConflictingCommitSession,
    )
    service, keyword = _service()

    with pytest.raises(IndexBuildSwitchConflict) as error:
        asyncio.run(getattr(service, door)(session, "build-v1", tenant_id=TENANT_ID))

    assert error.value.code is ErrorCode.SYSTEM_BUSY
    assert error.value.msg == SWITCH_CONFLICT_MESSAGE
    assert session.commits == 0, "输掉的那一方一笔都没落下（库列因此没被改）"
    assert keyword.activate_calls == [OLD, LIVE], "输掉的那一方把别名切回去"


@pytest.mark.parametrize("door", ["activate", "rollback"])
def test_a_losing_switch_restores_the_alias_to_the_version_that_is_live_now(door: str) -> None:
    """并发输掉时，别名要切回**此刻真正生效**的那一版，不是它开始时读到的那一版。

    现场：A 与 B 同时切同一个库；A 先提交（库列 → ``v3``），B 在提交处撞上部分唯一索引。
    如果 B 把别名切回自己开始时的 ``previous``（``v2``），别名就落在**谁也不认识**的那一版
    上：向量那一路按库列查 ``v3``、关键词那一路查 ``v2`` 的物理索引——关键词一路静默全空，
    而库行与切片一条都没错，排障会从「检索是不是坏了」查起。

    所以收尾路径必须**重读库列**（``active_index_version`` 是检索范围唯一的来源），
    读到什么就切回什么；读不到才退到 ``previous`` 并记一条 warning。
    """

    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v2")
    target = _row(
        OLD,
        INDEX_BUILD_ARCHIVED if door == "rollback" else INDEX_BUILD_READY,
        build_id="build-v1",
    )
    kb = _kb(LIVE)
    session = _RacedCommitSession(
        kb=kb,
        winner=WINNER,
        rows={
            KnowledgeBase: [kb],
            IndexBuildVersion: [live, target],
            Chunk: _chunks(2),
        },
    )
    service, keyword = _service()

    with pytest.raises(IndexBuildSwitchConflict):
        asyncio.run(getattr(service, door)(session, "build-v1", tenant_id=TENANT_ID))

    assert keyword.activate_calls == [OLD, WINNER], (
        "回切的是此刻生效的那一版（v3），不是切换前读到的 previous（v2）"
    )
    assert kb.active_index_version == WINNER, "库列由赢家写的，输家一个字节都不改"
    assert session.commits == 0, "输掉的那一方一笔都没落下"


# ────────────────────────────────────────── 验收二：检索命中旧版本的数据


def test_retrieval_pins_back_to_the_old_version_after_a_rollback() -> None:
    """回滚后检索范围指回 v1，而 v1 的切片仍在——响应契约一个字没变。"""

    live, old, kb = _state()
    session = _session(
        versions=[live, old], kb=kb, chunk_rows=_chunks(2) + _chunks(2, version=LIVE)
    )
    service, keyword = _service()

    before = dict(resolve_index_versions([kb]))
    asyncio.run(service.rollback(session, "build-v1", tenant_id=TENANT_ID))
    after = dict(resolve_index_versions([kb]))

    assert before == {KB_ID: LIVE}
    assert after == {KB_ID: OLD}, "检索 pin 回上一版（版本永远不是请求参数）"
    assert keyword.activate_calls == [OLD], "关键词一路查的是 v1 的物理索引"

    remaining = asyncio.run(
        chunk_repository.count_by_index_version(
            session, KB_ID, tenant_id=TENANT_ID, index_version=OLD
        )
    )
    assert remaining == 2, "v1 的切片还在：回滚能成立的前提就是归档版本的数据没被清掉"
