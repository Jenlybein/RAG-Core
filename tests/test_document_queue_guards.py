"""「排队中」带来的两个新权能：撤回排队（删除）与替换排队（重新提交）。

票 #217 / ADR-0034 决策一。这个模块只回答一个问题：**排队中的文档能不能操作**。
能，而且这正是这条改造存在的理由——

* 排队中没有租约，也就没有任何东西正在被写，删掉它撤回的只是一条**还没被执行的意图**；
* 只按「``status == 3`` 就拒」的口径在四态之后会把排队也一起拒掉，于是「排队」变成囚笼：
  传错一个文件要等它跑完才能撤回，而排队可能来自一次整库重建。

所以这里的断言分两层：守卫函数本身（纯函数，不需要库），以及仓库层那条
``UPDATE ... WHERE``（真 SQLite，能看见 ``rowcount`` 与行到底变成什么样）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.models.document import Document
from app.repositories import document_repository
from app.services import document_state
from app.utils.status import DocumentStatus

TENANT_ID = "tenant_demo"
KB_ID = "kb-1"


@pytest.fixture
def document_db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Document.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(
    db: Session,
    document_id: str,
    *,
    status: int,
    claimed_at: datetime | None = None,
) -> None:
    db.add(
        Document(
            id=document_id,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title=document_id,
            content="",
            source_type="markdown",
            status=status,
            index_claimed_at=claimed_at,
        )
    )
    db.commit()


def _document(status: int, *, claimed_at: datetime | None = None) -> SimpleNamespace:
    """守卫函数只看 ``status`` 与 ``index_claimed_at`` 两列。"""

    return SimpleNamespace(status=status, index_claimed_at=claimed_at)


# ───────────────────────────────────────────────── 守卫：排队中可删、可重交、可回滚


def test_a_queued_document_is_not_effectively_processing() -> None:
    """排队中没有租约，也就没有任何人在写它的切片。"""

    assert document_state.is_effectively_processing(_document(DocumentStatus.QUEUED)) is False


def test_a_queued_document_can_be_deleted() -> None:
    """删除排队中 = 撤回排队：守卫放行（不再回 20025）。"""

    document_state.ensure_deletable(_document(DocumentStatus.QUEUED))


def test_a_queued_document_can_be_resubmitted_without_force() -> None:
    """重新提交排队中 = 替换排队：不要求 ``force``（它不是「成功的文档」）。"""

    document_state.ensure_reindexable(_document(DocumentStatus.QUEUED), force=False)


def test_a_queued_document_can_take_a_new_version_or_a_rollback() -> None:
    """建版本 / 回滚的前置守卫同样只看租约：排队中放行。"""

    document_state.ensure_versionable(_document(DocumentStatus.QUEUED))


@pytest.mark.parametrize(
    "status",
    [DocumentStatus.SUCCESS, DocumentStatus.FAILED, DocumentStatus.QUEUED],
    ids=["success", "failed", "queued"],
)
def test_only_a_live_lease_blocks_the_operation(status: int) -> None:
    """20025 只在「索引中且租约有效」时返回——状态字面值不作证据。"""

    document_state.ensure_deletable(_document(status))

    with pytest.raises(AppError) as caught:
        document_state.ensure_deletable(_document(DocumentStatus.PROCESSING, claimed_at=now()))
    assert caught.value.code is ErrorCode.DOCUMENT_IS_PROCESSING


def test_a_queued_row_with_a_stale_lease_is_still_not_busy() -> None:
    """即便某一行在排队中残留了一个旧时间戳，它也不算在跑（判据先看状态）。"""

    stale = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1)
    assert (
        document_state.is_effectively_processing(_document(DocumentStatus.QUEUED, claimed_at=stale))
        is False
    )


# ───────────────────────────────────── 仓库层：排队中可被抢占 / 可被替换 / 可被撤回


def test_preparing_a_queued_row_replaces_the_queue(document_db: Session) -> None:
    """重新提交排队中的文档：准备入队成功，行仍是排队中且没有租约。"""

    _seed(document_db, "doc-queued", status=DocumentStatus.QUEUED)

    assert document_repository.prepare_for_indexing(
        document_db, "doc-queued", tenant_id=TENANT_ID, allow_success=False
    )
    document_db.commit()

    row = document_db.get(Document, "doc-queued")
    assert row is not None
    assert row.status == DocumentStatus.QUEUED
    assert row.index_claimed_at is None


def test_a_queued_row_is_claimable(document_db: Session) -> None:
    """Worker 认排队中：这是「4 ∪ 3 且租约失效 ∪ 2」里最主的一支。"""

    _seed(document_db, "doc-queued", status=DocumentStatus.QUEUED)

    assert document_repository.try_claim_for_indexing(
        document_db, "doc-queued", tenant_id=TENANT_ID, allow_success=False
    )
    document_db.commit()

    row = document_db.get(Document, "doc-queued")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.index_claimed_at is not None


def test_a_deleted_queued_row_cannot_be_claimed_by_the_late_task(document_db: Session) -> None:
    """撤回排队之后迟到的任务抢不到：``rowcount=0``，因此不会写出任何切片。"""

    _seed(document_db, "doc-queued", status=DocumentStatus.QUEUED)
    row = document_db.get(Document, "doc-queued")
    assert row is not None
    document_db.delete(row)
    document_db.commit()

    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-queued", tenant_id=TENANT_ID, allow_success=False
        )
        is None
    )


def test_claiming_a_success_document_still_requires_allow_success(document_db: Session) -> None:
    """排队中可抢占这件事没有放宽「成功文档」那一档：迟到重复投递抢不到。"""

    _seed(document_db, "doc-done", status=DocumentStatus.SUCCESS)

    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-done", tenant_id=TENANT_ID, allow_success=False
        )
        is None
    )


def test_release_normalises_indexing_back_to_queued_and_keeps_terminals(
    document_db: Session,
) -> None:
    """释放租约：``3 → 4``（它在等重投），但失败 / 成功不会被顺手改成排队中。"""

    _seed(document_db, "doc-running", status=DocumentStatus.PROCESSING, claimed_at=now())
    _seed(document_db, "doc-failed", status=DocumentStatus.FAILED, claimed_at=now())

    document_repository.release_index_claim(document_db, "doc-running", tenant_id=TENANT_ID)
    document_repository.release_index_claim(document_db, "doc-failed", tenant_id=TENANT_ID)
    document_db.commit()

    running = document_db.get(Document, "doc-running")
    failed = document_db.get(Document, "doc-failed")
    assert running is not None and failed is not None
    assert running.status == DocumentStatus.QUEUED
    assert running.index_claimed_at is None
    assert failed.status == DocumentStatus.FAILED, "终态不会被租约清理顺手改掉"
    assert failed.index_claimed_at is None


# ───────────────────────────────────────────── 异步孪生：控制面与 Worker 走同一份语句


def test_the_async_prepare_and_claim_see_the_same_queue_state(tmp_path: Path) -> None:
    """异步入口执行的是同一条 SQL：准备写 4、抢占写 3 + 租约。

    控制面（重建 / 建版本 / 回滚）与 Worker 都走 ``*_async`` 孪生，两者共用 statement
    builder。这条用例在**文件库**上跑一遍异步入口，确认同步与异步看到的行不是两种形状。
    """

    path = tmp_path / "queue-state.db"
    sync_engine = create_engine(f"sqlite:///{path}")
    Document.__table__.create(sync_engine)
    factory = sessionmaker(bind=sync_engine)
    with factory() as seed_session:
        seed_session.add(
            Document(
                id="doc-async",
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title="doc-async",
                content="",
                source_type="markdown",
                status=DocumentStatus.FAILED,
            )
        )
        seed_session.commit()

    async def _scenario() -> tuple[bool, datetime | None]:
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(bind=async_engine, expire_on_commit=False) as session:
                prepared = await document_repository.prepare_for_indexing_async(
                    session, "doc-async", tenant_id=TENANT_ID, allow_success=False
                )
                await session.commit()
                claimed = await document_repository.try_claim_for_indexing_async(
                    session, "doc-async", tenant_id=TENANT_ID, allow_success=False
                )
                await session.commit()
                return prepared, claimed
        finally:
            await async_engine.dispose()

    try:
        prepared, claimed = asyncio.run(_scenario())

        assert prepared is True and claimed is not None, "抢占回的是租约令牌（#213）"
        with factory() as check_session:
            row = check_session.get(Document, "doc-async")
            assert row is not None
            assert row.status == DocumentStatus.PROCESSING
            assert row.index_claimed_at is not None
    finally:
        sync_engine.dispose()
