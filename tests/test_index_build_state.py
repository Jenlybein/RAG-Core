"""#142 · 构建版本的状态机：谁能迁到谁、并发只允许一个、失败只落 failed。

接缝（父 spec 的 D 票「不加新缝」）：**构建服务**本身就是那条缝——它吃一个会话 + 几个
注入的依赖，把「一次构建」摊成若干次状态迁移。用例只看三件事：

* 库里落下了什么行（``building`` / ``failed`` / ``active`` / ``archived``）；
* 库上的 ``active_index_version`` 有**没有**被动过（构建期间它必须一个字节都不变）；
* 拒绝时抛的是哪一个业务异常（调用方据此回话）。

数据库是 ``AsyncRecordingSession``（既有的异步替身），ES / 向量库 / 索引流水线全部注入
替身：整套不连 PostgreSQL、不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppError, ErrorCode
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
    next_version_name,
    version_number,
)
from app.models.knowledge_base import KnowledgeBase
from app.services.index_build_service import (
    BUILD_FAILED_MESSAGE,
    IndexBuildAlreadyRunning,
    IndexBuildInvalidTransition,
    IndexBuildService,
    can_transition,
    describe_stalled_builds,
    ensure_transition,
)
from tests.fake_db import AsyncRecordingSession

TENANT_ID = "tenant_build"
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"


# ────────────────────────────────────────────────────────────── 替身


class _StubIndexing:
    """索引流水线替身：只记「重建了哪些文档、写进了哪个版本」。"""

    def __init__(self, *, fail_on: str | None = None, chunks_per_document: int = 2) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail_on = fail_on
        self.chunks_per_document = chunks_per_document

    async def index_existing_document(
        self,
        _db: Any,
        document_id: str,
        *,
        index_version: str | None = None,
        allow_success: bool = False,
        reparse: bool = False,
    ) -> int:
        self.calls.append((document_id, index_version))
        if document_id == self.fail_on:
            raise RuntimeError("embedding exploded")
        return self.chunks_per_document


def _kb(active: str | None = "v1", *, kb_id: str = KB_ID) -> SimpleNamespace:
    """一行知识库（检索 / 构建只读这几个字段）。"""

    return SimpleNamespace(id=kb_id, tenant_id=TENANT_ID, active_index_version=active)


def _version(
    *,
    version: str = "v1",
    status: str = INDEX_BUILD_ACTIVE,
    kb_id: str = KB_ID,
    created_at: datetime | None = None,
    build_id: str | None = None,
) -> IndexBuildVersion:
    """一行版本：默认是「已经在生效的 v1」。"""

    return IndexBuildVersion(
        id=build_id or f"build-{kb_id}-{version}",
        tenant_id=TENANT_ID,
        kb_id=kb_id,
        version=version,
        status=status,
        created_at=created_at or datetime(2026, 11, 1, 9, 0, 0),
    )


def _session(
    *, versions: list[IndexBuildVersion] | None = None, kb: Any | None = None
) -> AsyncRecordingSession:
    """异步假会话：预置版本行与知识库行（其余查询按替身口径回空）。"""

    return AsyncRecordingSession(
        rows={
            IndexBuildVersion: list(versions or []),
            KnowledgeBase: [kb] if kb is not None else [],
        }
    )


def _service(indexing: Any | None = None) -> IndexBuildService:
    """构建服务：三个依赖全部注入替身（构造它不该连任何外部服务）。"""

    return IndexBuildService(
        indexing=indexing if indexing is not None else _StubIndexing(),
        vector_store=SimpleNamespace(),
        keyword_search=SimpleNamespace(available=False),
    )


# ────────────────────────────────────────────────────── 版本名：纯函数


def test_next_version_name_goes_up_from_the_highest_used_number() -> None:
    """``max + 1`` 而不是 ``len + 1``：删过 v2 之后，下一个仍不能又叫 v2。"""

    assert next_version_name([]) == "v1"
    assert next_version_name(["v1"]) == "v2"
    assert next_version_name(["v1", "v3"]) == "v4", "跳过编号不该把新版本叫成 v3"
    assert next_version_name(["v2", "v1"]) == "v3"


def test_version_number_ignores_names_it_cannot_order() -> None:
    """排不出版本序的名字不参与计算，也不抛错（一行手写坏数据不该卡死整条流程）。"""

    assert version_number("v12") == 12
    assert version_number("release") is None
    assert next_version_name(["release", "v2"]) == "v3"


# ─────────────────────────────────────────────────────────── 状态机真值表


def test_the_state_machine_allows_exactly_the_documented_transitions() -> None:
    """ADR-0025 的那张图，一条不多一条不少。"""

    assert can_transition(INDEX_BUILD_BUILDING, INDEX_BUILD_READY)
    assert can_transition(INDEX_BUILD_BUILDING, INDEX_BUILD_FAILED)
    assert can_transition(INDEX_BUILD_READY, INDEX_BUILD_ACTIVE)
    assert can_transition(INDEX_BUILD_READY, INDEX_BUILD_FAILED)
    assert can_transition(INDEX_BUILD_ACTIVE, INDEX_BUILD_ARCHIVED)

    assert not can_transition(INDEX_BUILD_BUILDING, INDEX_BUILD_ACTIVE), (
        "building 直接 active 会把半成品推上线"
    )
    assert not can_transition(INDEX_BUILD_ARCHIVED, INDEX_BUILD_ACTIVE), (
        "归档是终态：要回到旧版本得另建一版，不许悄悄复活"
    )
    assert not can_transition(INDEX_BUILD_FAILED, INDEX_BUILD_READY)


def test_an_illegal_transition_names_both_ends() -> None:
    """拒绝时要说清「从哪到哪」，否则日志里只剩一句看不懂的 50000。"""

    with pytest.raises(IndexBuildInvalidTransition) as error:
        ensure_transition(_version(status=INDEX_BUILD_BUILDING), INDEX_BUILD_ACTIVE)

    assert (error.value.current, error.value.target) == (
        INDEX_BUILD_BUILDING,
        INDEX_BUILD_ACTIVE,
    )


# ──────────────────────────────────────────────────────── 起一个构建


def test_start_build_writes_a_building_row_and_leaves_the_live_version_alone() -> None:
    """新构建落到 ``building``，库上的生效版本**不动**（线上照旧查 v1）。"""

    kb = _kb("v1")
    session = _session(versions=[_version(version="v1")], kb=kb)

    row = asyncio.run(_service().start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    assert row.status == INDEX_BUILD_BUILDING
    assert row.version == "v2", "已有 v1，新版本从 v2 起"
    assert row.tenant_id == TENANT_ID and row.kb_id == KB_ID
    assert kb.active_index_version == "v1", "构建期间生效版本一个字节都不许改"
    assert [obj for obj in session.added if isinstance(obj, IndexBuildVersion)] == [row]
    assert session.commits == 1, "building 行必须当场落库，否则并发守卫看不见它"


def test_start_build_adopts_the_live_version_when_the_table_has_no_row_for_it() -> None:
    """建库只写了库列（#141 的落法）：构建开始时要把它补成一行 active。

    不补的话，激活时「旧版本 archived」没有行可以归档，而库列被改掉之后 v1 的数据就
    永远停在无人认领的状态。
    """

    session = _session(versions=[], kb=_kb("v1"))

    row = asyncio.run(_service().start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    added = [obj for obj in session.added if isinstance(obj, IndexBuildVersion)]
    assert [(obj.version, obj.status) for obj in added] == [
        ("v1", INDEX_BUILD_ACTIVE),
        ("v2", INDEX_BUILD_BUILDING),
    ], "先认领 v1（active），再开 v2（building）"
    assert row.version == "v2"


def test_a_second_build_for_the_same_kb_is_refused() -> None:
    """同一库同时只允许一个 building（D1 的部分唯一索引；这里断言业务侧的拒绝）。"""

    running = _version(version="v2", status=INDEX_BUILD_BUILDING)
    session = _session(versions=[_version(version="v1"), running], kb=_kb("v1"))

    with pytest.raises(IndexBuildAlreadyRunning) as error:
        asyncio.run(_service().start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    assert error.value.running is running, "要能说出「正在建的是哪一行」"
    assert session.added == [], "被拒的请求什么都不写"
    assert session.commits == 0


def test_a_concurrent_insert_that_races_the_check_is_refused_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读与写之间的窗口：另一个请求抢先插了 building 行 → 唯一约束报错。

    只做「先读一次」是假守卫（TOCTOU）。真正的裁决在那条部分唯一索引上，所以这条路径
    必须把 ``IntegrityError`` 翻译成**同一个**业务异常，而不是把 500 抛给调用方。
    """

    from app.repositories import index_version_repository

    async def _boom(_db: Any, _row: Any) -> Any:
        raise IntegrityError("INSERT INTO index_build_versions ...", {}, Exception("dup"))

    monkeypatch.setattr(index_version_repository, "create", _boom)
    session = _session(versions=[_version(version="v1")], kb=_kb("v1"))

    with pytest.raises(IndexBuildAlreadyRunning):
        asyncio.run(_service().start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    assert session.rollbacks == 1, "失败那一笔要回滚，别把半成品留在事务里"
    assert session.commits == 0


def test_start_build_refuses_an_unknown_knowledge_base() -> None:
    """库不存在 / 跨租户：同一个 20008，不额外泄露存在性（ADR-0006）。"""

    session = _session(versions=[], kb=None)

    with pytest.raises(AppError) as error:
        asyncio.run(_service().start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    assert error.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert session.added == []


# ─────────────────────────────────────────────────────────── 失败留痕


def test_mark_failed_records_the_reason_without_touching_the_live_version() -> None:
    """失败只落 ``failed`` + 固定原因；库列与旧 active 行原样不动。"""

    live = _version(version="v1", status=INDEX_BUILD_ACTIVE)
    building = _version(version="v2", status=INDEX_BUILD_BUILDING)
    kb = _kb("v1")
    session = _session(versions=[live, building], kb=kb)

    row = asyncio.run(_service().mark_failed(session, building.id, tenant_id=TENANT_ID))

    assert row is not None
    assert row.status == INDEX_BUILD_FAILED
    assert row.error_message == BUILD_FAILED_MESSAGE
    assert kb.active_index_version == "v1"
    assert live.status == INDEX_BUILD_ACTIVE, "线上那一版不许被一次失败改掉"


def test_a_late_failure_on_the_active_build_changes_nothing() -> None:
    """生效版本收到迟到的失败上报：什么都不改，只记一条 warning。"""

    live = _version(version="v1", status=INDEX_BUILD_ACTIVE)
    session = _session(versions=[live], kb=_kb("v1"))

    row = asyncio.run(_service().mark_failed(session, live.id, tenant_id=TENANT_ID))

    assert row is not None and row.status == INDEX_BUILD_ACTIVE
    assert row.error_message is None
    assert session.commits == 0, "没改就不该提交"


def test_mark_failed_on_an_unknown_build_is_a_quiet_no_op() -> None:
    """收尾路径不该因为「这一行不在了」再抛一次异常，把真正的原因盖掉。"""

    session = _session(versions=[], kb=_kb("v1"))

    assert asyncio.run(_service().mark_failed(session, "nope", tenant_id=TENANT_ID)) is None
    assert session.commits == 0


# ────────────────────────────────────────── 「有行、没任务」：重启后能看见


def test_stalled_builds_are_the_building_rows_older_than_the_threshold() -> None:
    """进程被杀之后只剩一行 ``building``：按年龄把它指出来（线上不受影响）。"""

    now = datetime(2026, 11, 14, 12, 0, 0)
    old = _version(
        version="v2",
        status=INDEX_BUILD_BUILDING,
        created_at=now - timedelta(hours=3),
        build_id="build-old",
    )
    fresh = _version(
        version="v3",
        status=INDEX_BUILD_BUILDING,
        created_at=now - timedelta(minutes=5),
        build_id="build-fresh",
    )
    session = _session(versions=[old, fresh], kb=_kb("v1"))
    service = _service()

    found = asyncio.run(
        service.list_stalled_builds(session, tenant_id=TENANT_ID, older_than_seconds=3600, at=now)
    )

    assert [row.id for row in found] == ["build-old"], "刚开始的那一行是在跑，不是卡住"
    assert "build-old" in describe_stalled_builds(found)
    assert describe_stalled_builds([]) == "no stalled index build"
