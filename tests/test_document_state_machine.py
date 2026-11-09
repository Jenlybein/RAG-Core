"""T5 · 重建 / 删除 / 卡死恢复：离线验收（无 PostgreSQL、无网络、无 Broker）。

四个接缝：
  * 仓库层（内存 SQLite，只建 ``documents`` 表）—— 按 ``id`` + 租户取单条、
    按库 + 状态列表、卡死扫描。证明跨租户不可见、租约窗口判定与列表过滤正确。
  * ASGI 边界（TestClient + 假会话 + monkeypatched 仓储 / 投递 / 文件 / 存储）——
    重建与删除的状态矩阵、步骤顺序、purge 覆盖存储列表、删文件、删行与提交。
  * 两个运维脚本的公开函数（假会话 + monkeypatched 仓储与投递）—— dry-run 不
    投递、统计数字、退出码规则。
  * 脚本模块的 CLI 入口 —— 只验证统计行与「已投递 ≠ 已完成」提示确实打出来。

脚本通过文件路径加载：``scripts*`` 不在安装包里（见 pyproject 的 packages.find），
所以测试不靠 ``sys.path`` 上的根目录，而是显式加载文件；这也顺带验证了脚本
「导入即安全」（模块顶层没有连接数据库、没有解析 argv）。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.db.base import now
from app.models.document import Document
from app.repositories import document_repository
from app.services import document_storage
from app.utils.status import DocumentStatus
from tests.fake_db import ZERO_COUNT

DOC_ID = "doc-1"
TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
REINDEX_REQUEST_ID = "t5-reindex-req"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


# ────────────────────────────────────────────────────────────── 仓库层（内存 SQLite）


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


def _seed_document(
    db: Session,
    document_id: str,
    *,
    status: int,
    tenant_id: str = TENANT_ID,
    kb_id: str = KB_ID,
    claimed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> None:
    db.add(
        Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=document_id,
            content="",
            source_type="text",
            status=status,
            index_claimed_at=claimed_at,
            created_at=created_at or now(),
        )
    )
    db.commit()


def test_get_by_id_for_tenant_filters_id_and_tenant(document_db: Session) -> None:
    """同一 id 属于别的租户时按「不存在」处理，绝不泄露存在性（ADR-0006）。"""

    _seed_document(document_db, DOC_ID, status=DocumentStatus.SUCCESS)
    _seed_document(
        document_db, "doc-other", status=DocumentStatus.SUCCESS, tenant_id="tenant-other"
    )

    assert (
        document_repository.get_by_id_for_tenant(document_db, DOC_ID, tenant_id=TENANT_ID)
        is not None
    )
    assert (
        document_repository.get_by_id_for_tenant(document_db, DOC_ID, tenant_id="tenant-other")
        is None
    )
    assert (
        document_repository.get_by_id_for_tenant(document_db, "doc-missing", tenant_id=TENANT_ID)
        is None
    )


def test_list_by_kb_and_status_filters_tenant_kb_and_status(document_db: Session) -> None:
    """列表只回本租户 + 本库 + 指定状态的行，并按 created_at 升序。"""

    _seed_document(
        document_db,
        "doc-success-late",
        status=DocumentStatus.SUCCESS,
        created_at=datetime(2026, 1, 2, 1, 1, 1),
    )
    _seed_document(
        document_db,
        "doc-success-early",
        status=DocumentStatus.SUCCESS,
        created_at=datetime(2026, 1, 1, 1, 1, 1),
    )
    _seed_document(document_db, "doc-failed", status=DocumentStatus.FAILED)
    _seed_document(
        document_db, "doc-other-tenant", status=DocumentStatus.SUCCESS, tenant_id="other"
    )
    _seed_document(document_db, "doc-other-kb", status=DocumentStatus.SUCCESS, kb_id="kb-other")

    rows = document_repository.list_by_kb_and_status(
        document_db, KB_ID, tenant_id=TENANT_ID, status=DocumentStatus.SUCCESS
    )

    assert [row.id for row in rows] == ["doc-success-early", "doc-success-late"]


def test_list_stuck_selects_processing_with_missing_or_expired_claim(
    document_db: Session,
) -> None:
    """卡死 = PROCESSING 且（从未开工 ‖ 抢占时间早于 now - lease）；有效租约不算。"""

    expired = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1)
    fresh = now()
    _seed_document(document_db, "doc-never-claimed", status=DocumentStatus.PROCESSING)
    _seed_document(document_db, "doc-expired", status=DocumentStatus.PROCESSING, claimed_at=expired)
    _seed_document(document_db, "doc-running", status=DocumentStatus.PROCESSING, claimed_at=fresh)
    _seed_document(document_db, "doc-failed", status=DocumentStatus.FAILED)
    _seed_document(document_db, "doc-success", status=DocumentStatus.SUCCESS)

    rows = document_repository.list_stuck(document_db)

    assert [row.id for row in rows] == ["doc-never-claimed", "doc-expired"]


def test_list_stuck_honours_kb_tenant_and_lease_filters(document_db: Session) -> None:
    expired = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1)
    _seed_document(document_db, "doc-target", status=DocumentStatus.PROCESSING, claimed_at=expired)
    _seed_document(
        document_db,
        "doc-wrong-kb",
        status=DocumentStatus.PROCESSING,
        claimed_at=expired,
        kb_id="kb-other",
    )
    _seed_document(
        document_db,
        "doc-wrong-tenant",
        status=DocumentStatus.PROCESSING,
        claimed_at=expired,
        tenant_id="tenant-other",
    )

    assert [
        row.id
        for row in document_repository.list_stuck(
            document_db,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            lease_seconds=document_repository.DOCUMENT_LEASE_SECONDS,
        )
    ] == ["doc-target"]
    assert [
        row.id
        for row in document_repository.list_stuck(
            document_db, kb_id=KB_ID, lease_seconds=document_repository.DOCUMENT_LEASE_SECONDS + 10
        )
    ] == [], "更长的租约让「过期」重新变成「有效处理中」"


# ───────────────────────────────────────── 准备入队 ≠ 抢占（Finding 0 的回归门）


def test_prepare_then_claim_makes_a_reindex_actually_indexable(
    document_db: Session,
) -> None:
    """重建必须真的可索引：准备只标记排队，不盖租约，Worker 之后仍能抢到。"""

    _seed_document(document_db, "doc-reindex", status=DocumentStatus.SUCCESS)

    prepared = document_repository.prepare_for_indexing(
        document_db, "doc-reindex", tenant_id=TENANT_ID, allow_success=True
    )
    assert prepared is True
    document_db.commit()

    row = document_db.get(Document, "doc-reindex")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.index_claimed_at is None, "入队侧绝不能盖租约；租约只属于 Worker"

    claimed = document_repository.try_claim_for_indexing(
        document_db, "doc-reindex", tenant_id=TENANT_ID, allow_success=False
    )
    assert claimed is True, "旧的双重抢占会把这里变成 rowcount=0：重建静默 no-op"
    document_db.commit()

    row = document_db.get(Document, "doc-reindex")
    assert row is not None
    assert row.index_claimed_at is not None, "Worker 抢到后才写租约"


def test_prepare_is_tenant_scoped(document_db: Session) -> None:
    """准备入队也强制带租户：跨租户的文档不能被标记。"""

    _seed_document(
        document_db,
        "doc-other-tenant",
        status=DocumentStatus.SUCCESS,
        tenant_id="tenant-other",
    )

    assert (
        document_repository.prepare_for_indexing(
            document_db,
            "doc-other-tenant",
            tenant_id=TENANT_ID,
            allow_success=True,
        )
        is False
    )


def test_prepare_refuses_while_another_worker_holds_a_live_lease(
    document_db: Session,
) -> None:
    """有效租约内，准备与抢占都拒绝；``allow_success`` 也不能抢别人的活。"""

    _seed_document(
        document_db,
        "doc-running",
        status=DocumentStatus.PROCESSING,
        claimed_at=now(),
    )

    for allow_success in (False, True):
        assert (
            document_repository.prepare_for_indexing(
                document_db,
                "doc-running",
                tenant_id=TENANT_ID,
                allow_success=allow_success,
            )
            is False
        )
    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-running", tenant_id=TENANT_ID, allow_success=True
        )
        is False
    )


def test_a_second_claim_on_a_live_lease_fails(document_db: Session) -> None:
    """并发守卫仍成立：同一份文档只有一个 Worker 能抢到。"""

    _seed_document(document_db, "doc-once", status=DocumentStatus.FAILED)
    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-once", tenant_id=TENANT_ID, allow_success=False
        )
        is True
    )
    document_db.commit()

    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-once", tenant_id=TENANT_ID, allow_success=False
        )
        is False
    )


def test_release_then_reclaim_supports_the_retry_lifecycle(document_db: Session) -> None:
    """claim → 可重试失败 → release_index_claim → 下一次 claim 必须再次成功。"""

    _seed_document(document_db, "doc-retry", status=DocumentStatus.FAILED)
    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-retry", tenant_id=TENANT_ID, allow_success=False
        )
        is True
    )
    document_db.commit()

    # 模拟可重试失败：只释放租约，status 保持 PROCESSING（失败文档重试路径）。
    document_repository.release_index_claim(document_db, "doc-retry", tenant_id=TENANT_ID)
    document_db.commit()

    row = document_db.get(Document, "doc-retry")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING, "release 只清租约，不动状态"
    assert row.index_claimed_at is None

    assert (
        document_repository.try_claim_for_indexing(
            document_db, "doc-retry", tenant_id=TENANT_ID, allow_success=False
        )
        is True
    ), "不释放租约的话，重试的 CAS 永远抢不到"


def test_release_is_tenant_scoped(document_db: Session) -> None:
    """释放租约也强制带租户：跨租户的 id 清不掉别人的租约。"""

    _seed_document(
        document_db,
        "doc-other-tenant",
        status=DocumentStatus.PROCESSING,
        claimed_at=now(),
        tenant_id="tenant-other",
    )

    document_repository.release_index_claim(document_db, "doc-other-tenant", tenant_id=TENANT_ID)
    document_db.commit()

    row = document_db.get(Document, "doc-other-tenant")
    assert row is not None
    assert row.index_claimed_at is not None


# ───────────────────────────────────────────────────────── 端点接缝的替身与夹具


class _FakeSession:
    """最小会话替身：只记录 purge / 删文件 / 删行 / 提交的调用顺序，不碰数据库。"""

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls if calls is not None else []
        self.deleted: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def add(self, _doc: Any) -> None:
        self.calls.append("add")

    def flush(self) -> None:
        return None

    def execute(self, _statement: Any) -> Any:
        # 重建前的并发处理配额会数一次行：替身表示「没有别的文档在处理」。
        return ZERO_COUNT

    def delete(self, doc: Any) -> None:
        self.deleted.append(doc)
        self.calls.append("delete-row")

    def commit(self) -> None:
        self.commits += 1
        self.calls.append("commit")

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _RecordingStore:
    """向量存储替身：记录 purge，任何写入 / 检索都算越界。"""

    def __init__(self, calls: list[str], name: str = "pgvector") -> None:
        self.calls = calls
        self.name = name
        self.deleted: list[str] = []
        self.tenant_ids: list[str] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str) -> None:
        self.calls.append(f"purge:{self.name}")
        self.deleted.append(document_id)
        self.tenant_ids.append(tenant_id)

    async def add_chunks(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("重建 / 删除端点不写切片；写入只属于 Worker 的单事务")

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("重建 / 删除端点不做检索")


def _document(
    *,
    status: int = DocumentStatus.FAILED,
    claimed_at: datetime | None = None,
    tenant_id: str = TENANT_ID,
    document_id: str = DOC_ID,
    source_file_path: str | None = None,
) -> SimpleNamespace:
    """文档行替身：字段形状与 ``app.models.document.Document`` 对齐。"""

    return SimpleNamespace(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=KB_ID,
        title="Demo",
        content="body",
        source_type="text",
        source_file_path=source_file_path,
        source_filename=None,
        status=status,
        index_claimed_at=claimed_at,
        error_message=None,
        created_at=datetime(2026, 1, 1, 1, 1, 1),
    )


def _install_lookup(monkeypatch: pytest.MonkeyPatch, doc: Any | None) -> None:
    """把按 id + 租户取文档换成内存查找；跨租户与不存在都返回 None。"""

    def _lookup(_db: Any, document_id: str, *, tenant_id: str) -> Any | None:
        if doc is None or document_id != doc.id or tenant_id != doc.tenant_id:
            return None
        return doc

    monkeypatch.setattr(document_repository, "get_by_id_for_tenant", _lookup)


@pytest.fixture
def offline_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """把端点接缝全部换成离线替身：假会话、假存储、投递记录、文件删除记录。"""

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    routes = importlib.import_module("app.api.routes.documents")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    calls: list[str] = []
    session = _FakeSession(calls)
    enqueued: list[dict[str, Any]] = []
    prepares: list[dict[str, Any]] = []
    store = _RecordingStore(calls)

    def _remove_file(path: str | None) -> None:
        calls.append("remove-file")
        document_storage.remove_document_file(path)

    def _prepare(_db: Any, document_id: str, **kwargs: Any) -> bool:
        prepares.append({"document_id": document_id, **kwargs})
        return True

    monkeypatch.setattr(routes, "get_vector_store", lambda: store)
    monkeypatch.setattr(routes, "enqueue_document", lambda **kwargs: enqueued.append(kwargs))
    monkeypatch.setattr(routes, "remove_document_file", _remove_file)
    monkeypatch.setattr(document_repository, "prepare_for_indexing", _prepare)

    main.app.dependency_overrides[session_mod.get_db] = lambda: session
    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                session=session,
                calls=calls,
                enqueued=enqueued,
                prepares=prepares,
                store=store,
            )
    finally:
        main.app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────── 重建索引端点


def test_reindex_failed_document_is_enqueued_with_reparse_forwarded(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败文档直接放行：先抢占后提交，再以 reparse=True 投递，立即回 3。"""

    _install_lookup(monkeypatch, _document(status=DocumentStatus.FAILED))

    response = offline_api.client.post(
        f"/api/v1/documents/{DOC_ID}/reindex",
        params={"reparse": "true"},
        headers={"X-Request-ID": REINDEX_REQUEST_ID},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {
        "document_id": DOC_ID,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "reparse": True,
    }
    assert offline_api.prepares == [
        {"document_id": DOC_ID, "tenant_id": TENANT_ID, "allow_success": False}
    ]
    assert offline_api.session.commits == 1, "准备入队后立刻提交，只持锁一瞬间"
    assert offline_api.enqueued == [
        {
            "document_id": DOC_ID,
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": REINDEX_REQUEST_ID,
            "reparse": True,
        }
    ]


def test_reindex_success_document_without_force_requires_force(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功文档必须显式 ``?force=true``，否则 20027，且不抢占、不投递。"""

    _install_lookup(monkeypatch, _document(status=DocumentStatus.SUCCESS))

    response = offline_api.client.post(f"/api/v1/documents/{DOC_ID}/reindex")

    body = response.json()
    assert body["code"] == ErrorCode.REINDEX_REQUIRES_FORCE
    assert body["msg"] == "reindex requires force"
    assert body["data"] is None
    assert offline_api.prepares == []
    assert offline_api.enqueued == []
    assert offline_api.session.commits == 0


def test_reindex_success_document_with_force_is_prepared_and_enqueued(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?force=true`` 让成功文档也走重建：准备入队时带 allow_success=True。"""

    _install_lookup(monkeypatch, _document(status=DocumentStatus.SUCCESS))

    response = offline_api.client.post(
        f"/api/v1/documents/{DOC_ID}/reindex",
        params={"force": "true"},
        headers={"X-Request-ID": "t5-force-req"},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {
        "document_id": DOC_ID,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "reparse": False,
    }
    assert offline_api.prepares == [
        {"document_id": DOC_ID, "tenant_id": TENANT_ID, "allow_success": True}
    ]
    assert offline_api.session.commits == 1
    assert offline_api.enqueued == [
        {
            "document_id": DOC_ID,
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": "t5-force-req",
            "reparse": False,
        }
    ]


def test_reindex_live_processing_document_is_rejected(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有效租约内的处理中：20025，即使带 force 也一样，且不抢占、不投递。"""

    _install_lookup(monkeypatch, _document(status=DocumentStatus.PROCESSING, claimed_at=now()))

    response = offline_api.client.post(
        f"/api/v1/documents/{DOC_ID}/reindex", params={"force": "true"}
    )

    body = response.json()
    assert body["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert offline_api.prepares == []
    assert offline_api.enqueued == []
    assert offline_api.session.commits == 0


def test_reindex_expired_lease_document_is_allowed(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """租约过期的处理中视为卡死：重建允许，不要求 force。"""

    stale = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1)
    _install_lookup(monkeypatch, _document(status=DocumentStatus.PROCESSING, claimed_at=stale))

    response = offline_api.client.post(f"/api/v1/documents/{DOC_ID}/reindex")

    body = response.json()
    assert body["code"] == 0
    assert offline_api.prepares == [
        {"document_id": DOC_ID, "tenant_id": TENANT_ID, "allow_success": False}
    ]
    assert offline_api.enqueued[0]["document_id"] == DOC_ID


def test_reindex_lost_prepare_race_returns_processing_conflict(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫通过但准备失败（并发请求先改同一行）：20025，不提交、不投递。"""

    _install_lookup(monkeypatch, _document(status=DocumentStatus.FAILED))
    monkeypatch.setattr(
        document_repository, "prepare_for_indexing", lambda *_args, **_kwargs: False
    )

    response = offline_api.client.post(f"/api/v1/documents/{DOC_ID}/reindex")

    body = response.json()
    assert body["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert offline_api.enqueued == []
    assert offline_api.session.commits == 0


@pytest.mark.parametrize("target", [None, "tenant-other"], ids=["missing", "cross-tenant"])
def test_reindex_missing_or_cross_tenant_document_is_not_found(
    offline_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
) -> None:
    """缺失与跨租户统一 20016，不泄露存在性；既不抢占也不投递。"""

    doc = None if target is None else _document(status=DocumentStatus.FAILED, tenant_id=target)
    _install_lookup(monkeypatch, doc)

    response = offline_api.client.post(f"/api/v1/documents/{DOC_ID}/reindex")

    body = response.json()
    assert body["code"] == ErrorCode.NOT_FOUND
    assert body["msg"] == "not found"
    assert body["data"] is None
    assert offline_api.prepares == []
    assert offline_api.enqueued == []
    assert offline_api.session.commits == 0


# ─────────────────────────────────────────────────────────────────── 删除文档端点


@pytest.mark.parametrize(
    ("status", "claim_age_seconds"),
    [
        (DocumentStatus.FAILED, None),
        (DocumentStatus.SUCCESS, None),
        (DocumentStatus.PROCESSING, document_repository.DOCUMENT_LEASE_SECONDS + 1),
    ],
    ids=["failed", "success", "expired-lease"],
)
def test_delete_allowed_document_purges_file_and_row_in_order(
    offline_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    claim_age_seconds: int | None,
) -> None:
    """失败 / 成功 / 租约过期都能删：purge → 删文件 → 删行 → 提交，一步不少。"""

    source = tmp_path / f"{DOC_ID}.md"
    source.write_text("# body", encoding="utf-8")
    claimed_at = None if claim_age_seconds is None else now() - timedelta(seconds=claim_age_seconds)
    doc = _document(status=status, claimed_at=claimed_at, source_file_path=str(source))
    _install_lookup(monkeypatch, doc)

    response = offline_api.client.delete(f"/api/v1/documents/{DOC_ID}")

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {"document_id": DOC_ID, "deleted": True}
    assert offline_api.calls == ["purge:pgvector", "remove-file", "delete-row", "commit"]
    assert offline_api.store.deleted == [DOC_ID], "切片的清除走存储列表，不直连某个存储"
    assert offline_api.store.tenant_ids == [TENANT_ID], "删除切片也必须强制带租户"
    assert not source.exists(), "原文件必须删掉"
    assert offline_api.session.deleted == [doc]
    assert offline_api.session.commits == 1


def test_delete_document_without_source_file_still_removes_the_row(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON 模式（无落盘文件）删除时文件步骤是 no-op，其余步骤照旧。"""

    doc = _document(status=DocumentStatus.SUCCESS)
    _install_lookup(monkeypatch, doc)

    response = offline_api.client.delete(f"/api/v1/documents/{DOC_ID}")

    body = response.json()
    assert body["code"] == 0
    assert offline_api.calls == ["purge:pgvector", "remove-file", "delete-row", "commit"]
    assert offline_api.store.deleted == [DOC_ID]
    assert offline_api.store.tenant_ids == [TENANT_ID]
    assert offline_api.session.deleted == [doc]


def test_delete_live_processing_document_is_rejected(
    offline_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有效租约内的处理中拒绝删除（20025）：不清切片、不删文件、不动行。"""

    source = tmp_path / f"{DOC_ID}.md"
    source.write_text("# body", encoding="utf-8")
    doc = _document(
        status=DocumentStatus.PROCESSING, claimed_at=now(), source_file_path=str(source)
    )
    _install_lookup(monkeypatch, doc)

    response = offline_api.client.delete(f"/api/v1/documents/{DOC_ID}")

    body = response.json()
    assert body["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert body["msg"] == "document is processing"
    assert offline_api.calls == [], "守卫拒绝在第一步，任何清理都不发生"
    assert source.exists()
    assert offline_api.store.deleted == []
    assert offline_api.session.deleted == []
    assert offline_api.session.commits == 0


@pytest.mark.parametrize("target", [None, "tenant-other"], ids=["missing", "cross-tenant"])
def test_delete_missing_or_cross_tenant_document_is_not_found(
    offline_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str | None,
) -> None:
    """缺失与跨租户统一 20016：连原文件都不能碰。"""

    source = tmp_path / f"{DOC_ID}.md"
    source.write_text("# body", encoding="utf-8")
    doc = (
        None
        if target is None
        else _document(status=DocumentStatus.FAILED, tenant_id=target, source_file_path=str(source))
    )
    _install_lookup(monkeypatch, doc)

    response = offline_api.client.delete(f"/api/v1/documents/{DOC_ID}")

    body = response.json()
    assert body["code"] == ErrorCode.NOT_FOUND
    assert body["msg"] == "not found"
    assert body["data"] is None
    assert offline_api.calls == []
    assert source.exists()
    assert offline_api.session.deleted == []
    assert offline_api.session.commits == 0


# ─────────────────────────────────────────────────────────── 运维脚本（加载与替身）


def _load_script(module_name: str) -> ModuleType:
    """从文件加载脚本模块：``scripts*`` 不在安装包里，显式路径最可靠。

    加载本身也要成功——脚本顶层不得连接数据库、不得解析 argv（导入即安全）。
    """

    path = _SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t5_script_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules：dataclass 处理 ``from __future__ import annotations``
    # 的字符串注解时，要按 ``cls.__module__`` 反查模块。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture
def reindex_script() -> ModuleType:
    return _load_script("reindex_knowledge_base")


def _script_documents(*document_ids: str) -> list[SimpleNamespace]:
    """脚本扫描结果的替身：行自带 tenant_id / kb_id，投递时按行口径。"""

    return [
        SimpleNamespace(id=document_id, tenant_id=TENANT_ID, kb_id=KB_ID)
        for document_id in document_ids
    ]


def _patch_reindex_listing(
    monkeypatch: pytest.MonkeyPatch, reindex_script: ModuleType, documents: list[SimpleNamespace]
) -> list[tuple[str, str, int]]:
    listed: list[tuple[str, str, int]] = []

    def _list(_db: Any, kb_id: str, *, tenant_id: str, status: int) -> list[SimpleNamespace]:
        listed.append((kb_id, tenant_id, status))
        return documents

    monkeypatch.setattr(reindex_script.document_repository, "list_by_kb_and_status", _list)
    return listed


# ──────────────────────────────────────────────────────────────── 全库重建脚本


def test_reindex_script_dry_run_counts_candidates_without_claim_or_dispatch(
    reindex_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` 只统计：不抢占、不投递、不提交。"""

    documents = _script_documents("doc-a", "doc-b")
    listed = _patch_reindex_listing(monkeypatch, reindex_script, documents)
    dispatched: list[dict[str, Any]] = []
    claims: list[str] = []
    monkeypatch.setattr(
        reindex_script.document_repository,
        "prepare_for_indexing",
        lambda _db, document_id, **_kwargs: claims.append(document_id) or True,
    )
    monkeypatch.setattr(
        reindex_script, "enqueue_document", lambda **kwargs: dispatched.append(kwargs)
    )

    session = _FakeSession()
    stats = reindex_script.reindex_documents(
        session, kb_id=KB_ID, tenant_id=TENANT_ID, dry_run=True
    )

    assert stats == reindex_script.ReindexStats(considered=2, submitted=0, skipped=0, failed=0)
    assert claims == []
    assert dispatched == []
    assert session.commits == 0
    assert listed == [(KB_ID, TENANT_ID, DocumentStatus.SUCCESS)], (
        "脚本只重建 SUCCESS 文档，且查询强制带租户"
    )


def test_reindex_script_counts_submitted_skipped_and_failed(
    reindex_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐篇准备：成功即提交并投递，准备不到记 skipped，投递失败记 failed。"""

    documents = _script_documents("doc-ok", "doc-lost-race", "doc-enqueue-boom")
    _patch_reindex_listing(monkeypatch, reindex_script, documents)
    claims: list[str] = []
    dispatched: list[dict[str, Any]] = []

    def _claim(_db: Any, document_id: str, **_kwargs: Any) -> bool:
        claims.append(document_id)
        return document_id != "doc-lost-race"

    def _enqueue(**kwargs: Any) -> None:
        if kwargs["document_id"] == "doc-enqueue-boom":
            raise RuntimeError("broker down")
        dispatched.append(kwargs)

    monkeypatch.setattr(reindex_script.document_repository, "prepare_for_indexing", _claim)
    monkeypatch.setattr(reindex_script, "enqueue_document", _enqueue)

    session = _FakeSession()
    stats = reindex_script.reindex_documents(session, kb_id=KB_ID, tenant_id=TENANT_ID)

    assert stats == reindex_script.ReindexStats(considered=3, submitted=1, skipped=1, failed=1)
    assert claims == ["doc-ok", "doc-lost-race", "doc-enqueue-boom"]
    assert dispatched == [
        {
            "document_id": "doc-ok",
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": None,
            "reparse": False,
        }
    ]
    assert session.commits == 2, "抢到的两篇都先提交再投递，抢不到的不提交"


def test_reindex_script_limit_caps_candidates(
    reindex_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--limit`` 只处理前 N 篇，调试用的收敛口。"""

    documents = _script_documents("doc-a", "doc-b", "doc-c")
    _patch_reindex_listing(monkeypatch, reindex_script, documents)
    monkeypatch.setattr(
        reindex_script.document_repository,
        "prepare_for_indexing",
        lambda _db, _document_id, **_kwargs: True,
    )
    monkeypatch.setattr(reindex_script, "enqueue_document", lambda **_kwargs: None)

    session = _FakeSession()
    stats = reindex_script.reindex_documents(session, kb_id=KB_ID, tenant_id=TENANT_ID, limit=2)

    assert stats == reindex_script.ReindexStats(considered=2, submitted=2, skipped=0, failed=0)
    assert session.commits == 2


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (dict(considered=2, submitted=1, skipped=0, failed=1), 0),
        (dict(considered=1, submitted=0, skipped=0, failed=1), 1),
        (dict(considered=2, submitted=0, skipped=2, failed=0), 0),
        (dict(considered=0), 0),
        (dict(considered=2, submitted=0, skipped=1, failed=1), 1),
    ],
)
def test_reindex_script_exit_code_rule(
    reindex_script: ModuleType, stats: dict[str, int], expected: int
) -> None:
    """只有「一个都没投递成功且有失败」才退出 1，其余一律 0。"""

    assert reindex_script.exit_code_for(reindex_script.ReindexStats(**stats)) == expected


def test_reindex_script_resolves_tenant_from_the_knowledge_base(
    reindex_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--tenant-id`` 缺省时取该 kb 的 tenant_id；显式值优先；kb 不存在则报错。"""

    # 归属发现走仓储层里显式命名的运维函数（它只回租户标识，不回库行）；
    # 数据面上的「按 id 取库」已经不存在，必须带租户。
    monkeypatch.setattr(
        reindex_script.knowledge_base_repository,
        "get_owner_tenant_id",
        lambda _db, _kb_id: TENANT_ID,
    )

    assert (
        reindex_script.resolve_tenant_id(SimpleNamespace(), kb_id=KB_ID, tenant_id=None)
        == TENANT_ID
    )
    assert (
        reindex_script.resolve_tenant_id(
            SimpleNamespace(), kb_id=KB_ID, tenant_id="tenant-explicit"
        )
        == "tenant-explicit"
    )

    monkeypatch.setattr(
        reindex_script.knowledge_base_repository, "get_owner_tenant_id", lambda _db, _kb_id: None
    )
    with pytest.raises(LookupError):
        reindex_script.resolve_tenant_id(SimpleNamespace(), kb_id=KB_ID, tenant_id=None)


def test_reindex_script_main_prints_stats_and_submitted_not_completed_note(
    reindex_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 入口打印三行统计，并明确「已投递 ≠ 已完成」。"""

    session = _FakeSession()
    monkeypatch.setattr(reindex_script, "SessionLocal", lambda: session)
    monkeypatch.setattr(reindex_script, "setup_logging", lambda: None)
    monkeypatch.setattr(
        reindex_script,
        "resolve_tenant_id",
        lambda _db, *, kb_id, tenant_id: TENANT_ID,
    )
    monkeypatch.setattr(
        reindex_script,
        "reindex_documents",
        lambda _db, **_kwargs: reindex_script.ReindexStats(
            considered=2, submitted=1, skipped=1, failed=0
        ),
    )

    exit_code = reindex_script.main(["--kb-id", KB_ID])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "submitted: 1" in out
    assert "skipped: 1" in out
    assert "failed: 0" in out
    assert reindex_script.SUBMITTED_NOT_COMPLETED_NOTE in out
    assert "GET /documents" in out, "提示里要给出确认终态的手段"
    assert session.closed


# ──────────────────────────────────────────────────────────────── 卡死恢复脚本


@pytest.fixture
def requeue_script() -> ModuleType:
    return _load_script("requeue_stuck_documents")


def _patch_requeue_listing(
    monkeypatch: pytest.MonkeyPatch, requeue_script: ModuleType, documents: list[SimpleNamespace]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _list(
        _db: Any,
        *,
        tenant_id: str | None = None,
        lease_seconds: int = document_repository.DOCUMENT_LEASE_SECONDS,
        kb_id: str | None = None,
    ) -> list[SimpleNamespace]:
        calls.append({"tenant_id": tenant_id, "lease_seconds": lease_seconds, "kb_id": kb_id})
        return documents

    monkeypatch.setattr(requeue_script.document_repository, "list_stuck", _list)
    return calls


def test_requeue_script_dry_run_counts_candidates_without_claim_or_dispatch(
    requeue_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` 只统计：不抢占、不投递、不提交。"""

    documents = _script_documents("doc-stuck-a", "doc-stuck-b")
    listed = _patch_requeue_listing(monkeypatch, requeue_script, documents)
    dispatched: list[dict[str, Any]] = []
    claims: list[str] = []
    monkeypatch.setattr(
        requeue_script.document_repository,
        "prepare_for_indexing",
        lambda _db, document_id, **_kwargs: claims.append(document_id) or True,
    )
    monkeypatch.setattr(
        requeue_script, "enqueue_document", lambda **kwargs: dispatched.append(kwargs)
    )

    session = _FakeSession()
    stats = requeue_script.requeue_documents(session, dry_run=True)

    assert stats == requeue_script.RequeueStats(considered=2, requeued=0, skipped=0, failed=0)
    assert claims == []
    assert dispatched == []
    assert session.commits == 0
    assert listed == [
        {
            "tenant_id": None,
            "lease_seconds": document_repository.DOCUMENT_LEASE_SECONDS,
            "kb_id": None,
        }
    ], "缺省扫全库；租约默认与 DOCUMENT_LEASE_SECONDS 一致"


def test_requeue_script_counts_requeued_skipped_and_failed(
    requeue_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐篇重新准备：成功即提交并投递，准备不到记 skipped，投递失败记 failed。"""

    documents = _script_documents("doc-ok", "doc-lost-race", "doc-enqueue-boom")
    _patch_requeue_listing(monkeypatch, requeue_script, documents)
    dispatched: list[dict[str, Any]] = []

    def _claim(_db: Any, document_id: str, **_kwargs: Any) -> bool:
        return document_id != "doc-lost-race"

    def _enqueue(**kwargs: Any) -> None:
        if kwargs["document_id"] == "doc-enqueue-boom":
            raise RuntimeError("broker down")
        dispatched.append(kwargs)

    monkeypatch.setattr(requeue_script.document_repository, "prepare_for_indexing", _claim)
    monkeypatch.setattr(requeue_script, "enqueue_document", _enqueue)

    session = _FakeSession()
    stats = requeue_script.requeue_documents(session, kb_id=KB_ID, lease_seconds=1234)

    assert stats == requeue_script.RequeueStats(considered=3, requeued=1, skipped=1, failed=1)
    assert dispatched == [
        {
            "document_id": "doc-ok",
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": None,
            "reparse": False,
        }
    ], "卡死扫描默认跨租户，投递时用行自带的 tenant_id / kb_id"
    assert session.commits == 2


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (dict(considered=2, requeued=1, skipped=0, failed=1), 0),
        (dict(considered=1, requeued=0, skipped=0, failed=1), 1),
        (dict(considered=2, requeued=0, skipped=2, failed=0), 0),
        (dict(considered=0), 0),
    ],
)
def test_requeue_script_exit_code_rule(
    requeue_script: ModuleType, stats: dict[str, int], expected: int
) -> None:
    """只有「一份都没救回且有失败」才退出 1，其余一律 0。"""

    assert requeue_script.exit_code_for(requeue_script.RequeueStats(**stats)) == expected


def test_requeue_script_main_prints_stats_and_requeued_not_completed_note(
    requeue_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 入口打印统计，并明确「已重新投递 ≠ 已完成」。"""

    session = _FakeSession()
    monkeypatch.setattr(requeue_script, "SessionLocal", lambda: session)
    monkeypatch.setattr(requeue_script, "setup_logging", lambda: None)
    monkeypatch.setattr(
        requeue_script,
        "requeue_documents",
        lambda _db, **_kwargs: requeue_script.RequeueStats(
            considered=2, requeued=1, skipped=1, failed=0
        ),
    )

    exit_code = requeue_script.main(["--kb-id", KB_ID, "--lease-seconds", "600"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "requeued: 1" in out
    assert "skipped: 1" in out
    assert "failed: 0" in out
    assert requeue_script.REQUEUED_NOT_COMPLETED_NOTE in out
    assert "GET /documents" in out
    assert session.closed
