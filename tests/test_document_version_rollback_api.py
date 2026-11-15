"""#176 · 回滚接口：``POST /documents/{id}/versions/{v}/rollback``（第 ② 层，ASGI）。

接口形状与 #175 的两条版本路由同一条分层：路由只做**分流 / 白名单 / 信封**，业务在
``app.services.document_version_service``。本文件守的是**接口边界**：

* 成功是 HTTP 200 + ``code=0``（ADR-0007：业务失败也是 200，只有 ``code`` 说话）；
* 四种拒绝：处理中 20025 / 稿不存在 20030 / 已是当前稿 20032 / 跨租户 20016（不泄露存在性）；
* 路径里的稿号不是整数 → 既有的 20002（参数校验失败），不是 500；
* 回滚之后**立刻**是「已排队、未开工」，而重建投递在**提交之后**才发生、载荷带目标稿号。

数据库用同一个 SQLite 文件、每个请求各开一个异步引擎；投递缝换成记账器——于是
「事务提交了吗」「投递看到的是新指针吗」都是真的。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ErrorCode as Codes
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
DOC_ID = "doc-1"
CONTENTS = {
    1: "第 1 稿正文：退款流程见附件。",
    2: "第 2 稿正文：退款流程见附件，另需工单号。",
    3: "第 3 稿正文：退款流程改为线上申请。",
}
ROLLBACK_URL = f"/api/v1/documents/{DOC_ID}/versions/{{version}}/rollback"


def _checksum(text: str) -> str:
    from app.utils.checksum import checksum_of

    return checksum_of(text)


class _DbObservingPublisher:
    """记账投递器：在投递那一刻回读数据库，于是「投递在提交之后」可以被证明。"""

    def __init__(self, db_path: Path) -> None:
        self._url = f"sqlite:///{db_path.as_posix()}"
        self.calls: list[dict[str, Any]] = []
        self.seen: list[dict[str, Any]] = []

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> None:
        self.calls.append({"kwargs": dict(kwargs), "task_id": task_id})
        engine = create_engine(self._url)
        try:
            with engine.connect() as connection:
                pointer = connection.execute(
                    select(Document.current_version).where(Document.id == kwargs["document_id"])
                ).scalar_one()
        finally:
            engine.dispose()
        self.seen.append({"pointer": pointer})


@pytest.fixture
def api(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """离线接缝：一个 SQLite 文件、两张真表、按需播种，以及可改身份的当前 Key。"""

    db_path = tmp_path / "docver-rollback.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    sync_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Document.__table__.create(sync_engine)
    DocumentVersion.__table__.create(sync_engine)

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    dispatch = importlib.import_module("app.services.index_dispatch")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    publisher = _DbObservingPublisher(db_path)
    previous_publisher = dispatch.get_task_publisher()
    dispatch.set_task_publisher(publisher)

    async def _db() -> Any:
        # 引擎建在事件循环里（TestClient 的 portal 循环）：aiosqlite 的连接绑定循环。
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine) as session:
                yield session
        finally:
            await engine.dispose()

    identity: dict[str, DevKeyContext] = {"key": DevKeyContext(tenant_id=TENANT_ID)}

    main.app.dependency_overrides[async_session_mod.get_async_db] = _db
    main.app.dependency_overrides[get_current_key] = lambda: identity["key"]
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                publisher=publisher,
                set_tenant=lambda tenant_id: identity.update(
                    key=DevKeyContext(tenant_id=tenant_id)
                ),
                seed=lambda *rows: _seed(sync_engine, *rows),
                rows=lambda: _versions(sync_engine),
                document=lambda: _document(sync_engine),
                drop_version=lambda version: _drop_version(sync_engine, version),
            )
    finally:
        main.app.dependency_overrides.clear()
        dispatch.set_task_publisher(previous_publisher)
        sync_engine.dispose()


def _seed(engine: Any, *rows: Any) -> None:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        for row in rows:
            session.merge(row)
        session.commit()


def _document(engine: Any) -> Document:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        doc = session.get(Document, DOC_ID)
        assert doc is not None
        session.expunge(doc)
        return doc


def _versions(engine: Any) -> list[tuple[int, str]]:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        rows = session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == DOC_ID)
            .order_by(DocumentVersion.version)
        ).scalars()
        return [(row.version, row.status) for row in rows]


def _drop_version(engine: Any, version: int) -> None:
    """模拟保留策略的物理删除（#178）：版本行整行消失，不留墓碑。"""

    factory = sessionmaker(bind=engine)
    with factory() as session:
        row = session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == DOC_ID,
                DocumentVersion.version == version,
            )
        ).scalar_one()
        session.delete(row)
        session.commit()


def _seed_three_drafts(api: SimpleNamespace) -> None:
    api.seed(
        Document(
            id=DOC_ID,
            tenant_id=TENANT_ID,
            kb_id="kb-1",
            title="积分规则",
            content=CONTENTS[3],
            status=DocumentStatus.SUCCESS,
            current_version=3,
        )
    )
    for number in (1, 2, 3):
        api.seed(
            DocumentVersion(
                id=f"v{number}",
                document_id=DOC_ID,
                version=number,
                content=CONTENTS[number],
                checksum=_checksum(CONTENTS[number]),
                status="current" if number == 3 else "archived",
                created_at=datetime(2026, 11, 15, number, 0, 0),
            )
        )


# ── 成功路径 ──


def test_rollback_answers_with_the_envelope_and_leaves_an_enqueued_rebuild(
    api: SimpleNamespace,
) -> None:
    """回滚受理的回执 + 库里的真实状态 + 投递载荷（带目标稿号、在提交之后）。"""

    _seed_three_drafts(api)

    response = api.client.post(ROLLBACK_URL.format(version=1))

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0, body
    assert body["data"] == {
        "document_id": DOC_ID,
        "version": 1,
        "previous_version": 3,
        "status": DocumentStatus.PROCESSING,
    }

    assert api.rows() == [(1, "current"), (2, "archived"), (3, "archived")]
    doc = api.document()
    assert doc.current_version == 1
    assert doc.content == CONTENTS[1]
    assert doc.status == DocumentStatus.PROCESSING

    assert [call["kwargs"]["doc_version"] for call in api.publisher.calls] == [1]
    assert api.publisher.calls[0]["task_id"] == f"doc:{DOC_ID}"
    assert api.publisher.seen == [{"pointer": 1}], "投递发生在提交之后：它读到的已经是新指针"


# ── 四种拒绝 ──


def test_rolling_back_to_the_current_draft_answers_20032(api: SimpleNamespace) -> None:
    _seed_three_drafts(api)

    body = api.client.post(ROLLBACK_URL.format(version=3)).json()

    assert body["code"] == int(Codes.DOCUMENT_VERSION_ALREADY_ACTIVE) == 20032
    assert body["data"] == {"version": 3}
    assert api.publisher.calls == [], "被拒的回滚不投递重建"
    assert api.rows() == [(1, "archived"), (2, "archived"), (3, "current")]


def test_a_processing_document_answers_20025(api: SimpleNamespace) -> None:
    from app.db.base import now

    _seed_three_drafts(api)
    doc = Document(
        id=DOC_ID,
        tenant_id=TENANT_ID,
        kb_id="kb-1",
        title="积分规则",
        content=CONTENTS[3],
        status=DocumentStatus.PROCESSING,
        current_version=3,
        index_claimed_at=now(),
    )
    api.seed(doc)

    body = api.client.post(ROLLBACK_URL.format(version=1)).json()

    assert body["code"] == int(Codes.DOCUMENT_IS_PROCESSING) == 20025
    assert api.publisher.calls == []
    assert api.document().current_version == 3


def test_a_purged_draft_answers_20030(api: SimpleNamespace) -> None:
    _seed_three_drafts(api)
    api.drop_version(1)

    body = api.client.post(ROLLBACK_URL.format(version=1)).json()

    assert body["code"] == int(Codes.DOCUMENT_VERSION_NOT_FOUND) == 20030
    assert body["data"] == {"version": 1}
    assert api.publisher.calls == []
    assert api.document().current_version == 3


def test_a_cross_tenant_document_answers_not_found(api: SimpleNamespace) -> None:
    """跨租户一律「不存在」：不返回「无权限」，不泄露资源存在性。"""

    _seed_three_drafts(api)
    api.set_tenant("tenant-b")

    body = api.client.post(ROLLBACK_URL.format(version=1)).json()

    assert body["code"] == int(Codes.NOT_FOUND) == 20016
    assert api.document().current_version == 3


def test_a_non_integer_version_is_a_parameter_error_not_a_500(api: SimpleNamespace) -> None:
    """路径里的稿号不是整数 → 既有的 20002（参数校验失败），不是 500。"""

    _seed_three_drafts(api)

    body = api.client.post(ROLLBACK_URL.format(version="latest")).json()

    assert body["code"] == int(Codes.REQUEST_VALIDATION_ERROR) == 20002
    assert api.document().current_version == 3
