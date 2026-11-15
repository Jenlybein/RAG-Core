"""#175 · 建版本 + 列版本：文档版本管理的第一条用户可见路径。

本文件守的是**接口边界**（第 ② 层，ASGI）：TestClient 发真实请求，只看统一响应体与库里
留下的行。数据库用**同一个 SQLite 文件**、每个请求各开一个异步引擎（``get_async_db`` 的
依赖覆盖在事件循环里现建）——于是「事务提交了吗」「唯一约束撞出来了吗」都是真的，
而不是替身会话点过头就算。

投递缝换成记账器（``app.services.index_dispatch.set_task_publisher``）：既不需要 Broker，
又能在**投递那一刻**回读数据库，于是「重建在提交之后才投递（不会抢跑）」可以被直接证明。

验收项与用例的对应关系（票面顺序）：

* 提交新内容 → 新稿次 + 旧稿归档 + 指针前移 + 文档置处理中，重建在提交**之后**才投递；
* 提交与当前稿相同 → 「内容未变」（20031），版本行数不变，**什么都不写**；
* 提交与历史某稿相同 → 建**新**稿次，不复活旧行；
* 两个并发提交不会得到同一稿号，撞唯一约束映射成「请重试」（20029）而不是 500；
* 文档处理中时拒绝写版本（复用 20025，不新造 ``DOCUMENT_BUSY``）；
* 列版本：按稿号倒序、默认不带正文、正文需显式要求。
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

from app.core.config import settings
from app.core.exceptions import ErrorCode as Codes
from app.db.base import now
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.repositories import document_version_repository
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
DOC_ID = "doc-1"
OLD_CONTENT = "第 1 稿正文：退款流程见附件。"
NEW_CONTENT = "第 2 稿正文：退款流程见附件，另需工单号。"
VERSIONS_URL = f"/api/v1/documents/{DOC_ID}/versions"


def test_the_four_new_version_error_codes_are_the_agreed_numbers() -> None:
    """错误码是**线上词汇**（响应体里就是这个整数），所以期望值写成字面量。

    20032 本票用不到（「已经是当前稿」属于 #176 的回滚 / 激活语义），但词汇位先占好：
    之后再补一个码时，「20032 是什么」不应该有第二种答案。
    """

    assert int(Codes.DOCUMENT_VERSION_CONFLICT) == 20029
    assert int(Codes.DOCUMENT_VERSION_NOT_FOUND) == 20030
    assert int(Codes.DOCUMENT_VERSION_UNCHANGED) == 20031
    assert int(Codes.DOCUMENT_VERSION_ALREADY_ACTIVE) == 20032


def test_the_version_codes_do_not_reuse_the_existing_document_codes() -> None:
    """「文档处理中」复用既有的 20025，**不**新造 ``DOCUMENT_BUSY``。

    一个含义一个码：再造一个「忙」的码，调用方就得靠「两个码都要处理」才不误判，
    而漏掉一个的症状是「偶发 20029 之外的未知码」。
    """

    assert int(Codes.DOCUMENT_IS_PROCESSING) == 20025
    assert not hasattr(Codes, "DOCUMENT_BUSY")


# ────────────────────────────────────────── 离线接缝：真库（SQLite 文件）+ 记账投递


class _DbObservingPublisher:
    """记账投递器：**在投递的那一刻**回读数据库，记下它看到的指针与稿次。

    这是「重建不会抢跑」唯一能证明的地方：投递发生在 commit 之后 → 它读到的是新稿；
    若投递发生在 commit 之前（先投递再提交），读到的是旧稿。只断言「投递被调用了」
    看不出这个先后。
    """

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
                versions = [
                    row[0]
                    for row in connection.execute(
                        select(DocumentVersion.version)
                        .where(DocumentVersion.document_id == kwargs["document_id"])
                        .order_by(DocumentVersion.version)
                    ).all()
                ]
        finally:
            engine.dispose()
        self.seen.append({"pointer": pointer, "versions": versions})


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """离线接缝：一个 SQLite 文件、真表、按需播种，以及可改身份的当前 Key。"""

    db_path = tmp_path / "docver.db"
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
        # 引擎建在**事件循环里**（TestClient 的 portal 循环）：aiosqlite 的连接绑定循环，
        # 在用例线程里建好再交进去会在第一次 await 上炸。
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine) as session:
                yield session
        finally:
            await engine.dispose()

    identity: dict[str, DevKeyContext] = {
        "key": DevKeyContext(tenant_id=TENANT_ID),
    }
    monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(tmp_path / "uploads"))

    main.app.dependency_overrides[async_session_mod.get_async_db] = _db
    main.app.dependency_overrides[get_current_key] = lambda: identity["key"]
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                publisher=publisher,
                db_path=db_path,
                set_tenant=lambda tenant_id: identity.update(
                    key=DevKeyContext(tenant_id=tenant_id)
                ),
                seed=lambda *rows: _seed(sync_engine, *rows),
                rows=lambda document_id=DOC_ID: _versions(sync_engine, document_id),
                document=lambda: _document(sync_engine),
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
        return session.execute(select(Document).where(Document.id == DOC_ID)).scalar_one()


def _versions(engine: Any, document_id: str) -> list[tuple[int, str, str | None]]:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        rows = session.execute(
            select(DocumentVersion.version, DocumentVersion.status, DocumentVersion.content)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version)
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def _doc_row(
    *,
    document_id: str = DOC_ID,
    tenant_id: str = TENANT_ID,
    current_version: int = 1,
    content: str = OLD_CONTENT,
    status: int = DocumentStatus.SUCCESS,
    claimed_at: datetime | None = None,
) -> Document:
    return Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id="kb-1",
        title="积分规则",
        content=content,
        status=status,
        current_version=current_version,
        index_claimed_at=claimed_at,
    )


def _version_row(
    *,
    version_id: str,
    version: int,
    content: str,
    status: str = "current",
    document_id: str = DOC_ID,
    source_file_path: str | None = None,
) -> DocumentVersion:
    from app.utils.checksum import checksum_of

    return DocumentVersion(
        id=version_id,
        document_id=document_id,
        version=version,
        content=content,
        checksum=checksum_of(content),
        status=status,
        source_file_path=source_file_path,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )


def _one_version_ready(api: SimpleNamespace) -> None:
    """一篇已索引成功的文档：第 1 稿是当前稿，正文与指针一致。"""

    api.seed(
        _doc_row(),
        _version_row(version_id="v1", version=1, content=OLD_CONTENT),
    )


def _submit(api: SimpleNamespace, content: str = NEW_CONTENT):
    return api.client.post(VERSIONS_URL, json={"content": content})


# ── 提交新内容 ──


def test_submitting_new_content_returns_the_next_draft(api: SimpleNamespace) -> None:
    """新稿次：200 + code 0，回执里带新稿号与上一稿，文档被置处理中。"""

    _one_version_ready(api)

    response = _submit(api)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0
    assert body["data"]["document_id"] == DOC_ID
    assert body["data"]["version"] == 2
    assert body["data"]["previous_version"] == 1
    assert body["data"]["status"] == DocumentStatus.PROCESSING
    assert len(body["data"]["checksum"]) == 64

    assert api.rows() == [(1, "archived", OLD_CONTENT), (2, "current", NEW_CONTENT)]
    doc = api.document()
    assert doc.current_version == 2
    assert doc.content == NEW_CONTENT
    assert doc.status == DocumentStatus.PROCESSING
    assert doc.index_claimed_at is None, "排队不盖租约：Worker 才是唯一抢占者"


def test_the_rebuild_is_dispatched_only_after_the_commit(api: SimpleNamespace) -> None:
    """重建在提交**之后**才投递：投递那一刻回读数据库，看到的是已经落地的新稿。

    先投递再提交的写法在这里读到的是第 1 稿——Worker 会拿一份还没提交的文档去重建，
    而症状是「偶发重建了旧内容」，不报错。
    """

    _one_version_ready(api)

    _submit(api)

    assert len(api.publisher.calls) == 1
    call = api.publisher.calls[0]
    assert call["task_id"] == f"doc:{DOC_ID}"
    assert call["kwargs"]["doc_version"] == 2, "投递载荷里带着这次要重建的稿号"
    assert api.publisher.seen == [{"pointer": 2, "versions": [1, 2]}], (
        "投递发生在提交之前：任务看到的是旧指针 / 少一条版本行"
    )


def test_nothing_is_dispatched_when_the_submission_is_rejected(api: SimpleNamespace) -> None:
    """被拒的提交不投递：副作用绑在**提交**上，不是绑在「试着写过」上。"""

    _one_version_ready(api)

    _submit(api, content=OLD_CONTENT)

    assert api.publisher.calls == []


def test_the_keyword_current_draft_flip_is_also_registered_after_the_commit(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键词索引的「当前稿」翻转也排在这一笔提交之后，而且带的是**新稿号**。

    不翻的话，指针已经前移、旧稿切片却仍带着 ``is_current=true``：关键词路会继续召回旧稿，
    而向量那一路已经只看新稿——两路对同一稿给出不同答案，症状是「同一段内容出现两次」。
    """

    _one_version_ready(api)
    flips: list[dict[str, Any]] = []
    keyword_draft = importlib.import_module("app.services.keyword_draft")

    async def _record(*, document_id: str, tenant_id: str, doc_version: int) -> None:
        flips.append(
            {"document_id": document_id, "tenant_id": tenant_id, "doc_version": doc_version}
        )

    monkeypatch.setattr(keyword_draft, "flip_current_draft_after_commit", _record)

    _submit(api)

    assert flips == [{"document_id": DOC_ID, "tenant_id": TENANT_ID, "doc_version": 2}]


def test_the_flip_is_not_scheduled_when_the_submission_is_rejected(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被拒的提交连翻转都不登记：它挂在提交上。"""

    _one_version_ready(api)
    flips: list[dict[str, Any]] = []
    keyword_draft = importlib.import_module("app.services.keyword_draft")

    async def _record(**_kwargs: Any) -> None:
        flips.append(_kwargs)

    monkeypatch.setattr(keyword_draft, "flip_current_draft_after_commit", _record)

    _submit(api, content=OLD_CONTENT)

    assert flips == []


# ── 内容未变 ──


def test_resubmitting_the_current_draft_reports_unchanged(api: SimpleNamespace) -> None:
    """与当前稿相同 → HTTP 200 + 20031「内容未变」，版本行数不变，什么都不写。"""

    _one_version_ready(api)

    response = _submit(api, content="  第 1 稿正文：退款流程见附件。\r\n")

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.DOCUMENT_VERSION_UNCHANGED)
    assert body["data"] == {"version": 1}

    assert api.rows() == [(1, "current", OLD_CONTENT)]
    doc = api.document()
    assert doc.status == DocumentStatus.SUCCESS, "什么都没写：状态也不该被改成处理中"
    assert doc.current_version == 1


# ── 与历史某稿相同 ──


def test_resubmitting_an_archived_draft_creates_a_new_draft(api: SimpleNamespace) -> None:
    """与**历史**某稿相同 → 新稿次（第 3 稿），旧行原样留在归档里，不复活。"""

    api.seed(
        _doc_row(current_version=2, content=NEW_CONTENT),
        _version_row(version_id="v1", version=1, content=OLD_CONTENT, status="archived"),
        _version_row(version_id="v2", version=2, content=NEW_CONTENT),
    )

    response = _submit(api, content=OLD_CONTENT)

    assert response.json()["data"]["version"] == 3
    assert api.rows() == [
        (1, "archived", OLD_CONTENT),
        (2, "archived", NEW_CONTENT),
        (3, "current", OLD_CONTENT),
    ]


# ── 冲突 ──


def test_a_draft_number_collision_is_reported_as_retry_not_500(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撞唯一约束 → HTTP 200 + 20029「请重试」，而不是 500。

    **怎么造出来的**：真 PostgreSQL 上两个提交由文档行锁串行化，撞不上唯一约束；撞得上的是
    「读 max 读到旧快照、另一个人已经写进去了」那个窗口。这里把 ``max_version`` 的返回值
    钉成旧快照（1），而库里已经有第 2 稿（竞争者的）——于是请求 ``flush`` 时撞的是**真实的**
    ``(document_id, version)`` 唯一约束，异常一路走到服务的映射分支。
    """

    api.seed(
        _doc_row(current_version=2, content="竞争者的第 2 稿正文。"),
        _version_row(version_id="v1", version=1, content=OLD_CONTENT, status="archived"),
        _version_row(version_id="v2", version=2, content="竞争者的第 2 稿正文。"),
    )

    async def _stale_max(*_args: Any, **_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(document_version_repository, "max_version", _stale_max)

    response = _submit(api)

    body = response.json()
    assert response.status_code == 200, "冲突是可重试的业务失败，不是 5xx"
    assert body["code"] == int(Codes.DOCUMENT_VERSION_CONFLICT)
    assert "重试" in body["msg"]

    # 整笔回滚：竞争者的第 2 稿还在，没有第三条写进来，指针没有被挪，也没有投递。
    assert api.rows() == [
        (1, "archived", OLD_CONTENT),
        (2, "current", "竞争者的第 2 稿正文。"),
    ]
    assert api.document().current_version == 2
    assert api.publisher.calls == []


# ── 守卫 ──


def test_a_processing_document_refuses_new_versions(api: SimpleNamespace) -> None:
    """处理中（租约新鲜）→ 20025，复用既有的「文档处理中」。"""

    api.seed(
        _doc_row(status=DocumentStatus.PROCESSING, claimed_at=now()),
        _version_row(version_id="v1", version=1, content=OLD_CONTENT),
    )

    response = _submit(api)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.DOCUMENT_IS_PROCESSING)
    assert api.rows() == [(1, "current", OLD_CONTENT)]
    assert api.publisher.calls == []


def test_another_tenants_document_is_not_found(api: SimpleNamespace) -> None:
    """跨租户一律「不存在」（20016）：不返回「无权限」，不泄露存在性。"""

    _one_version_ready(api)
    api.set_tenant("tenant-b")

    response = _submit(api)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.NOT_FOUND)
    assert api.rows() == [(1, "current", OLD_CONTENT)]


# ── 入口形状（两种 content-type 共用一套 20002） ──


def test_a_multipart_submission_without_a_file_is_a_validation_error(
    api: SimpleNamespace,
) -> None:
    """multipart 却没带 ``file`` → 20002（与上传入口逐字相同的错误体）。"""

    _one_version_ready(api)

    response = api.client.post(VERSIONS_URL, files={"content": (None, NEW_CONTENT)})

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.REQUEST_VALIDATION_ERROR)
    assert body["data"]["errors"][0]["loc"] == ["body", "file"]


def test_an_invalid_json_body_is_a_validation_error(api: SimpleNamespace) -> None:
    """半截 JSON 也归入 20002，不许穿成 500。"""

    _one_version_ready(api)

    response = api.client.post(
        VERSIONS_URL,
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.REQUEST_VALIDATION_ERROR)
    assert body["data"]["errors"][0]["type"] == "json_invalid"


def test_an_unknown_field_is_rejected_instead_of_ignored(api: SimpleNamespace) -> None:
    """多传字段 → 20002：静默忽略「我传了 version」是最难查的那类分歧。"""

    _one_version_ready(api)

    response = api.client.post(
        VERSIONS_URL, json={"content": NEW_CONTENT, "version": 99}
    )

    assert response.json()["code"] == int(Codes.REQUEST_VALIDATION_ERROR)


def test_a_file_submission_is_parsed_and_keeps_its_own_source_copy(
    api: SimpleNamespace,
) -> None:
    """文件模式走既有解析入口；源文件按稿号落盘（不覆盖原始上传的那一份）。"""

    _one_version_ready(api)

    response = api.client.post(
        VERSIONS_URL,
        files={"file": ("notes.md", "# 第 2 稿\n\n退款需工单号。".encode(), "text/markdown")},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["data"]["version"] == 2
    assert api.rows()[1] == (2, "current", "# 第 2 稿\n\n退款需工单号。")

    stored = api.document().source_file_path
    assert stored is not None and ".v2." in stored
    assert (Path(settings.DOCUMENT_STORAGE_PATH) / stored).exists()


# ── 列版本 ──

def test_listing_versions_is_newest_first_and_hides_the_content_by_default(
    api: SimpleNamespace,
) -> None:
    """按稿号**倒序**；默认不带正文（一批整篇正文的响应没人想要），要正文得显式要求。"""

    api.seed(
        _doc_row(current_version=3, content="第三稿正文。"),
        _version_row(version_id="v1", version=1, content="第一稿正文。", status="archived"),
        _version_row(version_id="v2", version=2, content="第二稿正文。", status="archived"),
        _version_row(version_id="v3", version=3, content="第三稿正文。"),
    )

    response = api.client.get(VERSIONS_URL)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0
    data = body["data"]
    assert data["document_id"] == DOC_ID
    assert data["current_version"] == 3
    assert [item["version"] for item in data["items"]] == [3, 2, 1]
    assert [item["status"] for item in data["items"]] == ["current", "archived", "archived"]
    assert [item["content"] for item in data["items"]] == [None, None, None]
    # 字符数在「不给正文」时是唯一能回答「这一稿有多长」的字段（#177 加进共享形状）。
    assert [item["char_count"] for item in data["items"]] == [
        len("第三稿正文。"),
        len("第二稿正文。"),
        len("第一稿正文。"),
    ]


def test_listing_versions_returns_the_content_only_when_asked(api: SimpleNamespace) -> None:
    """``?include_content=true`` 才带正文；其余字段两种情况下逐字相同。"""

    api.seed(
        _doc_row(current_version=2, content="第二稿正文。"),
        _version_row(version_id="v1", version=1, content="第一稿正文。", status="archived"),
        _version_row(version_id="v2", version=2, content="第二稿正文。"),
    )

    response = api.client.get(f"{VERSIONS_URL}?include_content=true")

    items = response.json()["data"]["items"]
    assert [item["content"] for item in items] == ["第二稿正文。", "第一稿正文。"]
    assert items[0]["version"] == 2
    assert len(items[0]["checksum"]) == 64, "校验和两种情况下都给（判重与对账要用）"


def test_listing_versions_of_another_tenants_document_is_not_found(api: SimpleNamespace) -> None:
    """跨租户一律「不存在」（20016）：列表也不泄露存在性。"""

    _one_version_ready(api)
    api.set_tenant("tenant-b")

    response = api.client.get(VERSIONS_URL)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == int(Codes.NOT_FOUND)
    assert body["data"] is None


def test_listing_versions_of_a_document_without_rows_is_an_empty_list(
    api: SimpleNamespace,
) -> None:
    """#172 之前的上传、或还没索引成功的文档：空列表 + 当前指针，不是 20030。

    「这篇文档没有历史稿」是正常状态；20030 留给「指名要看某一稿、而那一稿不存在」
    （那是 #176 的按稿直查）。
    """

    api.seed(_doc_row(current_version=1))

    body = api.client.get(VERSIONS_URL).json()

    assert body["code"] == 0
    assert body["data"] == {"document_id": DOC_ID, "current_version": 1, "items": []}

def test_two_consecutive_submissions_get_consecutive_draft_numbers(
    api: SimpleNamespace,
) -> None:
    """连着提交两次 → 第 2、第 3 稿，各自一条当前稿行。

    稿号由 ``max(version) + 1`` 在**文档行锁内**算出来（见
    ``document_repository.lock_statement``）：后一个提交读到的 ``max`` 一定是前一个写下的
    那一行，于是两个提交永远不会算出同一个号。这条用例钉的是「串行化之后的样子」；
    「撞上了怎么办」由冲突那条用例覆盖。
    """

    _one_version_ready(api)

    first = _submit(api, content="第一次修改。").json()
    second = _submit(api, content="第二次修改。").json()

    assert first["code"] == 0
    assert second["code"] == 0, "刚提交过一稿的文档是「已排队未开工」，不是「处理中」"
    assert (first["data"]["version"], second["data"]["version"]) == (2, 3)
    assert second["data"]["previous_version"] == 2

    assert api.rows() == [
        (1, "archived", OLD_CONTENT),
        (2, "archived", "第一次修改。"),
        (3, "current", "第二次修改。"),
    ]
    assert api.document().current_version == 3

# ── 删文档时版本行的源文件也要走 ──


def test_deleting_the_document_also_removes_the_version_source_files(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版本行的源文件是按稿号落盘的（#175），删文档时必须一起清掉。

    不清的后果不是报错，而是一批永远无人认领的文件：文档行与版本行都没了，没有任何地方
    还记着这些路径——`rm -rf` 之外的清理方式都找不到它们。
    """

    documents_route = importlib.import_module("app.api.routes.documents")
    monkeypatch.setattr(documents_route, "build_chunk_stores", lambda *_a, **_k: [])

    storage = Path(settings.DOCUMENT_STORAGE_PATH)
    v1_file = storage / TENANT_ID / f"{DOC_ID}.v1.md"
    v2_file = storage / TENANT_ID / f"{DOC_ID}.v2.md"
    for path, body in ((v1_file, b"v1"), (v2_file, b"v2")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    api.seed(
        _doc_row(current_version=2, content=NEW_CONTENT, status=DocumentStatus.SUCCESS),
        _version_row(
            version_id="v1",
            version=1,
            content=OLD_CONTENT,
            status="archived",
            source_file_path=f"{TENANT_ID}/{DOC_ID}.v1.md",
        ),
        _version_row(
            version_id="v2",
            version=2,
            content=NEW_CONTENT,
            source_file_path=f"{TENANT_ID}/{DOC_ID}.v2.md",
        ),
    )

    response = api.client.delete(f"/api/v1/documents/{DOC_ID}")

    assert response.json()["code"] == 0
    assert not v1_file.exists(), "第 1 稿的源文件还在"
    assert not v2_file.exists(), "当前稿的源文件还在"
    assert api.rows() == [], "版本行随文档一起走"
