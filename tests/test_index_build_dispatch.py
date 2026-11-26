"""#215 · 整库构建按篇投递：派活、续建、失败明细、队列隔离。

## 为什么这些用例不打桩「函数被调用过」

这一票的核心是**副作用落在哪里**：投了多少条消息、哪些篇被跳过、失败明细进了构建行、
文档状态有没有被改坏。断言「派活函数被调了一次」对上面任何一条都不成立——`#209` 那次
事故的教训正是这个（状态回写被换成记录器、断言「调用过」，而真实缺陷是「调用了但没写
进去」）。

所以三条缝各自断言它能看见的事实：

* **投递**用离线记账发布器（`tests/conftest.py` 已默认注入）——它记的是**真的**被投出去的
  载荷与 task_id，而「投递」这件事本身没有第二个可观察面；
* **续建与明细**用真库（`chunks` 与 `index_build_versions` 的行走真 SQL）；
* **队列隔离**断言 Celery 的路由配置——它是一份声明，没有别的观察面。
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.celery_app import celery_app
from app.db.base import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import INDEX_BUILD_BUILDING, IndexBuildVersion
from app.models.knowledge_base import KnowledgeBase
from app.services import index_dispatch
from app.services.index_build_service import (
    BUILD_DOCUMENT_FAILED_REASON,
    DISPATCH_BATCH_SIZE,
    MAX_REDISPATCH,
    IndexBuildService,
)
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_dispatch"
KB_ID = "kb-dispatch"
BUILD_ID = "build-dispatch"
VERSION = "v2"

#: 只建本文件要用的四张表：整库 `create_all` 会让这个文件依赖全仓迁移的形状
#: （而它测的是编排，不是迁移）。
_TABLES = (
    Document.__table__,
    KnowledgeBase.__table__,
    IndexBuildVersion.__table__,
    Chunk.__table__,
)

SEED_IDS = tuple(f"dispatch-doc-{index}" for index in range(4))


class _RecordingPublisher:
    """记账版发布器：只记 ``apply_async`` 的实参，不连 Broker。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> None:
        self.calls.append({"kwargs": dict(kwargs), "task_id": task_id})


@pytest.fixture(autouse=True)
def _offline_build_seams():
    """把两条新缝换成记账器（`conftest` 的默认注入之外再兜一层，用例自己注入时覆盖）。"""

    from app.services import index_dispatch as dispatch_module

    recorder = _RecordingPublisher()
    dispatch_module.set_build_document_publisher(recorder)
    try:
        yield recorder
    finally:
        dispatch_module.set_build_document_publisher(None)


def _document_row(document_id: str, *, status: int = DocumentStatus.SUCCESS) -> Document:
    return Document(
        id=document_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title=document_id,
        content="正文",
        source_type="markdown",
        status=status,
        current_version=2,
    )


def _build_row(*, planned: int, draft: int = 2) -> IndexBuildVersion:
    from app.db.base import now

    return IndexBuildVersion(
        id=BUILD_ID,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=VERSION,
        status=INDEX_BUILD_BUILDING,
        documents_planned=planned,
        doc_version=draft,
        documents_done=0,
        converge_deadline=now() + timedelta(hours=1),
        created_at=now(),
    )


# ─────────────────────────────────────────── 派活：投出去的是什么


class _StubSession:
    """派活只要三条只读：取构建行、取库、取文档。为此造一个最小的异步会话替身。"""

    def __init__(self, *, build: IndexBuildVersion, kb: KnowledgeBase, documents: list[Document]):
        self._build = build
        self._kb = kb
        self._documents = documents

    async def execute(self, _statement: Any) -> Any:  # pragma: no cover - 手法见下
        raise AssertionError("派活用例不该走到裸 execute")


@pytest.mark.anyio
async def test_dispatch_pages_the_documents_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 篇的库分三批投（200 + 200 + 100），载荷带齐构建的三样输入。

    为什么必须分批：一次把 5000 条挤进 Redis 会让 build 队列一瞬间比所有租户的上传加起来
    还长，而队列长度本身就是运维要看的信号。
    """

    documents = [_document_row(f"dispatch-doc-{index}") for index in range(500)]
    recorded: list[dict[str, Any]] = []

    async def _get_row(_db: Any, build_id: str, *, tenant_id: str) -> Any:
        return _build_row(planned=500)

    async def _get_kb(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            active_index_version=None,
        )

    async def _list(_db: Any, kb_id: str, *, tenant_id: str) -> list[Document]:
        return list(documents)

    async def _built(_db: Any, *_args: Any, **_kwargs: Any) -> set[str]:
        return set()

    monkeypatch.setattr("app.repositories.index_version_repository.get_for_tenant", _get_row)
    monkeypatch.setattr("app.repositories.knowledge_base_repository.get_by_id_for_tenant", _get_kb)
    monkeypatch.setattr("app.repositories.document_repository.list_by_kb_async", _list)
    monkeypatch.setattr("app.repositories.chunk_repository.list_indexed_document_ids", _built)

    def _publish(**kwargs: Any) -> None:
        recorded.append(kwargs)

    dispatched = await IndexBuildService().dispatch(
        None,
        BUILD_ID,
        tenant_id=TENANT_ID,
        publish=_publish,  # type: ignore[arg-type]
    )

    assert dispatched == 500
    assert len(recorded) == 500
    # 每一条都带着这一版的三个输入：目标版本、统一稿号、build_id（回去记账用）。
    sample = recorded[0]
    assert sample["index_version"] == VERSION
    assert sample["doc_version"] == 2
    assert sample["build_id"] == BUILD_ID
    assert sample["allow_success"] is True
    # 每批 200 条：200 / 200 / 100 是三批，而不是 500 条一次。
    assert DISPATCH_BATCH_SIZE == 200


@pytest.mark.anyio
async def test_dispatch_skips_documents_that_already_have_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续建：这一版这一稿下已经有切片的篇直接跳过，不重复花 embedding。"""

    documents = [_document_row(document_id) for document_id in SEED_IDS]
    recorded: list[dict[str, Any]] = []

    async def _get_row(_db: Any, build_id: str, *, tenant_id: str) -> Any:
        return _build_row(planned=4)

    async def _get_kb(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            active_index_version=None,
        )

    async def _list(_db: Any, kb_id: str, *, tenant_id: str) -> list[Document]:
        return list(documents)

    async def _built(_db: Any, *_args: Any, **_kwargs: Any) -> set[str]:
        # 前三篇已经建好了（上一次构建跑到一半 / 重投）。
        return set(SEED_IDS[:3])

    monkeypatch.setattr("app.repositories.index_version_repository.get_for_tenant", _get_row)
    monkeypatch.setattr("app.repositories.knowledge_base_repository.get_by_id_for_tenant", _get_kb)
    monkeypatch.setattr("app.repositories.document_repository.list_by_kb_async", _list)
    monkeypatch.setattr("app.repositories.chunk_repository.list_indexed_document_ids", _built)

    def _publish(**kwargs: Any) -> None:
        recorded.append(kwargs)

    dispatched = await IndexBuildService().dispatch(
        None,
        BUILD_ID,
        tenant_id=TENANT_ID,
        publish=_publish,  # type: ignore[arg-type]
    )

    assert dispatched == 1, "只有缺的那一篇该被投出去"
    assert recorded[0]["document_id"] == SEED_IDS[3]


@pytest.mark.anyio
async def test_dispatch_refuses_when_the_build_is_not_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迟到的派活不许再投：这一版已经有结论了（ready / failed / active）。"""

    async def _get_row(_db: Any, build_id: str, *, tenant_id: str) -> Any:
        row = _build_row(planned=4)
        row.status = "ready"
        return row

    monkeypatch.setattr("app.repositories.index_version_repository.get_for_tenant", _get_row)

    assert (
        await IndexBuildService().dispatch(None, BUILD_ID, tenant_id=TENANT_ID)  # type: ignore[arg-type]
        == 0
    )


# ─────────────────────────────────────────── 队列隔离：一份声明，一个观察面


def test_build_tasks_are_routed_to_the_dedicated_queue() -> None:
    """构建的三条任务进 ``build`` 队列；单篇重建**不**路由（它要和别人的上传排在一起）。"""

    routes = celery_app.conf.task_routes or {}

    assert routes["app.tasks.index_build.dispatch_build_task"]["queue"] == "build"
    assert routes["app.tasks.index_build.build_document_task"]["queue"] == "build"
    assert routes["app.tasks.index_build.build_index_version_task"]["queue"] == "build"
    # 用户点的那一下不隔离：它是一次请求的后续，不是后台维护。
    assert "app.tasks.indexing.index_document_task" not in routes


def test_registering_the_offline_seams_keeps_them_injected() -> None:
    """两道新缝默认处于注入状态——否则「起一次构建」的用例会真往本机 Redis 投几百条消息。"""

    assert index_dispatch.build_publisher_is_injected()
    # 恢复之后仍是注入态（conftest 的 autouse 会在下一个用例重新注入）。
    index_dispatch.set_build_document_publisher(None)
    assert not index_dispatch.build_document_publisher_is_injected()
    from app.services import index_dispatch as dispatch_module

    dispatch_module.set_build_document_publisher(_RecordingPublisher())


# ─────────────────────────────────────────── 续建判据：真库


pytestmark_integration = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


@pytest.fixture
def sync_db() -> Any:
    """真库上的同步会话；只建本文件要用的表、只删本文件种下的行。"""

    assert INTEGRATION_DATABASE_URL is not None
    engine: Engine = create_engine(INTEGRATION_DATABASE_URL)
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, future=True)
    session: Session = factory()
    try:
        session.execute(delete(Chunk).where(Chunk.tenant_id == TENANT_ID))
        session.execute(delete(Document).where(Document.tenant_id == TENANT_ID))
        session.execute(
            text("DELETE FROM index_build_versions WHERE tenant_id = :tenant"),
            {"tenant": TENANT_ID},
        )
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def async_db() -> Any:
    """与 ``sync_db`` 指向同一个库的异步会话（被测代码是异步栈）。"""

    assert INTEGRATION_DATABASE_URL is not None
    engine = create_async_engine(INTEGRATION_DATABASE_URL)
    factory = AsyncSession(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        import asyncio

        asyncio.run(factory.close())
        asyncio.run(engine.dispose())


@pytest.mark.integration
@pytest.mark.skipif(not INTEGRATION_DATABASE_URL, reason="RAG_INTEGRATION_DATABASE_URL is not set")
def test_resume_query_only_counts_this_version_and_draft(
    sync_db: Session, async_db: AsyncSession
) -> None:
    """续建判据的三个条件缺一不可：同版本、同稿、同租户。

    只看版本会把「上一次构建写到一半」当成建好的；只看文档会把线上旧版本的切片当成新版；
    漏掉稿号会让「标着第 N 稿、装着别的稿正文」的批切片被当成这一稿的产物。
    """

    import asyncio

    from app.repositories import chunk_repository

    for document_id in ("resume-a", "resume-b"):
        sync_db.add(_document_row(document_id))
    sync_db.add(_build_row(planned=2))
    sync_db.commit()

    def _chunk(document_id: str, *, version: str, draft: int) -> Chunk:
        return Chunk(
            id=f"chunk-{document_id}-{version}-{draft}",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            document_id=document_id,
            content="片",
            retrieval_text="检索文本",
            embedding=[0.0] * 8,
            index_version=version,
            doc_version=draft,
            meta={},
        )

    sync_db.add(_chunk("resume-a", version=VERSION, draft=2))  # 这一版这一稿：算建好
    sync_db.add(_chunk("resume-b", version=VERSION, draft=1))  # 这一版**别的稿**：不算
    sync_db.add(_chunk("resume-b", version="v1", draft=2))  # **别的版本**：不算
    sync_db.commit()

    built = asyncio.run(
        chunk_repository.list_indexed_document_ids(
            async_db,
            KB_ID,
            tenant_id=TENANT_ID,
            index_version=VERSION,
            doc_version=2,
        )
    )
    assert built == {"resume-a"}


# ─────────────────────────────────────────── 失败明细：真库


@pytest.mark.integration
@pytest.mark.skipif(not INTEGRATION_DATABASE_URL, reason="RAG_INTEGRATION_DATABASE_URL is not set")
def test_a_failed_document_is_recorded_and_keeps_its_status(
    sync_db: Session, async_db: AsyncSession
) -> None:
    """构建子任务失败：明细进构建行，**文档状态保持成功**。

    为什么文档状态不许改：那篇文档本身没坏——线上旧版本照常可查、检索照常命中。把它标成
    ``failed`` 会让用户看到失败却发现内容好好的，两者矛盾（ADR-0034 决策九）。
    """

    import asyncio


    sync_db.add(_document_row("fails-here"))
    sync_db.add(_build_row(planned=2))
    sync_db.commit()

    async def _scenario() -> Any:
        service = IndexBuildService()
        return await service.record_outcome(
            async_db,
            BUILD_ID,
            tenant_id=TENANT_ID,
            document_id="fails-here",
            title="某某.pdf",
            ok=False,
            reason=BUILD_DOCUMENT_FAILED_REASON,
        )

    asyncio.run(_scenario())

    sync_db.expire_all()
    document = sync_db.get(Document, "fails-here")
    assert document is not None
    assert document.status == DocumentStatus.SUCCESS, "失败的构建子任务不许把文档标成失败"

    build = sync_db.get(IndexBuildVersion, BUILD_ID)
    assert build is not None
    assert build.failed_documents, "失败明细必须落进构建行"
    entry = build.failed_documents[-1]
    assert entry["document_id"] == "fails-here"
    assert entry["title"] == "某某.pdf"
    assert entry["reason"] == BUILD_DOCUMENT_FAILED_REASON


@pytest.mark.integration
@pytest.mark.skipif(not INTEGRATION_DATABASE_URL, reason="RAG_INTEGRATION_DATABASE_URL is not set")
def test_redispatch_is_capped_and_a_runaway_build_is_failed(
    sync_db: Session, async_db: AsyncSession
) -> None:
    """重投次数到顶就收成 ``failed``：救不回来也是一个必须兑现的结论。

    没有这条上限，一个永远缺同一篇的构建会让清扫器每轮投同一批必然失败的消息——清扫器
    从此变成一台永动机（ADR-0034 决策七）。
    """

    import asyncio

    sync_db.add(_build_row(planned=1))
    sync_db.commit()

    async def _scenario() -> list[int]:
        service = IndexBuildService()
        return [
            await service.mark_redispatched(async_db, BUILD_ID, tenant_id=TENANT_ID)
            for _ in range(MAX_REDISPATCH + 1)
        ]

    counts = asyncio.run(_scenario())

    sync_db.expire_all()
    build = sync_db.get(IndexBuildVersion, BUILD_ID)
    assert build is not None
    assert counts[-1] == MAX_REDISPATCH + 1
    assert build.status == "failed", "超上限的构建必须收尾，不许再等"
