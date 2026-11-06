"""真实 PostgreSQL 上的索引权集成用例（唯一允许连真实库的测试文件）。

默认离线测试集不会碰它：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块 skip，
CI 里只有 ``workflow_dispatch`` 触发的 ``integration`` job 会设这个变量
（见 .github/workflows/ci.yml）。用例直接建 ``documents`` 表并跑真实的
``UPDATE ... WHERE``，验证单元测试用内存 SQLite 覆盖不到的数据库行为：

* 准备入队（``prepare_for_indexing``，不盖租约）之后，Worker 还能抢占；
* 有效租约下第二个抢占失败（并发守卫）；
* ``release_index_claim`` 之后下一次抢占成功（重试生命周期）；
* 有效租约内 ``prepare_for_indexing`` 拒绝（没人能偷走别人的处理中任务）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import Engine, create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, now
from app.models.document import Document
from app.repositories import document_repository
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_integration"
KB_ID = "kb-integration"
SEED_IDS = (
    "it-prepare-then-claim",
    "it-second-claim",
    "it-release-reclaim",
    "it-live-lease",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """在真实库上建 ``documents`` 表；只删本文件种下的行，绝不 DROP 表。"""

    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(INTEGRATION_DATABASE_URL)
    Base.metadata.create_all(bind=engine, tables=[Document.__table__])
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
    try:
        db.execute(delete(Document).where(Document.id.in_(SEED_IDS)))
        db.commit()
    except Exception:
        db.rollback()
        raise


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
            source_type="text",
            status=status,
            index_claimed_at=claimed_at,
        )
    )
    db.commit()


def test_prepare_then_claim_succeeds_on_real_postgres(db: Session) -> None:
    """重建契约：准备入队不盖租约，Worker 随后必须抢得到。"""

    _seed(db, "it-prepare-then-claim", status=DocumentStatus.SUCCESS)

    assert document_repository.prepare_for_indexing(
        db, "it-prepare-then-claim", tenant_id=TENANT_ID, allow_success=True
    )
    db.commit()

    row = db.get(Document, "it-prepare-then-claim")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.index_claimed_at is None

    assert document_repository.try_claim_for_indexing(
        db, "it-prepare-then-claim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()

    row = db.get(Document, "it-prepare-then-claim")
    assert row is not None
    assert row.index_claimed_at is not None


def test_second_claim_on_live_lease_fails_on_real_postgres(db: Session) -> None:
    """并发守卫：同一份文档的第二次抢占必须失败。"""

    _seed(db, "it-second-claim", status=DocumentStatus.FAILED)
    assert document_repository.try_claim_for_indexing(
        db, "it-second-claim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()

    assert not document_repository.try_claim_for_indexing(
        db, "it-second-claim", tenant_id=TENANT_ID, allow_success=False
    )


def test_release_re_enables_the_claim_on_real_postgres(db: Session) -> None:
    """重试生命周期：claim → release → claim 必须再次成功。"""

    _seed(db, "it-release-reclaim", status=DocumentStatus.FAILED)
    assert document_repository.try_claim_for_indexing(
        db, "it-release-reclaim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()

    document_repository.release_index_claim(db, "it-release-reclaim", tenant_id=TENANT_ID)
    db.commit()

    row = db.get(Document, "it-release-reclaim")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.index_claimed_at is None

    assert document_repository.try_claim_for_indexing(
        db, "it-release-reclaim", tenant_id=TENANT_ID, allow_success=False
    )
    db.commit()


def test_live_lease_makes_prepare_refuse_on_real_postgres(db: Session) -> None:
    """有人正在跑时，准备入队必须拒绝，force 也不能偷走租约。"""

    _seed(
        db,
        "it-live-lease",
        status=DocumentStatus.PROCESSING,
        claimed_at=now(),
    )

    for allow_success in (False, True):
        assert not document_repository.prepare_for_indexing(
            db, "it-live-lease", tenant_id=TENANT_ID, allow_success=allow_success
        )
