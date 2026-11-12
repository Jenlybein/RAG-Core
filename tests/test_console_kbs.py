"""票 #90：控制面「管库管文档」的 ASGI 边界验收。

接缝是一条：真实应用 + ``TestClient``，用真实的 ``POST /console/login`` 拿到会话 cookie，
之后只看统一响应体、审计行与内存会话里留下的行。**不另造一套登录流程**——被验的是
「面板身份能建库 / 传文档 / 重建 / 删文档」，登录只是到达那个身份的方式。

存储侧换成内存替身（``RecordingSession`` 的子类）与仓储级替身，理由与
``test_console_api.py`` 相同：离线套件不连 PostgreSQL、不连 Broker、不发模型请求。
这里刻意保留真实的 ``document_service.upload``、``document_state`` 守卫、审计写法与
``knowledge_base_service.create_kb``，于是测到的是「同一个租户、同一套配额、同一份
错误码」这些行为，而不是某个函数有没有被调用。

管理员（``tenant_id IS NULL``）是这套接口的一个**明确的外部状态**：它能登录、能看
``/console/me``，但用户面板类的库与文档接口一律 ``20011``。这不是「没登录」，也不能
被前端当成掉线清 cookie——所以单独成组断言。
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.db.base import now
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from app.repositories import document_repository
from app.utils.status import DocumentStatus
from tests.fake_db import RecordingSession

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"
ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

KB_ALPHA_ONE = "kb-a1"
KB_ALPHA_EMPTY = "kb-a2"
KB_BETA_ONE = "kb-b1"

DOC_ALPHA_DONE = "doc-a1"
DOC_ALPHA_FAILED = "doc-a2"
DOC_BETA_DONE = "doc-b1"

TREE_PATH = "/console/knowledge-bases/tree"
CREATE_PATH = "/console/knowledge-bases"
UPLOAD_PATH = "/console/knowledge-bases/{kb_id}/documents"
REINDEX_PATH = "/console/documents/{document_id}/reindex"
DELETE_DOC_PATH = "/console/documents/{document_id}"
READ_CONTENT_PATH = "/console/documents/{document_id}/content"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}
CONTENT_SENTINEL = "CONSOLE-UPLOAD-CONTENT-SENTINEL"

#: 切片计数替身给出的分组结果；未列出的文档按 0 计。
CHUNK_COUNTS = {DOC_ALPHA_DONE: 4, DOC_ALPHA_FAILED: 2, DOC_BETA_DONE: 1}


class _MemoryPlanCache:
    """内存假缓存：套餐解析只用到 ``get/set/delete`` 三个方法。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}
        self.set_calls: list[int] = []
        self.deleted: list[int] = []

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.set_calls.append(plan_id)
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.deleted.append(plan_id)
        self.items.pop(plan_id, None)


class _ConsoleSession(RecordingSession):
    """``RecordingSession`` + 两件真实会话会做的事。

    1. 记录 add / delete / commit 的顺序——「purge → 删文件 → 删行 → 提交」是票面
       要求保持不变的清除顺序，顺序错了这里就能看见；
    2. 模拟 ``flush`` 时给 ``created_at`` 填默认值。真实落库时列的 default 在 INSERT
       时求值；假会话的 ``add`` 不落库，不补这一下的话建库响应会因为 ``created_at
       = None`` 通不过响应模型。
    """

    def __init__(self, *, rows: dict[type, list[Any]], calls: list[str]) -> None:
        super().__init__(rows=rows)
        self.calls = calls

    def add(self, obj: Any) -> None:
        super().add(obj)
        if isinstance(obj, (KnowledgeBase, Document)) and getattr(obj, "created_at", None) is None:
            obj.created_at = now()
        self.calls.append(f"add:{type(obj).__name__}")

    def delete(self, obj: Any) -> None:
        self.calls.append("delete-row")
        super().delete(obj)
        # 预置的文档行是 ``SimpleNamespace``，键却是 ``Document``：``RecordingSession``
        # 按 ``type(obj)`` 查表会找不到它们，这里按对象身份兜底移除一次。
        for rows in self._rows.values():
            for index, row in enumerate(rows):
                if row is obj:
                    del rows[index]
                    return

    def commit(self) -> None:
        super().commit()
        self.calls.append("commit")


class _RecordingStore:
    """向量存储替身：只记录 purge，任何写入 / 检索都算越界。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.deleted: list[str] = []
        self.tenant_ids: list[str] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str) -> None:
        self.calls.append("purge")
        self.deleted.append(document_id)
        self.tenant_ids.append(tenant_id)

    async def add_chunks(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("删除 / 重建端点不写切片；写入只属于 Worker 的单事务")

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("删除 / 重建端点不做检索")


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: _ConsoleSession
    cache: _MemoryPlanCache
    store: _RecordingStore
    enqueued: list[dict[str, Any]]
    prepares: list[dict[str, Any]]
    removed: list[str | None]
    count_calls: list[list[str]]
    calls: list[str]
    uploads_dir: Path

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def documents_of(self, kb_id: str) -> list[Any]:
        return [doc for doc in self.db._rows.get(Document, []) if doc.kb_id == kb_id]

    def document(self, document_id: str) -> Any:
        return next(doc for doc in self.db._rows.get(Document, []) if doc.id == document_id)

    def knowledge_base(self, kb_id: str) -> KnowledgeBase:
        return next(kb for kb in self.db._rows.get(KnowledgeBase, []) if kb.id == kb_id)

    @staticmethod
    def session_id_of(response: Any) -> str:
        raw = response.headers["set-cookie"]
        head = raw.split(";", 1)[0]
        _name, value = head.split("=", 1)
        return value

    def use_cookie(self, session_id: str) -> None:
        self.client.cookies.set("console_session", session_id, path="/console")


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _account(
    account_id: str,
    username: str,
    password: str,
    *,
    role: str = "user",
    tenant_id: str | None = None,
) -> Any:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        status="active",
    )


def _kb(kb_id: str, tenant_id: str) -> KnowledgeBase:
    return KnowledgeBase(
        id=kb_id,
        tenant_id=tenant_id,
        name=f"name-{kb_id}",
        description=None,
        split_strategy="structure",
        allowed_formats=["markdown"],
        settings={"chunk_size": 512},
        created_at=datetime(2026, 1, 1, 9, 0, 0),
    )


def _doc(
    document_id: str,
    kb_id: str,
    tenant_id: str,
    *,
    status: int,
    title: str,
    created_at: datetime,
    error_message: str | None = None,
    index_claimed_at: datetime | None = None,
    source_file_path: str | None = None,
    content: str = "",
) -> Any:
    """文档行替身：字段形状与 ``app.models.document.Document`` 对齐。

    用 ``SimpleNamespace`` 而不是建 SQLite 表：``chunks`` 的 ``Vector(1536)`` 与
    ``JSONB`` 在 SQLite 上建不出来，而本票要验的是端点的**行为**（守卫、码、审计、
    形状），不是数据库本身——数据库行为已由 ``test_document_state_machine.py`` 覆盖。
    """

    return SimpleNamespace(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=title,
        content=content,
        source_type="markdown",
        source_file_path=source_file_path,
        source_filename=None,
        status=status,
        index_claimed_at=index_claimed_at,
        error_message=error_message,
        created_at=created_at,
    )


def _world() -> dict[type, list[Any]]:
    """一个够用的世界：两种角色、两个租户、三档套餐、三个库、三份文档。

    Alpha 的 ``kb-a1`` 有两份文档（一份成功、一份失败），``kb-a2`` 是空库——删空库
    成功与删非空库被拒两个分支因此都能在同一套世界下走一遍。Beta 的库与文档用于
    「跨租户按不存在」。
    """

    return {
        Plan: _plan_rows(),
        Tenant: [
            Tenant(id=TENANT_ALPHA, name="Alpha Corp", plan_id=3, status="active"),
            Tenant(id=TENANT_BETA, name="Beta Corp", plan_id=3, status="active"),
        ],
        Account: [
            _account(ADMIN_ID, "admin", ADMIN_PASSWORD, role="admin"),
            _account(ALICE_ID, "alice", USER_PASSWORD, tenant_id=TENANT_ALPHA),
            _account(BOB_ID, "bob", USER_PASSWORD, tenant_id=TENANT_BETA),
        ],
        KnowledgeBase: [
            _kb(KB_ALPHA_ONE, TENANT_ALPHA),
            _kb(KB_ALPHA_EMPTY, TENANT_ALPHA),
            _kb(KB_BETA_ONE, TENANT_BETA),
        ],
        Document: [
            _doc(
                DOC_ALPHA_DONE,
                KB_ALPHA_ONE,
                TENANT_ALPHA,
                status=DocumentStatus.SUCCESS,
                title="Alpha done",
                created_at=datetime(2026, 1, 1, 10, 0, 0),
            ),
            _doc(
                DOC_ALPHA_FAILED,
                KB_ALPHA_ONE,
                TENANT_ALPHA,
                status=DocumentStatus.FAILED,
                title="Alpha failed",
                created_at=datetime(2026, 1, 2, 10, 0, 0),
                error_message="document indexing failed, please retry or contact support",
            ),
            _doc(
                DOC_BETA_DONE,
                KB_BETA_ONE,
                TENANT_BETA,
                status=DocumentStatus.SUCCESS,
                title="Beta done",
                created_at=datetime(2026, 1, 3, 10, 0, 0),
            ),
        ],
    }


@pytest.fixture
def kbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """真实应用 + 真实登录 + 内存会话；仓储级只替换「另一张表怎么读」。

    被替换的都是仓储函数而不是业务函数：``document_state``、``document_service``、
    ``knowledge_base_service`` 与审计写法全部走真代码，于是错误码与顺序是从真实实现
    里测出来的。``prepare_for_indexing`` 也换成内存版本，因为它是一条 SQL ``UPDATE``；
    它自己的 CAS 语义由 ``test_upload_async.py`` 的内存 SQLite 用例守着。
    """

    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    console_deps = importlib.import_module("app.api.console_deps")
    kb_repository = importlib.import_module("app.repositories.knowledge_base_repository")
    chunk_repository = importlib.import_module("app.repositories.chunk_repository")
    document_service = importlib.import_module("app.services.document_service")
    console_kb_service = importlib.import_module("app.services.console_kb_service")
    document_storage = importlib.import_module("app.services.document_storage")

    calls: list[str] = []
    session = _ConsoleSession(rows=_world(), calls=calls)
    cache = _MemoryPlanCache()
    store = _RecordingStore(calls)
    enqueued: list[dict[str, Any]] = []
    prepares: list[dict[str, Any]] = []
    removed: list[str | None] = []
    count_calls: list[list[str]] = []
    uploads_dir = tmp_path / "uploads"

    def _kb_lookup(_db: Any, kb_id: str, *, tenant_id: str) -> Any | None:
        return next(
            (
                kb
                for kb in session._rows.get(KnowledgeBase, [])
                if kb.id == kb_id and kb.tenant_id == tenant_id
            ),
            None,
        )

    def _document_lookup(_db: Any, document_id: str, *, tenant_id: str) -> Any | None:
        return next(
            (
                doc
                for doc in session._rows.get(Document, [])
                if doc.id == document_id and doc.tenant_id == tenant_id
            ),
            None,
        )

    def _list_documents(_db: Any, kb_id: str, *, tenant_id: str) -> list[Any]:
        return [
            doc
            for doc in session._rows.get(Document, [])
            if doc.kb_id == kb_id and doc.tenant_id == tenant_id
        ]

    def _count_documents(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        return len(_list_documents(_db, kb_id, tenant_id=tenant_id))

    def _count_processing(_db: Any, *, tenant_id: str) -> int:
        lease = now() - timedelta(seconds=document_repository.DOCUMENT_LEASE_SECONDS)
        return sum(
            1
            for doc in session._rows.get(Document, [])
            if doc.tenant_id == tenant_id
            and doc.status == DocumentStatus.PROCESSING
            and (doc.index_claimed_at is None or doc.index_claimed_at >= lease)
        )

    def _prepare(
        _db: Any, document_id: str, *, tenant_id: str, allow_success: bool, **_kwargs: Any
    ) -> bool:
        prepares.append(
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "allow_success": allow_success,
            }
        )
        doc = _document_lookup(None, document_id, tenant_id=tenant_id)
        if doc is None:
            return False
        if doc.status == DocumentStatus.PROCESSING and doc.index_claimed_at is not None:
            return False
        if doc.status == DocumentStatus.SUCCESS and not allow_success:
            return False
        doc.status = DocumentStatus.PROCESSING
        doc.index_claimed_at = None
        doc.error_message = None
        return True

    def _count_chunks(_db: Any, document_ids: list[str], *, tenant_id: str) -> dict[str, int]:
        assert tenant_id == Tenants.alpha_or_beta(document_ids), "切片计数必须强制带租户"
        count_calls.append(list(document_ids))
        return {document_id: CHUNK_COUNTS.get(document_id, 0) for document_id in document_ids}

    def _enqueue(**kwargs: Any) -> None:
        enqueued.append(kwargs)

    def _remove_file(path: str | None) -> None:
        calls.append("remove-file")
        removed.append(path)
        document_storage.remove_document_file(path)

    async def _kb_lookup_async(db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return _kb_lookup(db, kb_id, tenant_id=tenant_id)

    async def _count_documents_async(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        return _count_documents(_db, kb_id, tenant_id=tenant_id)

    async def _count_processing_async(_db: Any, *, tenant_id: str, **_kwargs: Any) -> int:
        return _count_processing(_db, tenant_id=tenant_id)

    monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(uploads_dir))
    monkeypatch.setattr(kb_repository, "get_by_id_for_tenant", _kb_lookup)
    # 控制面上传复用的是数据面那个 ``document_service.upload``，它已经是异步栈（票 #132）：
    # 于是同一条路径既要同步孪生（控制面自己的守卫 / 列表）也要异步孪生（共用的登记）。
    monkeypatch.setattr(kb_repository, "get_by_id_for_tenant_async", _kb_lookup_async)
    monkeypatch.setattr(document_repository, "get_by_id_for_tenant", _document_lookup)
    monkeypatch.setattr(document_repository, "list_by_kb", _list_documents)
    monkeypatch.setattr(document_repository, "count_by_kb", _count_documents)
    monkeypatch.setattr(document_repository, "count_processing_for_tenant", _count_processing)
    monkeypatch.setattr(document_repository, "count_by_kb_async", _count_documents_async)
    monkeypatch.setattr(
        document_repository, "count_processing_for_tenant_async", _count_processing_async
    )
    monkeypatch.setattr(document_repository, "prepare_for_indexing", _prepare)
    monkeypatch.setattr(chunk_repository, "count_by_document_ids", _count_chunks)
    monkeypatch.setattr(console_kb_service, "get_vector_store", lambda: store)
    monkeypatch.setattr(console_kb_service, "enqueue_document", _enqueue)
    monkeypatch.setattr(console_kb_service, "remove_document_file", _remove_file)
    monkeypatch.setattr(document_service, "enqueue_document", _enqueue)

    main.app.dependency_overrides[session_module.get_db] = lambda: session
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(
                client=client,
                db=session,
                cache=cache,
                store=store,
                enqueued=enqueued,
                prepares=prepares,
                removed=removed,
                count_calls=count_calls,
                calls=calls,
                uploads_dir=uploads_dir,
            )
    finally:
        main.app.dependency_overrides.clear()


class Tenants:
    """测试里唯一需要「按请求里的文档推断租户」的替身：``count_chunks`` 断言用。

    Alpha 的两份文档与 Beta 的一份文档分属两个租户；替身只有在调用方把**同一批**
    id 一起传进来时才可能混租户，因此这里按 id 查表即可。
    """

    _OWNER = {
        DOC_ALPHA_DONE: TENANT_ALPHA,
        DOC_ALPHA_FAILED: TENANT_ALPHA,
        DOC_BETA_DONE: TENANT_BETA,
    }

    @classmethod
    def alpha_or_beta(cls, document_ids: list[str]) -> str:
        owners = {
            cls._OWNER[document_id] for document_id in document_ids if document_id in cls._OWNER
        }
        assert len(owners) <= 1, f"一次计数里混了多个租户：{document_ids}"
        return owners.pop() if owners else TENANT_ALPHA


# ─────────────────────────────────────────────────────────────── 鉴权与防跨站矩阵


def _write_requests() -> list[tuple[str, str, dict[str, Any]]]:
    """五个写端点各一条最小合法请求：缺头 / 管理员 / 未登录三组矩阵共用。"""

    return [
        ("POST", CREATE_PATH, {"json": {"name": "x", "split_strategy": "structure"}}),
        ("DELETE", f"{CREATE_PATH}/{KB_ALPHA_EMPTY}", {}),
        (
            "POST",
            UPLOAD_PATH.format(kb_id=KB_ALPHA_ONE),
            {"json": {"title": "t", "content": "c"}},
        ),
        ("POST", REINDEX_PATH.format(document_id=DOC_ALPHA_FAILED), {}),
        ("DELETE", DELETE_DOC_PATH.format(document_id=DOC_ALPHA_DONE), {}),
    ]


def test_every_write_without_the_anti_csrf_header_is_forbidden(kbs: _Harness) -> None:
    """防跨站头是写操作的硬要求：缺它一律 20011，且不产生任何副作用。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0
    audits_before = len(kbs.audit_entries())

    for method, path, kwargs in _write_requests():
        response = kbs.client.request(method, path, **kwargs)
        assert response.json()["code"] == ErrorCode.FORBIDDEN, path

    assert len(kbs.audit_entries()) == audits_before, "被拒的写请求不写账本"
    assert kbs.enqueued == []


def test_an_admin_without_a_tenant_is_forbidden_on_every_endpoint(kbs: _Harness) -> None:
    """管理员能登录、能看 ``/me``，但用户面板类接口一律 20011（不是掉登录）。"""

    assert kbs.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = kbs.client.get(TREE_PATH)
    assert response.json()["code"] == ErrorCode.FORBIDDEN

    for method, path, kwargs in _write_requests():
        response = kbs.client.request(method, path, headers=WRITE_HEADERS, **kwargs)
        assert response.json()["code"] == ErrorCode.FORBIDDEN, path

    assert kbs.audit_entries() == []
    assert kbs.enqueued == []
    assert kbs.store.deleted == []


def test_every_endpoint_without_a_session_is_unauthorized(kbs: _Harness) -> None:
    """没有会话 cookie 一律 20010：管理员之外的「未登录」是另一档。"""

    response = kbs.client.get(TREE_PATH)
    assert response.json()["code"] == ErrorCode.UNAUTHORIZED

    for method, path, kwargs in _write_requests():
        response = kbs.client.request(method, path, headers=WRITE_HEADERS, **kwargs)
        assert response.json()["code"] == ErrorCode.UNAUTHORIZED, path


# ─────────────────────────────────────────────────────────────────────── 树接口


def test_tree_returns_all_kbs_with_documents_and_grouped_chunk_counts(
    kbs: _Harness,
) -> None:
    """一次请求拿全本租户的库与文档；空库是 ``[]``，切片数每库一次分组查询。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    body = kbs.client.get(TREE_PATH).json()
    assert body["code"] == 0
    nodes = body["data"]["knowledge_bases"]
    assert [node["kb_id"] for node in nodes] == [KB_ALPHA_ONE, KB_ALPHA_EMPTY]

    populated, empty = nodes
    assert set(populated) == {
        "kb_id",
        "name",
        "split_strategy",
        "allowed_formats",
        "chunk_size",
        "documents",
    }
    assert populated["split_strategy"] == "structure"
    assert populated["allowed_formats"] == ["markdown"]
    assert populated["chunk_size"] == 512

    documents = populated["documents"]
    assert [doc["document_id"] for doc in documents] == [DOC_ALPHA_DONE, DOC_ALPHA_FAILED]
    assert [doc["chunk_count"] for doc in documents] == [4, 2]
    assert documents[0]["status"] == DocumentStatus.SUCCESS
    assert documents[1]["status"] == DocumentStatus.FAILED
    assert documents[1]["error_message"] == (
        "document indexing failed, please retry or contact support"
    )
    assert set(documents[0]) == {
        "document_id",
        "title",
        "status",
        "chunk_count",
        "error_message",
        "source_type",
        "created_at",
    }

    # 空库必须是 []（前端靠它区分父子行），不是 null，也不是「没有 documents 键」。
    assert empty["documents"] == []

    # 每库一次分组计数，不按文档逐条查（N+1）。
    assert kbs.count_calls == [[DOC_ALPHA_DONE, DOC_ALPHA_FAILED], []]

    # 控制面看整个租户，但绝不跨租户：Beta 的库与文档都不出现。
    payload = json.dumps(body["data"], ensure_ascii=False, default=str)
    assert KB_BETA_ONE not in payload
    assert DOC_BETA_DONE not in payload


def test_tree_never_widens_by_key_whitelist(kbs: _Harness) -> None:
    """控制面不做白名单收窄：会话租户里有哪些库就返回哪些。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    body = kbs.client.get(TREE_PATH).json()

    assert {node["kb_id"] for node in body["data"]["knowledge_bases"]} == {
        KB_ALPHA_ONE,
        KB_ALPHA_EMPTY,
    }


# ─────────────────────────────────────────────────────────────────────── 建库


def test_create_kb_uses_the_session_tenant_and_writes_one_audit(kbs: _Harness) -> None:
    """建库走 ``knowledge_base_service.create_kb``：租户来自会话，响应与数据面同形。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        CREATE_PATH,
        json={
            "name": "Handbook",
            "description": "team handbook",
            "split_strategy": "structure",
            "chunk_size": 300,
        },
        headers=WRITE_HEADERS,
    )

    data = kbs.data(response)
    assert data["tenant_id"] == TENANT_ALPHA, "租户只能来自会话，不能来自请求体"
    assert data["name"] == "Handbook"
    assert data["split_strategy"] == "structure"
    assert data["allowed_formats"] == ["markdown"]
    assert data["chunk_size"] == 300
    assert data["created_at"]

    entry = kbs.audit_entries()[-1]
    assert entry.actor_id == ALICE_ID
    assert entry.action == "kb.create"
    assert entry.target == f"kb:{data['kb_id']}"
    assert entry.before_snapshot is None
    assert entry.after_snapshot == {
        "kb_id": data["kb_id"],
        "name": "Handbook",
        "split_strategy": "structure",
        "allowed_formats": ["markdown"],
        "chunk_size": 300,
    }

    listed = kbs.client.get(TREE_PATH).json()["data"]["knowledge_bases"]
    assert data["kb_id"] in {node["kb_id"] for node in listed}, "新建的库下一次读就能看见"
    assert kbs.documents_of(data["kb_id"]) == [], "新库是空库"


def test_create_kb_rejects_a_tenant_id_in_the_body(kbs: _Harness) -> None:
    """``tenant_id`` 不进请求体：多发一个键就是 20002，别让调用方能指定写进哪个租户。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        CREATE_PATH,
        json={"name": "x", "split_strategy": "structure", "tenant_id": TENANT_BETA},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == ErrorCode.REQUEST_VALIDATION_ERROR
    assert kbs.audit_entries() == []


def test_create_kb_still_obeys_the_plan_kb_quota(kbs: _Harness) -> None:
    """面板不是绕过套餐的后门：静态库数配额由 ``create_kb`` 判（20014），且不写账本。"""

    tenant = next(row for row in kbs.db._rows[Tenant] if row.id == TENANT_ALPHA)
    tenant.plan_id = 1  # free：max_kb=1，而 Alpha 已经有 2 个库
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        CREATE_PATH,
        json={"name": "x", "split_strategy": "structure"},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == ErrorCode.QUOTA_EXCEEDED
    assert len(kbs.db._rows[KnowledgeBase]) == 3, "被配额拒绝的库不落行"
    assert kbs.audit_entries() == []


# ─────────────────────────────────────────────────────────────────────── 删库


def test_delete_empty_kb_removes_it_and_audits_before_only(kbs: _Harness) -> None:
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(f"{CREATE_PATH}/{KB_ALPHA_EMPTY}", headers=WRITE_HEADERS)

    assert kbs.data(response) == {"kb_id": KB_ALPHA_EMPTY, "deleted": True}
    assert all(kb.id != KB_ALPHA_EMPTY for kb in kbs.db._rows.get(KnowledgeBase, []))
    assert "delete-row" in kbs.calls and "commit" in kbs.calls

    entry = kbs.audit_entries()[-1]
    assert entry.actor_id == ALICE_ID
    assert entry.action == "kb.delete"
    assert entry.target == f"kb:{KB_ALPHA_EMPTY}"
    assert entry.before_snapshot == {
        "kb_id": KB_ALPHA_EMPTY,
        "name": f"name-{KB_ALPHA_EMPTY}",
        "split_strategy": "structure",
        "allowed_formats": ["markdown"],
        "chunk_size": 512,
    }
    assert entry.after_snapshot is None

    listed = kbs.client.get(TREE_PATH).json()["data"]["knowledge_bases"]
    assert KB_ALPHA_EMPTY not in {node["kb_id"] for node in listed}


def test_delete_kb_with_documents_is_rejected_and_says_to_delete_them_first(
    kbs: _Harness,
) -> None:
    """不做级联删除：库内还有文档就是 20015，库与文档都不动。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(f"{CREATE_PATH}/{KB_ALPHA_ONE}", headers=WRITE_HEADERS)

    body = response.json()
    assert body["code"] == ErrorCode.PARAM_ERROR
    assert "先删文档" in body["msg"]
    assert body["data"] is None
    assert len(kbs.documents_of(KB_ALPHA_ONE)) == 2
    assert any(kb.id == KB_ALPHA_ONE for kb in kbs.db._rows.get(KnowledgeBase, []))
    assert kbs.audit_entries() == []


@pytest.mark.parametrize("kb_id", [KB_BETA_ONE, "kb-does-not-exist"], ids=["cross", "missing"])
def test_delete_kb_of_another_tenant_or_missing_is_not_found(kbs: _Harness, kb_id: str) -> None:
    """跨租户与不存在同一个码（20008），不泄露资源存在性。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(f"{CREATE_PATH}/{kb_id}", headers=WRITE_HEADERS)

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert len(kbs.db._rows.get(KnowledgeBase, [])) == 3, "跨租户的库绝不能被删掉"
    assert kbs.audit_entries() == []


# ─────────────────────────────────────────────────────────────────────── 上传文档


def test_json_upload_returns_processing_and_audits_without_the_body_text(
    kbs: _Harness,
) -> None:
    """单端点 JSON 分支：立即回 status=3，审计只留 id/title/status，不留正文。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        UPLOAD_PATH.format(kb_id=KB_ALPHA_ONE),
        json={"title": "Console note", "content": CONTENT_SENTINEL},
        headers={**WRITE_HEADERS, "X-Request-ID": "console-upload-req"},
    )

    data = kbs.data(response)
    assert data["kb_id"] == KB_ALPHA_ONE
    assert data["status"] == DocumentStatus.PROCESSING
    assert data["chunk_count"] == 0
    assert data["source_type"] == "markdown"

    document = kbs.document(data["document_id"])
    assert document.content == CONTENT_SENTINEL, "JSON 分支直接落正文，不读请求线程之外的解析"
    assert document.status == DocumentStatus.PROCESSING

    assert kbs.enqueued == [
        {
            "document_id": data["document_id"],
            "tenant_id": TENANT_ALPHA,
            "kb_id": KB_ALPHA_ONE,
            "request_id": "console-upload-req",
            "reparse": False,
        }
    ]

    entry = kbs.audit_entries()[-1]
    assert entry.actor_id == ALICE_ID
    assert entry.action == "doc.upload"
    assert entry.target == f"doc:{data['document_id']}"
    assert entry.before_snapshot is None
    assert entry.after_snapshot["status"] == DocumentStatus.PROCESSING
    assert CONTENT_SENTINEL not in json.dumps(entry.after_snapshot, ensure_ascii=False)


def test_multipart_upload_stores_the_file_and_returns_processing(kbs: _Harness) -> None:
    """文件分支：落盘由 ``document_service.upload`` 收口，正文留给 Worker 解析。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        UPLOAD_PATH.format(kb_id=KB_ALPHA_ONE),
        data={"title": "From file"},
        files={"file": ("Notes.MD", b"# Body\n", "text/markdown")},
        headers=WRITE_HEADERS,
    )

    data = kbs.data(response)
    assert data["status"] == DocumentStatus.PROCESSING
    assert data["source_type"] == "markdown"

    document = kbs.document(data["document_id"])
    assert document.content == "", "文件模式不在请求线程解析正文"
    assert document.source_filename == "Notes.MD", "原始文件名只进列"
    assert document.source_file_path == f"{TENANT_ALPHA}/{data['document_id']}.md"
    assert (kbs.uploads_dir / document.source_file_path).read_bytes() == b"# Body\n"

    entry = kbs.audit_entries()[-1]
    assert entry.action == "doc.upload"
    assert entry.after_snapshot["title"] == "From file"
    assert b"# Body" not in json.dumps(entry.after_snapshot, default=str).encode()


@pytest.mark.parametrize("kb_id", [KB_BETA_ONE, "kb-does-not-exist"], ids=["cross", "missing"])
def test_upload_to_another_tenant_or_missing_kb_is_not_found(kbs: _Harness, kb_id: str) -> None:
    """上传先过 ``ensure_visible``：跨租户与不存在统一 20008，不落盘、不建行、不投递。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        UPLOAD_PATH.format(kb_id=kb_id),
        json={"title": "t", "content": "body"},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert kbs.db.added_of(Document) == []
    assert kbs.enqueued == []
    assert kbs.audit_entries() == []


# ─────────────────────────────────────────────────────────────────────── 重建索引


def test_reindex_failed_document_returns_processing_and_audits_before_after(
    kbs: _Harness,
) -> None:
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        REINDEX_PATH.format(document_id=DOC_ALPHA_FAILED),
        params={"reparse": "true"},
        headers={**WRITE_HEADERS, "X-Request-ID": "console-reindex-req"},
    )

    data = kbs.data(response)
    assert data == {
        "document_id": DOC_ALPHA_FAILED,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "reparse": True,
    }
    assert kbs.prepares == [
        {
            "document_id": DOC_ALPHA_FAILED,
            "tenant_id": TENANT_ALPHA,
            "allow_success": False,
        }
    ], "准备入队只置状态、不盖租约；Worker 才是唯一抢占者"
    assert kbs.enqueued == [
        {
            "document_id": DOC_ALPHA_FAILED,
            "tenant_id": TENANT_ALPHA,
            "kb_id": KB_ALPHA_ONE,
            "request_id": "console-reindex-req",
            "reparse": True,
        }
    ]

    entry = kbs.audit_entries()[-1]
    assert entry.action == "doc.reindex"
    assert entry.target == f"doc:{DOC_ALPHA_FAILED}"
    assert entry.before_snapshot["status"] == DocumentStatus.FAILED
    assert entry.after_snapshot["status"] == DocumentStatus.PROCESSING
    assert entry.after_snapshot["reparse"] is True
    assert CONTENT_SENTINEL not in json.dumps(entry.before_snapshot, ensure_ascii=False)


def test_reindex_success_document_requires_force(kbs: _Harness) -> None:
    """成功文档未 ``force`` 时 20027：既不准备也不投递。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        REINDEX_PATH.format(document_id=DOC_ALPHA_DONE),
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == ErrorCode.REINDEX_REQUIRES_FORCE
    assert kbs.prepares == []
    assert kbs.enqueued == []
    assert kbs.audit_entries() == []


def test_reindex_success_document_with_force_is_prepared(kbs: _Harness) -> None:
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        REINDEX_PATH.format(document_id=DOC_ALPHA_DONE),
        params={"force": "true"},
        headers=WRITE_HEADERS,
    )

    assert kbs.data(response)["status"] == DocumentStatus.PROCESSING
    assert kbs.prepares == [
        {
            "document_id": DOC_ALPHA_DONE,
            "tenant_id": TENANT_ALPHA,
            "allow_success": True,
        }
    ]
    assert kbs.enqueued[0]["document_id"] == DOC_ALPHA_DONE


def test_reindex_of_a_document_being_processed_is_rejected(kbs: _Harness) -> None:
    """有效租约内的处理中 → 20025（force 也救不了）：不准备、不投递、不审计。"""

    kbs.db._rows[Document].append(
        _doc(
            "doc-live",
            KB_ALPHA_ONE,
            TENANT_ALPHA,
            status=DocumentStatus.PROCESSING,
            title="Live",
            created_at=datetime(2026, 1, 4, 10, 0, 0),
            index_claimed_at=now(),
        )
    )
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        REINDEX_PATH.format(document_id="doc-live"),
        params={"force": "true"},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert kbs.prepares == []
    assert kbs.enqueued == []
    assert kbs.audit_entries() == []


def test_reindex_lost_prepare_race_is_processing_conflict(
    kbs: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫通过但准备失败（并发请求先改了同一行）：20025，不投递、不审计。"""

    monkeypatch.setattr(document_repository, "prepare_for_indexing", lambda *_a, **_k: False)
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(
        REINDEX_PATH.format(document_id=DOC_ALPHA_FAILED), headers=WRITE_HEADERS
    )

    assert response.json()["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert kbs.enqueued == []
    assert kbs.audit_entries() == []


@pytest.mark.parametrize(
    "document_id", [DOC_BETA_DONE, "doc-does-not-exist"], ids=["cross", "missing"]
)
def test_reindex_of_another_tenant_or_missing_document_is_not_found(
    kbs: _Harness, document_id: str
) -> None:
    """缺失与跨租户统一 20016，不泄露存在性。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.post(REINDEX_PATH.format(document_id=document_id), headers=WRITE_HEADERS)

    assert response.json()["code"] == ErrorCode.NOT_FOUND
    assert kbs.prepares == []
    assert kbs.enqueued == []
    assert kbs.audit_entries() == []


# ─────────────────────────────────────────────────────────────────────── 删除文档


def test_delete_document_purges_file_and_row_in_the_original_order(
    kbs: _Harness, tmp_path: Path
) -> None:
    """清除顺序不变：purge → 删原文件 → 删行 → （审计）→ 提交。"""

    source = tmp_path / "alpha-done.md"
    source.write_text("# body", encoding="utf-8")
    kbs.document(DOC_ALPHA_DONE).source_file_path = str(source)
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(
        DELETE_DOC_PATH.format(document_id=DOC_ALPHA_DONE), headers=WRITE_HEADERS
    )

    assert kbs.data(response) == {"document_id": DOC_ALPHA_DONE, "deleted": True}
    assert kbs.store.deleted == [DOC_ALPHA_DONE], "切片清除走存储列表，不直连某个存储"
    assert kbs.store.tenant_ids == [TENANT_ALPHA], "删除切片也必须强制带租户"
    assert not source.exists(), "原文件必须删掉"
    assert kbs.removed == [str(source)]

    purge_at = kbs.calls.index("purge")
    remove_at = kbs.calls.index("remove-file", purge_at)
    delete_at = kbs.calls.index("delete-row", remove_at)
    commit_at = kbs.calls.index("commit", delete_at)
    assert purge_at < remove_at < delete_at < commit_at, "顺序是契约的一部分，不能重排"
    assert all(doc.id != DOC_ALPHA_DONE for doc in kbs.db._rows.get(Document, []))

    entry = kbs.audit_entries()[-1]
    assert entry.actor_id == ALICE_ID
    assert entry.action == "doc.delete"
    assert entry.target == f"doc:{DOC_ALPHA_DONE}"
    assert entry.before_snapshot == {
        "document_id": DOC_ALPHA_DONE,
        "kb_id": KB_ALPHA_ONE,
        "title": "Alpha done",
        "status": DocumentStatus.SUCCESS,
        "source_type": "markdown",
    }
    assert entry.after_snapshot is None


def test_delete_document_being_processed_is_rejected(kbs: _Harness) -> None:
    """有效租约内的处理中拒绝删除（20025）：不清切片、不删文件、不动行。"""

    kbs.db._rows[Document].append(
        _doc(
            "doc-live",
            KB_ALPHA_ONE,
            TENANT_ALPHA,
            status=DocumentStatus.PROCESSING,
            title="Live",
            created_at=datetime(2026, 1, 4, 10, 0, 0),
            index_claimed_at=now(),
        )
    )
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(
        DELETE_DOC_PATH.format(document_id="doc-live"), headers=WRITE_HEADERS
    )

    assert response.json()["code"] == ErrorCode.DOCUMENT_IS_PROCESSING
    assert kbs.store.deleted == []
    assert kbs.audit_entries() == []


@pytest.mark.parametrize(
    "document_id", [DOC_BETA_DONE, "doc-does-not-exist"], ids=["cross", "missing"]
)
def test_delete_document_of_another_tenant_or_missing_is_not_found(
    kbs: _Harness, document_id: str
) -> None:
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.delete(
        DELETE_DOC_PATH.format(document_id=document_id), headers=WRITE_HEADERS
    )

    assert response.json()["code"] == ErrorCode.NOT_FOUND
    assert kbs.store.deleted == []
    assert kbs.audit_entries() == []


# ───────────────────────────────────────────────── 两个租户的对照（会话隔离）


def test_a_second_tenant_sees_only_its_own_tree_and_documents(kbs: _Harness) -> None:
    """同一个应用、换一个会话 cookie：Beta 看不到 Alpha 的库，Alpha 也看不到 Beta 的。"""

    alice_session = kbs.session_id_of(kbs.login("alice", USER_PASSWORD))
    alice_tree = kbs.client.get(TREE_PATH).json()["data"]["knowledge_bases"]
    assert {node["kb_id"] for node in alice_tree} == {KB_ALPHA_ONE, KB_ALPHA_EMPTY}

    assert kbs.login("bob", USER_PASSWORD).json()["code"] == 0
    bob_tree = kbs.client.get(TREE_PATH).json()["data"]["knowledge_bases"]
    assert {node["kb_id"] for node in bob_tree} == {KB_BETA_ONE}
    assert [doc["document_id"] for doc in bob_tree[0]["documents"]] == [DOC_BETA_DONE]

    kbs.use_cookie(alice_session)
    assert {
        node["kb_id"] for node in kbs.client.get(TREE_PATH).json()["data"]["knowledge_bases"]
    } == {
        KB_ALPHA_ONE,
        KB_ALPHA_EMPTY,
    }


# ──────────────────────────────────────────────────────────────── 读文档原文（只读）

ALPHA_BODY = "# 退款政策\n\n签收后 7 天内可申请无理由退款。\n"


def test_read_content_returns_the_body_stored_in_the_database(kbs: _Harness) -> None:
    """JSON 模式（贴正文上传）：库里就有 content，直接读，不碰磁盘。"""

    kbs.document(DOC_ALPHA_DONE).content = ALPHA_BODY
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.get(READ_CONTENT_PATH.format(document_id=DOC_ALPHA_DONE))

    data = kbs.data(response)
    assert data["content"] == ALPHA_BODY
    assert data["length"] == len(ALPHA_BODY)
    assert data["truncated"] is False
    assert data["status"] == DocumentStatus.SUCCESS
    assert data["source_type"] == "markdown"


def test_read_content_parses_the_source_file_for_file_mode(kbs: _Harness, tmp_path: Path) -> None:
    """文件模式：库里 content 是空串，正文现场解析落盘文件——与 Worker 切块用的是同一个函数。"""

    source = tmp_path / "alpha-file.md"
    source.write_text("# 标题\n\n正文一段。\n", encoding="utf-8")
    document = kbs.document(DOC_ALPHA_DONE)
    assert document.content == "", "文件模式下库里不该有正文"
    document.source_file_path = str(source)
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    data = kbs.data(kbs.client.get(READ_CONTENT_PATH.format(document_id=DOC_ALPHA_DONE)))

    assert "正文一段" in data["content"]
    assert data["length"] == len(data["content"])
    assert data["truncated"] is False


def test_read_content_truncates_but_reports_the_real_length(kbs: _Harness) -> None:
    """截断必须说出来：只给截断后的文本而不说原文多长，会让人以为文档就这么长。"""

    kbs.document(DOC_ALPHA_DONE).content = "x" * 50
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    data = kbs.data(kbs.client.get(READ_CONTENT_PATH.format(document_id=DOC_ALPHA_DONE)))

    # 上限是服务层常量（20 万字符），这里用 50 字符造不出截断，所以直接断言「没截断」，
    # 真正的截断路径由下面那条用 monkeypatch 的用例覆盖。
    assert data["truncated"] is False
    assert data["length"] == 50


def test_read_content_honours_the_character_cap(
    kbs: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把上限压到 10 字符，验证 truncation 与 length 这一对字段的语义。"""

    console_kb_service = importlib.import_module("app.services.console_kb_service")
    monkeypatch.setattr(console_kb_service, "DOCUMENT_CONTENT_MAX_CHARS", 10)

    kbs.document(DOC_ALPHA_DONE).content = "0123456789abcdef"
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    data = kbs.data(kbs.client.get(READ_CONTENT_PATH.format(document_id=DOC_ALPHA_DONE)))

    assert data["content"] == "0123456789"
    assert data["truncated"] is True
    assert data["length"] == 16, "length 是原文真实长度，不是截断后的长度"


@pytest.mark.parametrize("document_id", [DOC_BETA_DONE, "no-such-document"])
def test_read_content_of_another_tenant_or_missing_is_not_found(
    kbs: _Harness, document_id: str
) -> None:
    """跨租户与不存在同码（20016）：可区分的拒绝会泄露资源存在性。"""

    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.get(READ_CONTENT_PATH.format(document_id=document_id))

    assert response.json()["code"] == ErrorCode.NOT_FOUND


def test_read_content_needs_no_anti_csrf_header(kbs: _Harness) -> None:
    """读接口走读门：不带 x-requested-with 也能看原文（那个头是给写操作防跨站用的）。"""

    kbs.document(DOC_ALPHA_DONE).content = ALPHA_BODY
    assert kbs.login("alice", USER_PASSWORD).json()["code"] == 0

    response = kbs.client.get(READ_CONTENT_PATH.format(document_id=DOC_ALPHA_DONE))

    assert response.json()["code"] == 0
    assert kbs.audit_entries() == [], "读原文不写账本"
