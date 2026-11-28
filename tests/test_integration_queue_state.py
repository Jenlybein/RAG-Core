"""真实 PostgreSQL 上的「排队中」状态集成用例（票 #217 / ADR-0034 决策一）。

为什么必须在真库上跑：这一票要守的是**库里那几行到底是什么样**——上传之后的状态与租约、
抢占之后的 4→3、删掉一行之后迟到的任务还能不能抢到、迁移语句对存量行的效果。离线替身
（``tests/fake_db.py``）明写「写语句不模拟行的变化」，在那条缝上断言状态只会得到假绿；
而「``3`` 且租约已过期」这类判断连时间窗口都要数据库来评（``UPDATE ... WHERE`` 的
``rowcount`` 就是判据本身）。

与 ``tests/test_integration_cas.py`` 同一套开关与纪律：
``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块 skip；只建自己用到的两张表、只删自己
种下的行（按本文件专用的 ``tenant_id`` 收口），绝不 DROP 表、也绝不碰别人的数据。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

import pytest
from sqlalchemy import Engine, create_engine, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, now
from app.db.transaction import async_transaction
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_FREE, PLAN_PRESETS
from app.repositories import document_repository
from app.services import document_service
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

#: 本文件专用的租户：清理只按它收口，绝不扫别人的行。
TENANT_ID = "tenant_queue_state"
KB_ID = "kb-queue-state"

#: 逐条种下的行（跨用例复用同一批 id，每个用例前后都清一遍）。
SEED_IDS = (
    "qs-queued-claim",
    "qs-queued-deleted",
    "qs-expired-reclaim",
    "qs-live-lease",
    "qs-migration-missing-lease",
    "qs-migration-live-lease",
    "qs-migration-queued",
    "qs-migration-success",
    "qs-migration-failed",
)

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MIGRATION_SLUG = "queue_document_status"

T = TypeVar("T")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """在真实库上建 ``documents`` 与 ``knowledge_bases`` 两张表（上传要读库）。"""

    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(INTEGRATION_DATABASE_URL)
    Base.metadata.create_all(bind=engine, tables=[Document.__table__, KnowledgeBase.__table__])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=db_engine)()
    _delete_seeded_rows(session)
    try:
        yield session
    finally:
        _delete_seeded_rows(session)
        session.close()


def _delete_seeded_rows(db: Session) -> None:
    """只删本文件种下的行：按专用租户 + 显式 id 收口。"""

    try:
        db.execute(delete(Document).where(Document.tenant_id == TENANT_ID))
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID))
        db.commit()
    except Exception:
        db.rollback()
        raise


def _seed_kb(db: Session) -> None:
    """一份能通过 ``ensure_visible`` 的最小知识库行（上传要按库判准入）。"""

    db.merge(
        KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="排队状态集成库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            settings={},
        )
    )
    db.commit()


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


def _use_selector_loop_on_windows() -> None:
    """psycopg 的异步模式不支持缺省的 ``ProactorEventLoop``（ADR-0023）。"""

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run(scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """在**一个**循环里跑完整个异步场景（异步会话不能跨循环复用）。"""

    url = INTEGRATION_DATABASE_URL
    assert url is not None, "整模块已在没有 RAG_INTEGRATION_DATABASE_URL 时 skip"

    async def _main() -> T:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(bind=engine, expire_on_commit=False) as session:
                return await scenario(session)
        finally:
            await engine.dispose()

    _use_selector_loop_on_windows()
    return asyncio.run(_main())


def _load_migration() -> ModuleType:
    """按路径装迁移模块：``migrations/versions`` 不是可导入的包。

    迁移里的两条语句都以模块常量交出（``MARK_QUEUED`` / ``RESTORE_PROCESSING``），
    于是「升级做的是什么」与「测试跑的是什么」是**同一份文本**，不是抄一遍。
    """

    matches = sorted(VERSIONS_DIR.glob(f"*_{MIGRATION_SLUG}.py"))
    assert len(matches) == 1, f"{MIGRATION_SLUG}: 期望恰好一个迁移文件，实际 {matches}"
    spec = importlib.util.spec_from_file_location("t217_queue_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────── 上传：受理即排队中，不写租约


def test_upload_registers_queued_without_a_lease(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传只做登记：库里落一行 ``4 排队中``，租约为空；任务在提交之后才投递。"""

    _seed_kb(db)
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        document_service, "enqueue_document", lambda **kwargs: published.append(kwargs)
    )

    async def _scenario(session: AsyncSession) -> dict[str, Any]:
        async with async_transaction(session):
            return await document_service.upload(
                session,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                plan=PLAN_PRESETS[PLAN_FREE],
                title="售后手册",
                content="# 退款流程\n\n见附件。",
            )

    data = _run(_scenario)

    assert data["status"] == DocumentStatus.QUEUED, "回执就是「已收下、在排队」"
    assert published == [
        {
            "document_id": data["document_id"],
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": None,
            "reparse": False,
            "doc_version": 1,
        }
    ], "投递发生在提交之后，载荷带这次算的稿号"

    row = db.get(Document, data["document_id"])
    assert row is not None
    assert row.status == DocumentStatus.QUEUED
    assert row.index_claimed_at is None, "入队侧绝不写租约：租约只由 Worker 在抢占时写"


# ──────────────────────────────────────────────── 抢占：4 → 3 + 租约


def test_claiming_a_queued_document_writes_indexing_and_a_lease(db: Session) -> None:
    """Worker 抢到即 ``4 → 3`` 并盖租约——这是「索引中」的唯一权威证据。"""

    _seed(db, "qs-queued-claim", status=DocumentStatus.QUEUED)

    assert document_repository.try_claim_for_indexing(
        db, "qs-queued-claim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()

    row = db.get(Document, "qs-queued-claim")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.index_claimed_at is not None


def test_a_live_lease_cannot_be_claimed_again(db: Session) -> None:
    """同一份「索引中」只有一个 Worker：有效租约下的第二次抢占必须失败。"""

    _seed(db, "qs-live-lease", status=DocumentStatus.PROCESSING, claimed_at=now())

    assert not document_repository.try_claim_for_indexing(
        db, "qs-live-lease", tenant_id=TENANT_ID, allow_success=False
    )


# ──────────────────────────────────────────────── 抢回：租约过期可被重新抢占


def test_an_expired_lease_can_be_reclaimed(db: Session) -> None:
    """租约过期即「没人跑」：清扫器重投之后，Worker 必须抢得到并盖上**新的**租约。"""

    stale = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 60)
    _seed(db, "qs-expired-reclaim", status=DocumentStatus.PROCESSING, claimed_at=stale)

    assert document_repository.try_claim_for_indexing(
        db, "qs-expired-reclaim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()

    row = db.get(Document, "qs-expired-reclaim")
    assert row is not None
    assert row.index_claimed_at is not None
    assert row.index_claimed_at >= stale + timedelta(
        seconds=document_repository.DOCUMENT_LEASE_SECONDS
    ), "抢占写入的是新的租约，不是原来那个过期时间戳"


# ─────────────────────────────────── 撤回排队：删掉之后迟到的任务抢不到、不产切片


def test_deleting_a_queued_row_leaves_the_late_task_unclaimable(db: Session) -> None:
    """排队中可删除（=撤回排队）：迟到的任务在 CAS 上拿到 ``rowcount=0``，因此不产生切片。

    这条用例守的正是那个承诺：删除与「后台还有一条在路上的消息」可以同时成立，
    而后者不会把文档写回来——它连抢占都抢不到。
    """

    _seed(db, "qs-queued-deleted", status=DocumentStatus.QUEUED)

    row = db.get(Document, "qs-queued-deleted")
    assert row is not None
    db.delete(row)
    db.commit()

    assert (
        document_repository.try_claim_for_indexing(
            db, "qs-queued-deleted", tenant_id=TENANT_ID, allow_success=False
        )
        is False
    ), "行已经不在了：迟到的任务抢不到，也就不会写出任何切片"
    assert db.get(Document, "qs-queued-deleted") is None, "删除之后不能被迟到任务「复活」"


# ───────────────────────────────────────────── 迁移：存量「3 且无租约」→ 排队中


def test_the_migration_moves_only_legacy_processing_rows_to_queued(db: Session) -> None:
    """存量迁移只动 ``3 且无租约``：在跑的那一行、以及别的终态一行都不许改。

    这是审计与排障的分界线——把租约有效的那一行也改成 4，等于声称「没有人在跑」，
    而它明明正在写切片。
    """

    migration = _load_migration()
    fresh = now()
    _seed(db, "qs-migration-missing-lease", status=DocumentStatus.PROCESSING)
    _seed(db, "qs-migration-live-lease", status=DocumentStatus.PROCESSING, claimed_at=fresh)
    _seed(db, "qs-migration-queued", status=DocumentStatus.QUEUED)
    _seed(db, "qs-migration-success", status=DocumentStatus.SUCCESS)
    _seed(db, "qs-migration-failed", status=DocumentStatus.FAILED)

    db.execute(migration.MARK_QUEUED)
    db.commit()

    def _status(document_id: str) -> int:
        row = db.get(Document, document_id)
        assert row is not None
        return row.status

    assert _status("qs-migration-missing-lease") == DocumentStatus.QUEUED
    assert _status("qs-migration-live-lease") == DocumentStatus.PROCESSING, "租约有效 = 真在跑"
    assert _status("qs-migration-queued") == DocumentStatus.QUEUED
    assert _status("qs-migration-success") == DocumentStatus.SUCCESS
    assert _status("qs-migration-failed") == DocumentStatus.FAILED

    # 反向迁移：所有 4 退回 3（租约仍为空，与改造前的形状逐字一致）。
    db.execute(migration.RESTORE_PROCESSING)
    db.commit()

    assert _status("qs-migration-missing-lease") == DocumentStatus.PROCESSING
    assert _status("qs-migration-queued") == DocumentStatus.PROCESSING
    assert _status("qs-migration-live-lease") == DocumentStatus.PROCESSING
