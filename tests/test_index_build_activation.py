"""#142 · 四项计数校验与**单事务**激活。

三条验收在这里落地：计数不等不许 ``ready``、激活后旧版本 archived + 别名指向新版本 + 检索
pin 到新版本、以及「构建被取消 / 进程死掉不会留下半成品 active」。

接缝仍是构建服务本身 + 注入的向量存储 / 关键词 Provider；数据库是异步替身。整套不连
PostgreSQL、不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.document import Document
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
from app.services.index_build_service import (
    BUILD_COUNTS_MISMATCH_MESSAGE,
    BUILD_FAILED_MESSAGE,
    BuildCounts,
    IndexBuildCountsMismatch,
    IndexBuildInvalidTransition,
    IndexBuildService,
    describe_stalled_builds,
)
from tests.fake_db import AsyncRecordingSession, AsyncTransactionalSession

TENANT_ID = "tenant_activate"
KB_ID = "kb-1"
LIVE = "v1"
TARGET = "v2"


# ──────────────────────────────────────────────────────────────── 替身


class _RecordingKeywordProvider(NoopKeywordSearchProvider):
    """关键词 Provider 替身：记录别名切换，并回答「这一版有多少文档」。"""

    def __init__(self, *, documents: int = 3, available: bool = True) -> None:
        self.documents = documents
        self._available = available
        self.activate_calls: list[str] = []
        self.count_calls: list[dict[str, Any]] = []
        self.fail_activate: set[str] = set()

    @property
    def available(self) -> bool:
        return self._available

    async def count_documents(self, *, tenant_id: str, kb_id: str, index_version: str) -> int:
        self.count_calls.append(
            {"tenant_id": tenant_id, "kb_id": kb_id, "index_version": index_version}
        )
        return self.documents

    async def activate_version(self, version: str) -> None:
        self.activate_calls.append(version)
        if version in self.fail_activate:
            raise RuntimeError("alias switch exploded")


class _RecordingVectorStore:
    """向量存储替身：回答某一版本的向量条数。"""

    def __init__(self, *, vectors: int = 3) -> None:
        self.vectors = vectors
        self.count_calls: list[dict[str, Any]] = []

    async def count_by_scope(
        self, _db: Any, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        self.count_calls.append(
            {"tenant_id": tenant_id, "kb_id": kb_id, "index_version": index_version}
        )
        return self.vectors


class _StubIndexing:
    """构建循环的流水线替身：可选地在某一篇文档上抛指定异常。"""

    def __init__(self, *, fail_on: str | None = None, exc: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.exc = exc

    async def index_existing_document(
        self,
        _db: Any,
        document_id: str,
        *,
        index_version: str | None = None,
        allow_success: bool = False,
        reparse: bool = False,
    ) -> int:
        self.calls.append(document_id)
        if document_id == self.fail_on:
            raise self.exc if self.exc is not None else RuntimeError("boom")
        return 1


class _FailingCommitSession(AsyncTransactionalSession):
    """提交必炸的会话：用来验证「同成同败」的收尾路径（别名切回去、异常照抛）。

    用 :class:`AsyncTransactionalSession` 而不是普通记录型替身：真库回滚会撤销这一笔写进
    库行的新版本，替身也得撤销——否则「失败之后重读库列」读到的是**没提交**的那个值，
    回切别名那条路径就验反了（#156）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.commit_attempts = 0

    async def commit(self) -> None:
        self.commit_attempts += 1
        raise RuntimeError("commit failed")


def _kb(active: str | None = LIVE) -> SimpleNamespace:
    return SimpleNamespace(id=KB_ID, tenant_id=TENANT_ID, active_index_version=active)


def _row(
    version: str,
    status: str,
    *,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> IndexBuildVersion:
    return IndexBuildVersion(
        id=build_id or f"build-{version}",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=version,
        status=status,
        created_at=created_at or datetime(2026, 11, 1, 9, 0, 0),
    )


def _chunk(chunk_id: str, *, version: str = TARGET) -> Any:
    """一行切片（真 ORM 对象，不 flush）：给「切片数」那一次计数读数用。"""

    from app.models.chunk import Chunk

    return Chunk(
        id=chunk_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        document_id="doc-1",
        content="正文",
        embedding=[0.0] * 1536,
        meta={},
        index_version=version,
    )


def _session(
    *,
    versions: list[IndexBuildVersion],
    kb: Any | None = None,
    documents: list[Any] | None = None,
    chunks: list[Any] | None = None,
) -> AsyncRecordingSession:
    return AsyncRecordingSession(
        rows={
            KnowledgeBase: [kb] if kb is not None else [],
            Document: list(documents or []),
            IndexBuildVersion: list(versions),
            Chunk: list(chunks or []),
        }
    )


def _document(document_id: str) -> SimpleNamespace:
    from app.utils.status import DocumentStatus

    return SimpleNamespace(
        id=document_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="售后手册",
        doc_version=1,
        status=DocumentStatus.SUCCESS,
    )


def _service(
    *,
    indexing: Any | None = None,
    vectors: int = 3,
    documents: int = 3,
    keyword_available: bool = True,
) -> tuple[IndexBuildService, _RecordingVectorStore, _RecordingKeywordProvider]:
    store = _RecordingVectorStore(vectors=vectors)
    keyword = _RecordingKeywordProvider(documents=documents, available=keyword_available)
    service = IndexBuildService(
        indexing=indexing if indexing is not None else _StubIndexing(),
        vector_store=store,
        keyword_search=keyword,
    )
    return service, store, keyword


# ──────────────────────────────────────── 四项计数：各自从哪来、不等就不许 ready


def test_the_four_counts_come_from_the_four_storages() -> None:
    """知识库数、切片数、向量数、关键词文档数各读各的存储，且都带目标版本。"""

    service, store, keyword = _service(vectors=5, documents=5)
    session = _session(versions=[], kb=_kb(LIVE))
    kb = _kb(LIVE)

    counts = asyncio.run(service.collect_counts(session, kb=kb, version=TARGET))

    assert counts.knowledge_bases == 1
    assert store.count_calls == [{"tenant_id": TENANT_ID, "kb_id": KB_ID, "index_version": TARGET}]
    assert keyword.count_calls == [
        {"tenant_id": TENANT_ID, "kb_id": KB_ID, "index_version": TARGET}
    ], "数的是目标版本的物理索引，不是生效那一版"


def test_chunks_are_counted_from_the_database_under_the_target_version() -> None:
    """切片数走仓储的计数语句（真 SQL 在 SQLite 上验：见下面的用例）。"""

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

    from app.repositories import chunk_repository

    async def _run() -> int:
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE chunks (id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64), "
                    "kb_id VARCHAR(36), document_id VARCHAR(36), content TEXT, embedding TEXT, "
                    "metadata JSON, index_version VARCHAR(32))"
                )
            )
            for chunk_id, version in [("c1", LIVE), ("c2", TARGET), ("c3", TARGET)]:
                await connection.execute(
                    text(
                        "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding,"
                        " metadata, index_version) VALUES (:id, :tenant, :kb, 'doc-1', 'x', '[]',"
                        " '{}', :version)"
                    ),
                    {"id": chunk_id, "tenant": TENANT_ID, "kb": KB_ID, "version": version},
                )
        async with AsyncSession(engine) as session:
            try:
                return await chunk_repository.count_by_index_version(
                    session, KB_ID, tenant_id=TENANT_ID, index_version=TARGET
                )
            finally:
                await session.rollback()
                await engine.dispose()

    assert asyncio.run(_run()) == 2, "只数目标版本的两行，v1 那一行不算"


def test_mark_ready_is_refused_when_the_four_counts_disagree() -> None:
    """计数不相等 → ready 被**拒绝**，行仍是 building，一个字节都不提交。

    这种版本一旦上线，表现是「有些文档搜不到」，而且不报错——ADR-0025 用这条硬闸门
    换掉「看起来差不多就上」。
    """

    service, _, _ = _service()
    building = _row(TARGET, INDEX_BUILD_BUILDING)
    session = _session(versions=[building], kb=_kb(LIVE))
    counts = BuildCounts(knowledge_bases=1, chunks=10, vectors=10, keyword_documents=9)

    with pytest.raises(IndexBuildCountsMismatch) as error:
        asyncio.run(service.mark_ready(session, building.id, tenant_id=TENANT_ID, counts=counts))

    assert error.value.counts == counts, "拒绝时要带上那四个数字（排障全靠它）"
    assert error.value.msg == BUILD_COUNTS_MISMATCH_MESSAGE
    assert building.status == INDEX_BUILD_BUILDING
    assert session.commits == 0


@pytest.mark.parametrize(
    "chunks, vectors, keyword",
    [
        (3, 2, 3),  # 向量少了
        (3, 3, 2),  # 关键词文档少了
        (0, 0, 3),  # 关键词多写了（旧版本的数据没清干净）
    ],
)
def test_every_dimension_can_refuse_the_ready(
    chunks: int, vectors: int, keyword: int | None
) -> None:
    """四个维度里任何一个对不上都拒绝——不是只比「切片与向量」。"""

    counts = BuildCounts(
        knowledge_bases=1, chunks=chunks, vectors=vectors, keyword_documents=keyword
    )

    assert counts.matched is False


def test_equal_counts_are_accepted_and_snapshotted() -> None:
    """四项对得上 → ready，并把切片数写进计数快照（``document_count`` 在 build 里落）。"""

    service, _, _ = _service()
    building = _row(TARGET, INDEX_BUILD_BUILDING)
    session = _session(versions=[building], kb=_kb(LIVE))
    counts = BuildCounts(knowledge_bases=1, chunks=7, vectors=7, keyword_documents=7)

    row = asyncio.run(service.mark_ready(session, building.id, tenant_id=TENANT_ID, counts=counts))

    assert row.status == INDEX_BUILD_READY
    assert row.chunk_count == 7
    assert session.commits == 1


def test_ready_needs_building_as_the_current_status() -> None:
    """已经 failed / active 的行不能再被标 ready（状态机只有一张真值表）。"""

    service, _, _ = _service()
    failed = _row(TARGET, INDEX_BUILD_FAILED)
    session = _session(versions=[failed], kb=_kb(LIVE))

    with pytest.raises(IndexBuildInvalidTransition):
        asyncio.run(
            service.mark_ready(
                session,
                failed.id,
                tenant_id=TENANT_ID,
                counts=BuildCounts(1, 3, 3, 3),
            )
        )


def test_a_missing_keyword_capability_is_not_counted_as_a_mismatch() -> None:
    """没部署 ES（空实现）时这一维不参与：否则那样的部署永远到不了 ready。"""

    service, _, keyword = _service(keyword_available=False, vectors=0)
    session = _session(versions=[], kb=_kb(LIVE))

    counts = asyncio.run(service.collect_counts(session, kb=_kb(LIVE), version=TARGET))

    assert counts.keyword_documents is None
    assert counts.matched is True
    assert keyword.count_calls == [], "能力不存在就不去问它要数字"


# ──────────────────────────────────────────────── 激活：单事务、别名、生效版本


def test_activation_archives_the_old_build_and_moves_the_effective_version() -> None:
    """旧版本 archived、新版本 active、库列更新——三者一起，一次提交。"""

    service, _, keyword = _service()
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    ready = _row(TARGET, INDEX_BUILD_READY, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(versions=[live, ready], kb=kb)

    row = asyncio.run(service.activate(session, "build-v2", tenant_id=TENANT_ID))

    assert row.status == INDEX_BUILD_ACTIVE
    assert row.activated_at is not None
    assert live.status == INDEX_BUILD_ARCHIVED
    assert live.archived_at is not None
    assert kb.active_index_version == TARGET
    assert session.commits == 1, "一次提交 = 三者同成同败"
    assert keyword.activate_calls == [TARGET], "别名切到新版本的物理索引"


def test_retrieval_moves_to_the_new_build_only_after_activation() -> None:
    """激活前 pin 在 v1，激活后 pin 在 v2——响应契约一个字没变，变的只是库上那一列。"""

    service, _, _ = _service()
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    ready = _row(TARGET, INDEX_BUILD_READY, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(versions=[live, ready], kb=kb)

    before = dict(resolve_index_versions([kb]))
    asyncio.run(service.activate(session, "build-v2", tenant_id=TENANT_ID))
    after = dict(resolve_index_versions([kb]))

    assert before == {KB_ID: LIVE}
    assert after == {KB_ID: TARGET}


def test_activation_requires_the_ready_status() -> None:
    """``building`` 直接激活会把半成品推上线：拒绝，且不碰任何东西。"""

    service, _, keyword = _service()
    building = _row(TARGET, INDEX_BUILD_BUILDING, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(versions=[building], kb=kb)

    with pytest.raises(IndexBuildInvalidTransition):
        asyncio.run(service.activate(session, "build-v2", tenant_id=TENANT_ID))

    assert kb.active_index_version == LIVE
    assert keyword.activate_calls == []
    assert session.commits == 0


def test_a_failed_commit_switches_the_alias_back() -> None:
    """别名先切、事务后提交：提交失败就把别名切回去（否则关键词会一直查新索引）。"""

    service, _, keyword = _service()
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    ready = _row(TARGET, INDEX_BUILD_READY, build_id="build-v2")
    kb = _kb(LIVE)
    session = _FailingCommitSession(rows={KnowledgeBase: [kb], IndexBuildVersion: [live, ready]})

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(service.activate(session, "build-v2", tenant_id=TENANT_ID))

    assert keyword.activate_calls == [TARGET, LIVE], "切到新版本失败后切回旧版本"
    assert session.commit_attempts == 1


def test_activation_of_a_kb_without_a_previous_build_still_works() -> None:
    """第一次构建（库上还没有生效版本）：只把新版本激活，没有旧行要归档。"""

    service, _, keyword = _service()
    ready = _row(TARGET, INDEX_BUILD_READY, build_id="build-v2")
    kb = _kb(None)
    session = _session(versions=[ready], kb=kb)

    row = asyncio.run(service.activate(session, "build-v2", tenant_id=TENANT_ID))

    assert row.status == INDEX_BUILD_ACTIVE
    assert kb.active_index_version == TARGET
    assert keyword.activate_calls == [TARGET]


# ──────────────────────────────────── 取消 / 崩溃：不留半成品 active


def test_a_cancelled_build_never_leaves_a_half_active_version() -> None:
    """构建中途被取消：行留在 building（**不是** active），线上照旧查旧版本。

    为什么「留在 building」是对的：在被取消的上下文里写一笔可提交的事务本身不可靠。
    它不影响线上（active 仍是旧版本），且下一节的「陈旧检测」会把它指出来。
    """

    indexing = _StubIndexing(fail_on="doc-2", exc=asyncio.CancelledError())
    service, _, keyword = _service(indexing=indexing)
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    building = _row(TARGET, INDEX_BUILD_BUILDING, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(
        versions=[live, building],
        kb=kb,
        documents=[_document("doc-1"), _document("doc-2")],
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.build(session, "build-v2", tenant_id=TENANT_ID))

    assert building.status == INDEX_BUILD_BUILDING, "取消不是失败上报，它停在原地"
    assert kb.active_index_version == LIVE
    assert live.status == INDEX_BUILD_ACTIVE
    assert keyword.activate_calls == [], "取消的构建绝不会切别名"


def test_a_crashed_build_is_named_by_the_stalled_check_after_a_restart() -> None:
    """重启之后「有 building 行、没有任务」要能被指出来（否则下一次构建永远被挡）。"""

    service, _, _ = _service()
    now = datetime(2026, 11, 14, 12, 0, 0)
    stuck = _row(
        TARGET,
        INDEX_BUILD_BUILDING,
        build_id="build-stuck",
        created_at=now - timedelta(hours=5),
    )
    session = _session(versions=[stuck], kb=_kb(LIVE))

    found = asyncio.run(
        service.list_stalled_builds(session, tenant_id=TENANT_ID, older_than_seconds=3600, at=now)
    )

    assert [row.id for row in found] == ["build-stuck"]
    assert "build-stuck" in describe_stalled_builds(found)


# ───────────────────────────────────── 端到端：build 跑完 → ready（不激活）


def test_build_ends_at_ready_with_the_counts_snapshotted() -> None:
    """整条构建：逐文档重建 → 四项计数 → ready；线上仍 pin 旧版本。"""

    indexing = _StubIndexing()
    service, store, keyword = _service(indexing=indexing, vectors=2, documents=2)
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    building = _row(TARGET, INDEX_BUILD_BUILDING, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(
        versions=[live, building],
        kb=kb,
        documents=[_document("doc-1"), _document("doc-2")],
        chunks=[_chunk("c1"), _chunk("c2"), _chunk("c-old", version=LIVE)],
    )

    row = asyncio.run(service.build(session, "build-v2", tenant_id=TENANT_ID))

    assert indexing.calls == ["doc-1", "doc-2"]
    assert row.status == INDEX_BUILD_READY
    assert row.document_count == 2
    assert row.chunk_count == 2, "计数快照来自 slabs=2 / vectors=2 / keyword=2 的共同读数"
    assert kb.active_index_version == LIVE, "ready 不等于上线"
    assert keyword.activate_calls == []
    assert store.count_calls[0]["index_version"] == TARGET


def test_a_build_with_mismatched_counts_lands_failed_with_that_reason() -> None:
    """计数对不上 → 构建落 failed + **计数不一致**这个原因（不是笼统的「跑崩了」）。"""

    service, _, _ = _service(vectors=1, documents=3)
    live = _row(LIVE, INDEX_BUILD_ACTIVE, build_id="build-v1")
    building = _row(TARGET, INDEX_BUILD_BUILDING, build_id="build-v2")
    kb = _kb(LIVE)
    session = _session(
        versions=[live, building], kb=kb, documents=[_document("doc-1"), _document("doc-2")]
    )

    with pytest.raises(IndexBuildCountsMismatch):
        asyncio.run(service.build(session, "build-v2", tenant_id=TENANT_ID))

    assert building.status == INDEX_BUILD_FAILED
    assert building.error_message == BUILD_COUNTS_MISMATCH_MESSAGE
    assert kb.active_index_version == LIVE


def test_a_generic_build_failure_uses_the_generic_reason() -> None:
    """一般性失败只留固定文案：异常原文绝不进库（失败留痕的同一条纪律）。"""

    indexing = _StubIndexing(fail_on="doc-2", exc=RuntimeError("SECRET stack detail"))
    service, _, _ = _service(indexing=indexing)
    building = _row(TARGET, INDEX_BUILD_BUILDING, build_id="build-v2")
    session = _session(
        versions=[building], kb=_kb(LIVE), documents=[_document("doc-1"), _document("doc-2")]
    )

    with pytest.raises(RuntimeError):
        asyncio.run(service.build(session, "build-v2", tenant_id=TENANT_ID))

    assert building.status == INDEX_BUILD_FAILED
    assert building.error_message == BUILD_FAILED_MESSAGE
    assert "SECRET" not in (building.error_message or "")


# ───────────────────────────────────────────────────── 任务模块接线


def test_the_build_task_is_included_by_the_celery_app() -> None:
    """Worker 从 app.celery_app 启动时必须自带构建任务模块，否则整库重建没人认领。"""

    from app.celery_app import celery_app

    assert "app.tasks.index_build" in list(celery_app.conf.include or [])


def test_importing_the_build_task_registers_it() -> None:
    """导入即注册（Celery 派生全名）。"""

    from app.celery_app import celery_app

    import app.tasks.index_build  # noqa: F401 - 导入本身是被测行为

    assert "app.tasks.index_build.build_index_version_task" in celery_app.tasks
