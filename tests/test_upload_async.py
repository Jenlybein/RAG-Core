"""T3 · 异步上传 + 状态守卫矩阵：ASGI 边界与纯函数接缝。

上传的唯一接缝是 ASGI 应用边界：TestClient 发真实请求，只观察统一响应体、落盘文件
与内存会话里留下的行。投递缝 ``document_service.enqueue_document`` 用 monkeypatch
换成记录器，因此不需要 Broker；知识库仓储、ID 生成、文档 / 切片仓储用 monkeypatch
换掉，数据库用框架依赖覆盖换成内存替身——同一条接缝离线，不连 PostgreSQL、不发真实
模型请求。

状态守卫是纯函数，直接在 ``app.services.document_state`` 的公开函数上验证矩阵。
抢占 CAS 用内存 SQLite 只建 ``documents`` 一张表，验证 ``UPDATE ... WHERE`` 的四条
分支（失败 / 已入队未开工 / 已被抢占 / 租约过期 / 成功 + force），不连 PostgreSQL。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.models.document import Document
from app.repositories import chunk_repository, document_repository
from app.services import document_state
from app.utils.status import DocumentStatus

KB_ID = "kb-1"
TENANT_ID = settings.DEV_TENANT_ID
CONTENT_SENTINEL = "T3-CONTENT-SENTINEL"
RAW_INPUT_SENTINEL = "T3-RAW-INPUT-SENTINEL"
FILE_BODY = b"# Notes\n\nbody"
EXPECTED_UPLOAD_DATA = {
    "document_id": "doc-1",
    "kb_id": KB_ID,
    "status": DocumentStatus.PROCESSING,
    "chunk_count": 0,
    "error_message": None,
}


class _FakeSession:
    """最小会话替身：文档行留在内存里，不碰数据库，只记录提交次数。"""

    def __init__(self) -> None:
        self.documents: dict[str, Any] = {}
        self.commit_count = 0

    def add(self, doc: Any) -> None:
        self.documents[doc.id] = doc

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """离线上传接缝：返回 client / session / 投递记录 / 落盘目录。"""

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    document_service = importlib.import_module("app.services.document_service")
    from app.core.auth import DevKeyContext, resolve_dev_key

    session = _FakeSession()
    enqueued: list[dict[str, Any]] = []
    uploads_dir = tmp_path / "uploads"

    monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(uploads_dir))
    monkeypatch.setattr(
        kb_repo, "get_by_id", lambda db, kb_id: SimpleNamespace(tenant_id=TENANT_ID)
    )
    monkeypatch.setattr(document_service, "new_document_id", lambda: "doc-1")
    monkeypatch.setattr(
        document_service, "enqueue_document", lambda **kwargs: enqueued.append(kwargs)
    )

    main.app.dependency_overrides[session_mod.get_db] = lambda: session
    main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client, session=session, enqueued=enqueued, uploads_dir=uploads_dir
            )
    finally:
        main.app.dependency_overrides.clear()


# ── 上传：JSON 模式 ──


def test_json_upload_returns_processing_and_only_enqueues(offline: SimpleNamespace) -> None:
    """JSON 模式：直接落正文、status=3、chunk_count=0，请求线程只登记 + 投递。"""

    response = offline.client.post(
        "/api/v1/documents/upload",
        json={"kb_id": KB_ID, "title": "demo", "content": CONTENT_SENTINEL},
        headers={"X-Request-ID": "t3-json-req"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert body["data"] == EXPECTED_UPLOAD_DATA

    doc = offline.session.documents["doc-1"]
    assert doc.status == DocumentStatus.PROCESSING
    assert doc.content == CONTENT_SENTINEL
    assert doc.source_type == "text"
    assert doc.source_file_path is None
    assert offline.session.commit_count == 1, "请求线程只提交登记，不写索引结果"
    assert offline.enqueued == [
        {
            "document_id": "doc-1",
            "tenant_id": TENANT_ID,
            "kb_id": KB_ID,
            "request_id": "t3-json-req",
            "reparse": False,
        }
    ], "登记完成后必须把索引任务投递出去，且带上请求关联标识"


# ── 上传：文件模式 ──


def test_multipart_upload_stores_file_and_leaves_content_empty(offline: SimpleNamespace) -> None:
    """文件模式：按服务端路径落盘、content 留空、原始文件名只进 source_filename。"""

    response = offline.client.post(
        "/api/v1/documents/upload",
        data={"kb_id": KB_ID},
        files={"file": ("Notes.MD", FILE_BODY, "text/markdown")},
        headers={"X-Request-ID": "t3-file-req"},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"] == EXPECTED_UPLOAD_DATA

    stored_path = offline.uploads_dir / TENANT_ID / "doc-1.md"
    assert stored_path.read_bytes() == FILE_BODY, (
        "扩展名统一小写后落在 {storage}/{tenant}/{id}{ext}"
    )

    doc = offline.session.documents["doc-1"]
    assert doc.content == "", "文件模式的正文留给 Worker 解析，不在请求线程读"
    assert doc.source_filename == "Notes.MD", "原始文件名只进列"
    assert doc.source_file_path == str(stored_path)
    assert doc.source_type == "markdown"
    assert doc.title == "Notes", "title 缺省时用文件名主干"
    assert offline.enqueued[0]["request_id"] == "t3-file-req"


def test_unknown_extension_is_rejected_before_anything_is_persisted(
    offline: SimpleNamespace,
) -> None:
    """白名单外的扩展名复用 20023，不落盘、不建行、不投递。"""

    response = offline.client.post(
        "/api/v1/documents/upload",
        data={"kb_id": KB_ID},
        files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
    )

    body = response.json()
    assert body["code"] == ErrorCode.DOCUMENT_PARSE_FAILED
    assert body["msg"] == "document parse failed"
    assert offline.session.documents == {}
    assert offline.enqueued == []


def test_oversize_file_is_rejected_with_file_too_large(
    offline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 settings.DOCUMENT_MAX_SIZE_MB 返回 20026，且不落盘。"""

    monkeypatch.setattr(settings, "DOCUMENT_MAX_SIZE_MB", 1)

    response = offline.client.post(
        "/api/v1/documents/upload",
        data={"kb_id": KB_ID},
        files={"file": ("big.md", b"x" * (1024 * 1024 + 1), "text/markdown")},
    )

    body = response.json()
    assert body["code"] == ErrorCode.FILE_TOO_LARGE
    assert body["msg"] == "file too large"
    assert not offline.uploads_dir.exists(), "超限文件不落盘"
    assert offline.session.documents == {}


def test_multipart_without_kb_id_returns_validation_error(offline: SimpleNamespace) -> None:
    """multipart 分支缺 kb_id 仍走 20002，而不是把空串带进业务层。"""

    response = offline.client.post(
        "/api/v1/documents/upload",
        files={"file": ("notes.md", FILE_BODY, "text/markdown")},
    )

    body = response.json()
    assert body["code"] == ErrorCode.REQUEST_VALIDATION_ERROR
    errors = body["data"]["errors"]
    assert errors
    assert any("kb_id" in error["loc"] for error in errors)
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}
    assert offline.session.documents == {}


# ── 上传：JSON 校验失败 ──


def test_json_validation_error_keeps_shape_and_does_not_echo_input(
    offline: SimpleNamespace,
) -> None:
    response = offline.client.post(
        "/api/v1/documents/upload",
        json={"kb_id": KB_ID, "content": RAW_INPUT_SENTINEL},
    )

    body = response.json()
    assert body["code"] == ErrorCode.REQUEST_VALIDATION_ERROR
    errors = body["data"]["errors"]
    assert errors
    assert any("title" in error["loc"] for error in errors)
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}
    assert RAW_INPUT_SENTINEL not in response.text, "校验失败不得回显原始输入"


def test_invalid_json_body_returns_validation_error_instead_of_server_error(
    offline: SimpleNamespace,
) -> None:
    """半截 JSON 归入 20002，而不是落到未捕获异常的 500。"""

    response = offline.client.post(
        "/api/v1/documents/upload",
        content=b'{"kb_id": "kb-1", "title": ',
        headers={"content-type": "application/json"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == ErrorCode.REQUEST_VALIDATION_ERROR
    assert offline.session.documents == {}


# ── 状态查询 ──


def test_list_documents_returns_status_and_grouped_chunk_counts(
    offline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列表接口一次分组计数覆盖全部文档，不按文档逐条查询。"""

    rows = [
        SimpleNamespace(
            id="doc-a",
            title="A",
            status=DocumentStatus.SUCCESS,
            error_message=None,
            source_type="markdown",
            created_at=datetime(2026, 1, 1, 1, 1, 1),
        ),
        SimpleNamespace(
            id="doc-b",
            title="B",
            status=DocumentStatus.FAILED,
            error_message="document indexing failed, please retry or contact support",
            source_type="text",
            created_at=datetime(2026, 1, 2, 2, 2, 2),
        ),
        SimpleNamespace(
            id="doc-c",
            title="C",
            status=DocumentStatus.PROCESSING,
            error_message=None,
            source_type="text",
            created_at=datetime(2026, 1, 3, 3, 3, 3),
        ),
    ]
    monkeypatch.setattr(
        document_repository,
        "list_by_kb",
        lambda db, kb_id, *, tenant_id: rows,
    )

    counter_calls: list[list[str]] = []

    def fake_counts(db: Any, document_ids: list[str], *, tenant_id: str) -> dict[str, int]:
        assert tenant_id == TENANT_ID, "切片计数必须强制带租户（ADR-0006）"
        counter_calls.append(list(document_ids))
        return {"doc-a": 5, "doc-b": 2}

    monkeypatch.setattr(chunk_repository, "count_by_document_ids", fake_counts)

    response = offline.client.get("/api/v1/documents", params={"kb_id": KB_ID})

    body = response.json()
    assert body["code"] == 0
    items = body["data"]["items"]
    assert [item["document_id"] for item in items] == ["doc-a", "doc-b", "doc-c"]
    assert [item["status"] for item in items] == [
        DocumentStatus.SUCCESS,
        DocumentStatus.FAILED,
        DocumentStatus.PROCESSING,
    ]
    assert [item["chunk_count"] for item in items] == [5, 2, 0]
    assert items[1]["error_message"] == "document indexing failed, please retry or contact support"
    assert set(items[0]) == {
        "document_id",
        "title",
        "status",
        "chunk_count",
        "error_message",
        "source_type",
        "created_at",
    }
    assert counter_calls == [["doc-a", "doc-b", "doc-c"]], "一次分组查询覆盖全部文档，别 N+1"


def test_list_documents_hides_knowledge_base_of_another_tenant(
    offline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨租户的 kb_id 一律按「不存在」处理，连列表查询都不发。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    monkeypatch.setattr(
        kb_repo, "get_by_id", lambda db, kb_id: SimpleNamespace(tenant_id="tenant-other")
    )

    list_calls: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        document_repository,
        "list_by_kb",
        lambda *args, **kwargs: list_calls.append((args, kwargs)),
    )

    response = offline.client.get("/api/v1/documents", params={"kb_id": KB_ID})

    body = response.json()
    assert body["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert body["msg"] == "knowledge base not found"
    assert body["data"] is None
    assert list_calls == [], "跨租户一律按不存在处理，绝不能先查后过滤"


# ── 状态守卫矩阵（纯函数）──


def _document(status: int, claimed_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(status=status, index_claimed_at=claimed_at)


def test_effective_processing_blocks_delete_and_reindex_even_with_force() -> None:
    doc = _document(DocumentStatus.PROCESSING, now())

    assert document_state.is_effectively_processing(doc) is True
    with pytest.raises(AppError) as delete_error:
        document_state.ensure_deletable(doc)
    assert delete_error.value.code is ErrorCode.DOCUMENT_IS_PROCESSING
    with pytest.raises(AppError) as reindex_error:
        document_state.ensure_reindexable(doc, force=True)
    assert reindex_error.value.code is ErrorCode.DOCUMENT_IS_PROCESSING


def test_failed_document_is_deletable_and_reindexable_without_force() -> None:
    doc = _document(DocumentStatus.FAILED, None)

    assert document_state.is_effectively_processing(doc) is False
    document_state.ensure_deletable(doc)
    document_state.ensure_reindexable(doc, force=False)


def test_success_document_requires_force_to_reindex() -> None:
    doc = _document(DocumentStatus.SUCCESS, None)

    document_state.ensure_deletable(doc)
    with pytest.raises(AppError) as error:
        document_state.ensure_reindexable(doc, force=False)
    assert error.value.code is ErrorCode.REINDEX_REQUIRES_FORCE
    document_state.ensure_reindexable(doc, force=True)


def test_expired_lease_counts_as_stuck_and_is_allowed() -> None:
    stale = _document(
        DocumentStatus.PROCESSING,
        now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1),
    )

    assert document_state.is_effectively_processing(stale) is False
    document_state.ensure_deletable(stale)
    document_state.ensure_reindexable(stale, force=False)


def test_enqueued_but_never_claimed_document_is_not_effectively_processing() -> None:
    """status=3 但没有抢占时间戳 = 已入队未开工，没有人真的在跑。"""

    queued = _document(DocumentStatus.PROCESSING, None)

    assert document_state.is_effectively_processing(queued) is False
    document_state.ensure_deletable(queued)
    document_state.ensure_reindexable(queued, force=False)


def test_lease_window_is_configurable() -> None:
    doc = _document(DocumentStatus.PROCESSING, now() - timedelta(seconds=30))

    assert document_state.is_effectively_processing(doc, lease_seconds=60) is True
    assert document_state.is_effectively_processing(doc, lease_seconds=10) is False


# ── 抢占 CAS（内存 SQLite，只建 documents 表）──


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
    claimed_at: datetime | None = None,
    error_message: str | None = None,
    tenant_id: str = TENANT_ID,
    created_at: datetime | None = None,
) -> None:
    db.add(
        Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=KB_ID,
            title=document_id,
            content="",
            source_type="text",
            status=status,
            error_message=error_message,
            index_claimed_at=claimed_at,
            created_at=created_at or now(),
        )
    )
    db.commit()


def _claim(
    db: Session,
    document_id: str,
    *,
    allow_success: bool = False,
    lease_seconds: int = document_repository.DOCUMENT_LEASE_SECONDS,
) -> bool:
    claimed = document_repository.try_claim_for_indexing(
        db,
        document_id,
        tenant_id=TENANT_ID,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
    )
    db.commit()
    return claimed


def test_claim_takes_failed_document_and_clears_error(document_db: Session) -> None:
    _seed_document(document_db, "doc-failed", status=DocumentStatus.FAILED, error_message="boom")

    assert _claim(document_db, "doc-failed") is True

    row = document_db.get(Document, "doc-failed")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING
    assert row.error_message is None
    assert row.index_claimed_at is not None


def test_claim_takes_enqueued_document_never_started(document_db: Session) -> None:
    _seed_document(document_db, "doc-queued", status=DocumentStatus.PROCESSING, claimed_at=None)

    assert _claim(document_db, "doc-queued") is True

    row = document_db.get(Document, "doc-queued")
    assert row is not None
    assert row.index_claimed_at is not None


def test_fresh_claim_blocks_a_second_worker(document_db: Session) -> None:
    _seed_document(document_db, "doc-running", status=DocumentStatus.PROCESSING, claimed_at=now())

    assert _claim(document_db, "doc-running") is False

    row = document_db.get(Document, "doc-running")
    assert row is not None
    assert row.status == DocumentStatus.PROCESSING


def test_expired_lease_can_be_reclaimed(document_db: Session) -> None:
    stale = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS + 1)
    _seed_document(document_db, "doc-stuck", status=DocumentStatus.PROCESSING, claimed_at=stale)

    assert _claim(document_db, "doc-stuck") is True


def test_success_document_is_only_claimable_with_allow_success(document_db: Session) -> None:
    _seed_document(document_db, "doc-done", status=DocumentStatus.SUCCESS)

    assert _claim(document_db, "doc-done", allow_success=False) is False
    assert _claim(document_db, "doc-done", allow_success=True) is True


def test_claim_of_missing_document_is_false(document_db: Session) -> None:
    assert _claim(document_db, "doc-missing") is False


def test_list_by_kb_excludes_other_tenants_and_orders_by_created_at(
    document_db: Session,
) -> None:
    """同一 kb_id 下其他租户的行必须被过滤掉；本租户的行按 created_at 升序。"""

    _seed_document(
        document_db,
        "doc-mine-late",
        status=DocumentStatus.SUCCESS,
        created_at=datetime(2026, 1, 2, 1, 1, 1),
    )
    _seed_document(
        document_db,
        "doc-mine-early",
        status=DocumentStatus.PROCESSING,
        created_at=datetime(2026, 1, 1, 1, 1, 1),
    )
    _seed_document(
        document_db,
        "doc-other-tenant",
        status=DocumentStatus.SUCCESS,
        tenant_id="tenant-other",
        created_at=datetime(2026, 1, 3, 1, 1, 1),
    )

    rows = document_repository.list_by_kb(document_db, KB_ID, tenant_id=TENANT_ID)

    assert [row.id for row in rows] == ["doc-mine-early", "doc-mine-late"]
