"""票 #79：静态资源配额（库数 / 单库文档数 / 并发处理数）的写路径校验。

这三项**不记在 Redis 里**，而是到库里数行——它们是「现在有多少」，不是「用掉了多少次」。
在 Redis 里再存一份就必须在删除、状态流转、Worker 崩溃时同步，漏一处就是两个真相；
直接数库永远是对的，而写路径本来就不是热路径（一次 count 查询）。

分开两道接缝测：

* **计数**是真 SQL。SQLite 内存库只建 ``documents`` 一张表（``knowledge_bases`` 的 JSONB
  列 SQLite 建不出来），验证「按库、按租户、按状态」这三条查询真的只数该数的行——尤其是
  「跨租户的行不算」与「Worker 卡死的行不占名额」；
* **判定**是纯阈值比较：计到上限内放行，达到上限拒绝，错误码 ``20014``。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.models.document import Document
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PlanSpec
from app.repositories import document_repository, knowledge_base_repository
from app.services import static_quota
from app.utils.status import DocumentStatus

TENANT_ID = "tenant_acme"
OTHER_TENANT_ID = "tenant_other"
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"


@pytest.fixture
def db() -> Iterator[Session]:
    """只有 ``documents`` 一张表的 SQLite 内存库：不连 PostgreSQL，也不碰 JSONB。"""

    engine = create_engine("sqlite://")
    Document.__table__.create(engine)
    with Session(engine) as session:
        yield session


def _document(
    document_id: str,
    *,
    kb_id: str = KB_ID,
    tenant_id: str = TENANT_ID,
    status: int = DocumentStatus.SUCCESS,
    claimed_at: datetime | None = None,
) -> Document:
    return Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=document_id,
        content="正文",
        source_type="markdown",
        status=status,
        index_claimed_at=claimed_at,
    )


def _plan(**overrides: int) -> PlanSpec:
    """免费档做底：库 1、单库 30 篇、并发 1——三个限额都小到能一眼数清。"""

    values = {
        "max_kb": 1,
        "max_documents_per_kb": 30,
        "max_processing_documents": 1,
    }
    values.update(overrides)
    base = PLAN_PRESETS[PLAN_FREE]
    return PlanSpec(**{**base.__dict__, **values})


# ───────────────────────────────────────────── 计数：真 SQL


def test_counting_a_kbs_documents_ignores_other_kbs_and_other_tenants(db: Session) -> None:
    db.add_all(
        [
            _document("doc-1"),
            _document("doc-2"),
            _document("doc-3", kb_id=OTHER_KB_ID),
            _document("doc-4", tenant_id=OTHER_TENANT_ID),
        ]
    )
    db.flush()

    assert document_repository.count_by_kb(db, KB_ID, tenant_id=TENANT_ID) == 2
    assert document_repository.count_by_kb(db, OTHER_KB_ID, tenant_id=TENANT_ID) == 1
    assert document_repository.count_by_kb(db, KB_ID, tenant_id=OTHER_TENANT_ID) == 1


def test_the_processing_count_only_counts_documents_that_are_really_in_flight(
    db: Session,
) -> None:
    """成功 / 失败的不算；已排队未开工的算（它占着队列）；有效租约内的算。"""

    db.add_all(
        [
            _document("done", status=DocumentStatus.SUCCESS),
            _document("failed", status=DocumentStatus.FAILED),
            _document("queued", status=DocumentStatus.PROCESSING),
            _document(
                "running",
                status=DocumentStatus.PROCESSING,
                claimed_at=now(),
            ),
        ]
    )
    db.flush()

    assert document_repository.count_processing_for_tenant(db, tenant_id=TENANT_ID) == 2


def test_a_document_stuck_past_its_lease_does_not_hold_a_slot(db: Session) -> None:
    """Worker 崩了之后那一行还写着「处理中」，但它已经不占任何 Worker。

    把它也数进并发名额，等于一次崩溃永久吃掉一个名额，直到有人跑 requeue 脚本——
    租户会看到「上传被拒」而库里没有任何文档在处理，排障只能靠猜。
    """

    db.add(
        _document(
            "stuck",
            status=DocumentStatus.PROCESSING,
            claimed_at=now() - timedelta(hours=1),
        )
    )
    db.flush()

    assert document_repository.count_processing_for_tenant(db, tenant_id=TENANT_ID) == 0


# ───────────────────────────────────────────── 判定：阈值与错误码


def test_the_document_allowance_allows_the_last_upload_and_rejects_the_next_one(
    db: Session,
) -> None:
    plan = _plan(max_documents_per_kb=2)
    db.add(_document("doc-1"))
    db.flush()

    # 库里已有 1 篇、上限 2：这一篇正好是第 2 篇，放行。
    static_quota.ensure_upload_quota(db, tenant_id=TENANT_ID, kb_id=KB_ID, plan=plan)

    db.add(_document("doc-2"))
    db.flush()
    with pytest.raises(AppError) as exc:
        static_quota.ensure_upload_quota(db, tenant_id=TENANT_ID, kb_id=KB_ID, plan=plan)
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED


def test_the_concurrent_processing_allowance_is_enforced_on_upload(db: Session) -> None:
    """并发处理数是「一个租户别把 Worker 全占满」的那一条：它也在上传路径上。"""

    plan = _plan(max_processing_documents=1)
    db.add(_document("running", status=DocumentStatus.PROCESSING, claimed_at=now()))
    db.flush()

    with pytest.raises(AppError) as exc:
        static_quota.ensure_upload_quota(db, tenant_id=TENANT_ID, kb_id=KB_ID, plan=plan)
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED
    assert "processing" in exc.value.msg


def test_the_reindex_path_charges_the_concurrent_processing_allowance(db: Session) -> None:
    """重建同样入队一次解析任务，因此也要过并发处理数——否则它就是绕过口。"""

    plan = _plan(max_processing_documents=1)
    db.add(_document("queued", status=DocumentStatus.PROCESSING))
    db.flush()

    with pytest.raises(AppError) as exc:
        static_quota.ensure_processing_quota(db, tenant_id=TENANT_ID, plan=plan)
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED


def test_the_knowledge_base_allowance_rejects_the_n_plus_first_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库数是「这个租户能有几个库」：免费档 1 个，建第 2 个必须被拦。"""

    plan = _plan(max_kb=1)

    monkeypatch.setattr(knowledge_base_repository, "count_by_tenant", lambda _db, _t: 0)
    static_quota.ensure_kb_quota(None, tenant_id=TENANT_ID, plan=plan)  # type: ignore[arg-type]

    monkeypatch.setattr(knowledge_base_repository, "count_by_tenant", lambda _db, _t: 1)
    with pytest.raises(AppError) as exc:
        static_quota.ensure_kb_quota(None, tenant_id=TENANT_ID, plan=plan)  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.QUOTA_EXCEEDED
