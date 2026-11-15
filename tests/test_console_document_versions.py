"""#177 · 调试台的版本列表与回滚：``/console`` 上的两条接口（第 ② 层，ASGI）。

接缝与 ``tests/test_console_kbs.py`` 同一条：真实应用 + 真实的 ``POST /console/login``
拿到会话 cookie，之后只看统一响应体、审计行与投递。**不另造一套登录流程**——被验的是
「面板身份能列版本、能点一次回滚」，登录只是到达那个身份的方式。

为什么控制面这两条接口还要单独验一遍：数据面那两条（#175 / #176）走 API Key，控制面换的是
**身份的门**，语义必须一字不差。本文件守的是「换门之后没走样」：

* 租户来自**会话**，请求体与路径里没有任何租户字段；跨租户一律「不存在」（20016），
  不返回「无权限」（可区分的拒绝会泄露资源存在性）；
* 列表带稿号 / 状态 / 校验和 / 字符数 / 时间，正文**按需**（``include_content``）；
* 回滚的三种拒绝（处理中 20025 / 稿不存在 20030 / 已是当前稿 20032）与数据面逐字一致，
  业务失败也是 HTTP 200 + ``code``（ADR-0007）；
* 回滚**只投递一次**重建且载荷带目标稿号——它是委托给
  ``document_version_service.rollback_to_version`` 的结果，控制面自己不改指针、
  不写版本行（「禁止另写一套逻辑」这条是票面验收项）；
* 写入口照样要求防跨站头（未登录 20010 / 缺头 20011），读入口不要求。

版本行是**真模型实例**、文档行是与 ``app.models.document.Document`` 同形状的替身：假会话
（``tests/fake_db.RecordingSession``）按 ``select(Model)`` 的等值条件在预置行里筛选，于是
「列出版本」「取某一稿」这些读是真的走了一遍仓储 SQL 形状。两条 ``UPDATE``（当前稿转归档 /
目标稿转当前稿）在替身上不模拟行变化，夹具按**与真实实现相同的语义**就地改内存行——
与 ``test_console_kbs.py`` 对 ``prepare_for_indexing`` 的处理同一个理由。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.exceptions import ErrorCode
from app.db.base import now
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.document_version import (
    DOCUMENT_VERSION_ARCHIVED,
    DOCUMENT_VERSION_CURRENT,
    DocumentVersion,
)
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from app.repositories import document_repository, document_version_repository
from app.utils.checksum import checksum_of
from app.utils.status import DocumentStatus
from tests.fake_db import MemoryPlanCache, RecordingSession, override_all_db

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"
ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

DOC_ALPHA = "doc-alpha"
DOC_BETA = "doc-beta"

#: 三稿正文：稿号 → 正文。校验和、字符数都从这一份算，不在断言里手抄两遍。
DRAFTS = {
    1: "第一稿：退款流程见附件。",
    2: "第二稿：退款流程见附件，另需工单号。",
    3: "第三稿：退款流程改为线上申请。",
}
CURRENT_DRAFT = 3

VERSIONS_PATH = "/console/documents/{document_id}/versions"
ROLLBACK_PATH = "/console/documents/{document_id}/versions/{version}/rollback"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


class _ConsoleSession(RecordingSession):
    """``RecordingSession`` + 「提交了几次」——写接口的事务边界是真的走了一遍生产实现。"""

    def __init__(self, *, rows: dict[type, list[Any]], calls: list[str]) -> None:
        super().__init__(rows=rows)
        self.calls = calls

    def commit(self) -> None:
        super().commit()
        self.calls.append("commit")


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: _ConsoleSession
    enqueued: list[dict[str, Any]]
    version_calls: list[str]

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def versions_of(self, document_id: str) -> list[DocumentVersion]:
        return [
            row
            for row in self.db._rows.get(DocumentVersion, [])
            if row.document_id == document_id
        ]

    def document(self, document_id: str) -> Any:
        return next(doc for doc in self.db._rows.get(Document, []) if doc.id == document_id)


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
) -> Account:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        status="active",
    )


def _doc(
    document_id: str,
    tenant_id: str,
    *,
    current_version: int,
    content: str,
    status: int = DocumentStatus.SUCCESS,
    index_claimed_at: datetime | None = None,
) -> Any:
    """文档行替身：字段形状与 ``app.models.document.Document`` 对齐。

    用 ``SimpleNamespace`` 而不是建 SQLite 表：本票验的是端点的**行为**（码、形状、审计、
    投递），不是数据库本身——版本行的列行为已由 ``test_document_version_repository.py``
    与 ``test_document_version_rollback.py`` 覆盖。
    """

    return SimpleNamespace(
        id=document_id,
        tenant_id=tenant_id,
        kb_id="kb-alpha" if tenant_id == TENANT_ALPHA else "kb-beta",
        title=f"title-{document_id}",
        content=content,
        source_type="text",
        source_file_path=None,
        source_filename=None,
        status=status,
        index_claimed_at=index_claimed_at,
        error_message=None,
        created_at=datetime(2026, 1, 1, 9, 0, 0),
        current_version=current_version,
    )


def _version(
    document_id: str,
    version: int,
    *,
    content: str,
    status: str,
    created_at: datetime,
) -> DocumentVersion:
    return DocumentVersion(
        id=f"{document_id}-v{version}",
        document_id=document_id,
        version=version,
        content=content,
        checksum=checksum_of(content),
        status=status,
        created_at=created_at,
    )


def _world() -> dict[type, list[Any]]:
    """一个够用的世界：两种角色、两个租户、两份文档、Alpha 的三稿。

    版本行按**稿号倒序**摆（``list_by_document`` 的排序在 SQL 里，替身不模拟排序）——
    与真库返回的顺序一致，于是断言里的 ``[3, 2, 1]`` 是在验契约而不是在验巧合。
    """

    documents = [
        _doc(DOC_ALPHA, TENANT_ALPHA, current_version=CURRENT_DRAFT, content=DRAFTS[3]),
        _doc(DOC_BETA, TENANT_BETA, current_version=1, content="Beta 的唯一一稿。"),
    ]
    versions = [
        _version(
            DOC_ALPHA,
            number,
            content=DRAFTS[number],
            status=DOCUMENT_VERSION_CURRENT if number == CURRENT_DRAFT else DOCUMENT_VERSION_ARCHIVED,
            created_at=datetime(2026, 11, number, 10, 0, 0),
        )
        for number in (3, 2, 1)
    ]
    versions.append(
        _version(
            DOC_BETA,
            1,
            content="Beta 的唯一一稿。",
            status=DOCUMENT_VERSION_CURRENT,
            created_at=datetime(2026, 11, 1, 10, 0, 0),
        )
    )
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
        Document: documents,
        DocumentVersion: versions,
    }


@pytest.fixture
def versions(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """真实应用 + 真实登录 + 内存会话；只替换「替身会话答不了的那几件事」。

    被替换的三处：两处 ``UPDATE ... WHERE``（替身不模拟行变化，见模块文档）与
    ``count_processing_for_tenant``（``SELECT count(*)`` 在替身上只是噪声）。业务判定
    （``ensure_versionable`` / 静态配额 / ``rollback_to_version`` 的全过程 / 审计写法）
    全部走真代码。
    """

    main = importlib.import_module("app.main")
    console_deps = importlib.import_module("app.api.console_deps")
    dispatch = importlib.import_module("app.services.index_dispatch")

    calls: list[str] = []
    session = _ConsoleSession(rows=_world(), calls=calls)
    cache = MemoryPlanCache()
    enqueued: list[dict[str, Any]] = []
    version_calls: list[str] = []

    def _archive_except(_db: Any, document_id: str, *, keep: int) -> None:
        version_calls.append(f"archive-except:{document_id}:{keep}")
        for row in session._rows.get(DocumentVersion, []):
            if (
                row.document_id == document_id
                and row.version != keep
                and row.status == DOCUMENT_VERSION_CURRENT
            ):
                row.status = DOCUMENT_VERSION_ARCHIVED

    def _mark_current(_db: Any, document_id: str, *, version: int) -> None:
        version_calls.append(f"mark-current:{document_id}:{version}")
        for row in session._rows.get(DocumentVersion, []):
            if row.document_id == document_id and row.version == version:
                row.status = DOCUMENT_VERSION_CURRENT

    def _enqueue(**kwargs: Any) -> None:
        enqueued.append(kwargs)

    async def _archive_except_async(db: Any, document_id: str, *, keep: int) -> None:
        _archive_except(db, document_id, keep=keep)

    async def _mark_current_async(db: Any, document_id: str, *, version: int) -> None:
        _mark_current(db, document_id, version=version)

    async def _count_processing(_db: Any, *, tenant_id: str, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(
        document_version_repository, "archive_current_except", _archive_except_async
    )
    monkeypatch.setattr(document_version_repository, "mark_current", _mark_current_async)
    monkeypatch.setattr(
        document_repository, "count_processing_for_tenant", _count_processing
    )
    # ``rollback_to_version`` 在 ``after_commit`` 回调里用**自己模块绑定的**
    # ``enqueue_document``（函数内 import）：换模块属性就是换它。
    monkeypatch.setattr(dispatch, "enqueue_document", _enqueue)

    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    override_all_db(main.app, session)
    try:
        with TestClient(main.app) as client:
            yield _Harness(
                client=client,
                db=session,
                enqueued=enqueued,
                version_calls=version_calls,
            )
    finally:
        main.app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────── 列版本


def test_listing_versions_answers_draft_number_status_checksum_char_count_and_time(
    versions: _Harness,
) -> None:
    """列表按稿号倒序，每行五个可核对的事实 + 当前稿指针；正文默认不给。"""

    assert versions.login("alice", USER_PASSWORD).json()["code"] == 0

    body = versions.client.get(VERSIONS_PATH.format(document_id=DOC_ALPHA)).json()

    assert body["code"] == 0, body
    data = body["data"]
    assert data["document_id"] == DOC_ALPHA
    assert data["current_version"] == CURRENT_DRAFT

    items = data["items"]
    assert [item["version"] for item in items] == [3, 2, 1]
    assert [item["status"] for item in items] == ["current", "archived", "archived"]
    assert [item["checksum"] for item in items] == [checksum_of(DRAFTS[n]) for n in (3, 2, 1)]
    assert [item["char_count"] for item in items] == [len(DRAFTS[n]) for n in (3, 2, 1)], (
        "字符数是这一稿正文的长度，不是分词口径的切块窗口"
    )
    assert [item["created_at"] for item in items] == [
        "2026-11-03T10:00:00",
        "2026-11-02T10:00:00",
        "2026-11-01T10:00:00",
    ]
    assert [item["content"] for item in items] == [None, None, None], "正文按需"


def test_the_body_is_only_returned_when_asked_for(versions: _Harness) -> None:
    """``?include_content=true`` 才带正文；其余字段两种情况下逐字相同。"""

    assert versions.login("alice", USER_PASSWORD).json()["code"] == 0
    path = VERSIONS_PATH.format(document_id=DOC_ALPHA)

    without = versions.data(versions.client.get(path))["items"]
    with_body = versions.data(versions.client.get(f"{path}?include_content=true"))["items"]

    assert [item["content"] for item in with_body] == [DRAFTS[3], DRAFTS[2], DRAFTS[1]]
    for plain, rich in zip(without, with_body, strict=True):
        assert {key: rich[key] for key in plain if key != "content"} == {
            key: value for key, value in plain.items() if key != "content"
        }


def test_listing_another_tenants_document_is_not_found(versions: _Harness) -> None:
    """跨租户一律「不存在」（20016）：列表也不泄露存在性。"""

    assert versions.login("alice", USER_PASSWORD).json()["code"] == 0

    body = versions.client.get(VERSIONS_PATH.format(document_id=DOC_BETA)).json()

    assert body["code"] == ErrorCode.NOT_FOUND
    assert body["data"] is None


def test_listing_versions_needs_a_console_session(versions: _Harness) -> None:
    """没有会话 cookie 是 20010（该去登录），不是 20016。"""

    body = versions.client.get(VERSIONS_PATH.format(document_id=DOC_ALPHA)).json()

    assert body["code"] == ErrorCode.UNAUTHORIZED


def test_an_admin_without_a_tenant_cannot_list_versions(versions: _Harness) -> None:
    """管理员能登录，但用户面板类的库与文档接口一律 20011（不是掉登录）。"""

    assert versions.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    body = versions.client.get(VERSIONS_PATH.format(document_id=DOC_ALPHA)).json()

    assert body["code"] == ErrorCode.FORBIDDEN


