"""#176 · 按版本只读直查：在某一稿上检索，**不改任何东西**。

这条路径是排查「旧版明明写了、新版为什么召回不到」的唯一工具，因此它的契约非常窄：

* **只读**：不动当前稿指针、不重建、不写归属索引（票面明写，且它是排查工具，不能有副作用）；
* **锁定「某篇某稿」**：向量查询绕过「只看当前稿」谓词（``current_draft_filter``），
  直接按 ``(document_id, doc_version)`` 取片；
* **只走向量路**：关键词索引里只有 ``is_current`` 一个布尔，历史稿切片的标记是 ``False``；
  要查历史稿得给 ES 再加一个「指定稿号」谓词——那超出本票范围（ADR / 票面 #176 第 5 条）。
  所以**混合检索的等价物在这里不存在**，回包与代码注释都要说清这一点，别让它被误认为
  「关键词路也生效」；
* **默认路径一个字不改**：不传稿号时检索仍然只看当前稿（本文件有一条回归守卫）。

验收项与用例的对应关系：

* 同一 query 在 v1 与 v3 上分别查 → 稿号恒为 1 与 3；查完指针与状态不变；
* 直查回包带「是否当前稿」；
* 稿不存在 → 20030（不做墓碑）、跨租户 → 20016。
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ErrorCode
from app.models.chunk import CHUNK_TYPE_REFERENCE_POINTER, Chunk, current_draft_filter
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.providers.vectorstores.pgvector import PgVectorStore
from app.services import version_search
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
DOC_ID = "doc-1"
KB_ID = "kb-1"
SEARCH_URL = f"/api/v1/documents/{DOC_ID}/versions/{{version}}/search"
CONTENTS = {
    1: "第 1 稿正文：退款流程见附件。",
    2: "第 2 稿正文：退款流程见附件，另需工单号。",
    3: "第 3 稿正文：退款流程改为线上申请。",
}


#: 临时库里的 ``chunks`` 表：真表带 PG 的 JSONB，SQLite 渲不出来，所以用裸 DDL。
CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSON NOT NULL,
    index_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    doc_version INTEGER NOT NULL DEFAULT '1',
    generation INTEGER NOT NULL DEFAULT 1
)
"""


def _checksum(text: str) -> str:
    from app.utils.checksum import checksum_of

    return checksum_of(text)


def _compiled(statement: Any) -> Any:
    """按 PostgreSQL 方言编译，**不渲染字面量**。

    向量列是 ``Vector(1536)``，SQLAlchemy 没有它的字面量处理器（``literal_binds`` 会炸），
    而这里要断言的也正是列与列的**比较**形状与绑定参数——值从 ``compiled.params`` 读，
    不靠字符串拼。与 ``tests/test_retrieval_current_draft.py`` 同一手法。
    """

    return statement.compile(dialect=postgresql.dialect())


def _sql(statement: Any) -> str:
    return " ".join(str(_compiled(statement)).split())


class _NoRows:
    def all(self) -> list[Any]:
        return []


class _CaptureSession:
    """只收语句、不连库的会话替身（pgvector 的 ``Vector`` 在 SQLite 上建不出表）。"""

    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> _NoRows:
        self.statement = statement
        return _NoRows()


# ── 第 ① 层：语句形状 ──


def test_the_locked_query_is_pinned_to_one_document_and_one_draft() -> None:
    """直查的 SQL 按 ``(document_id, doc_version)`` 收窄，**且不带「只看当前稿」谓词**。

    这就是「绕过当前稿谓词」的实现形态：带上它，查历史稿永远是空结果（历史稿切片的
    ``doc_version`` 与指针不相等），而那看起来完全像「这一稿没内容」。
    """

    session = _CaptureSession()
    asyncio.run(
        PgVectorStore().similarity_search_document_version(
            session,  # type: ignore[arg-type]
            [0.1, 0.2, 0.3],
            tenant_id=TENANT_ID,
            document_id=DOC_ID,
            doc_version=3,
            generation=2,
            top_k=7,
        )
    )

    compiled = _compiled(session.statement)
    sql = " ".join(str(compiled).split())

    assert "chunks.document_id = " in sql
    assert "chunks.doc_version = " in sql
    assert "chunks.generation = " in sql, "按代收窄：同一稿可能并存多代（旧代留着给回滚）"
    assert "chunks.tenant_id = " in sql, "租户必须在条件里（ADR-0006）"
    assert "documents.current_version" not in sql, "直查必须绕过「只看当前稿」谓词"

    # 绑定参数就是「这一篇、这一稿、这一版」：条件只是形状，值同样是契约。
    bound = compiled.params
    assert DOC_ID in bound.values()
    assert TENANT_ID in bound.values()
    assert 3 in bound.values()
    assert 2 in bound.values(), "代也是值的一部分：不是形状对就算"
    assert 7 in bound.values(), "LIMIT 来自 top_k"
    assert CHUNK_TYPE_REFERENCE_POINTER in bound.values(), (
        "路标切片在默认召回里被排除，直查也不该放回来"
    )


def test_the_default_recall_path_still_only_sees_the_current_draft() -> None:
    """默认路径语义不变：不传稿号时，召回仍然只看当前稿（#174 的谓词还在）。

    这条是**回归守卫**：给向量侧加一条「锁定某篇某稿」的路径时，最危险的改法是顺手把
    ``current_draft_filter`` 从默认查询里拿掉——那样两稿切片会一起进 TopK，而结果看起来
    完全正常（同一段内容出现两份）。
    """

    session = _CaptureSession()
    asyncio.run(
        PgVectorStore().similarity_search_scope(
            session,  # type: ignore[arg-type]
            [0.1, 0.2, 0.3],
            tenant_id=TENANT_ID,
            kb_ids=[KB_ID],
            top_k=5,
        )
    )

    assert "chunks.doc_version = documents.current_version" in _sql(session.statement)
    assert _sql(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(current_draft_filter())
    ).endswith("chunks.doc_version = documents.current_version"), "谓词本身只有一个来源"


def test_the_read_only_path_never_imports_the_keyword_engine() -> None:
    """直查只走向量路：这条路径**不 import** 关键词引擎（#176 的边界）。

    为什么用「import 表」而不是「调用次数」：这个边界是设计约束（关键词索引里只有
    ``is_current`` 一个布尔，查历史稿需要给 ES 再加一个「指定稿号」谓词，超出本票范围）。
    一旦有人在服务里接上关键词路，这里会立刻红——而症状本来极难察觉。

    **归属索引从 #208 起不再被这条边界排除**：直查要记账（``endpoint=version_search``、
    三本账全 0），所以 ``app.services.retrieval_trace`` 出现在 import 表里是设计的一部分。
    它记的仍然**不是一次检索**——见下面的 ``test_a_version_search_writes_one_ledger_row``。
    """

    source = Path(version_search.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not any("keyword_search" in name for name in imported), imported


# ── 第 ② 层：接口边界（ASGI）──


class _StubEmbeddings:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]

    async def embed_texts(self, _texts: list[str]) -> list[list[float]]:
        raise AssertionError("直查只向量化问句，不该走文档侧的批量接口")


class _StubVectorStore:
    """记账版向量存储：**回显被锁定的稿号**，与真实现的 SQL 语义一致。

    「稿号恒为 1 与 3」由两部分合起来钉：真正的筛选在 SQL 里
    （``test_the_locked_query_is_pinned_to_one_document_and_one_draft``），这里钉
    「服务确实按请求的稿号去问、并且把回来的稿号原样交给调用方」。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def similarity_search_document_version(
        self,
        _db: Any,
        _query_vector: list[float],
        *,
        tenant_id: str,
        document_id: str,
        doc_version: int,
        generation: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "doc_version": doc_version,
                "generation": generation,
                "top_k": top_k,
            }
        )
        return [
            {
                "chunk_id": f"chunk-{doc_version}-{index}",
                "kb_id": KB_ID,
                "document_id": document_id,
                "doc_version": doc_version,
                "title": "积分规则",
                "content": f"第 {doc_version} 稿的第 {index} 片",
                "score": 0.9 - index * 0.1,
            }
            for index in range(2)
        ]


def _seed(engine: Any) -> None:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title="积分规则",
                content=CONTENTS[2],
                status=DocumentStatus.SUCCESS,
                # 指针停在**第 2 稿**：于是 v1/v3 都是历史稿，v1 与 v3 的「是否当前稿」
                # 分别是 False / False——再补一次指针在 1 的场景由下一个用例覆盖。
                current_version=2,
            )
        )
        # 每稿两代：旧代（1）+ 最新一代（2）。按稿直查必须取 **2**，取 1 就是旧切法。
        for number in (1, 2, 3):
            for generation in (1, 2):
                session.execute(
                    text(
                        "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, "
                        "embedding, metadata, index_version, doc_version, generation) VALUES "
                        "(:id, :tenant, :kb, :doc, :content, '[]', '{}', 'v1', :draft, :gen)"
                    ),
                    {
                        "id": f"c-v{number}-g{generation}",
                        "tenant": TENANT_ID,
                        "kb": KB_ID,
                        "doc": DOC_ID,
                        "content": CONTENTS[number],
                        "draft": number,
                        "gen": generation,
                    },
                )
        for number in (1, 2, 3):
            session.add(
                DocumentVersion(
                    id=f"v{number}",
                    document_id=DOC_ID,
                    version=number,
                    content=CONTENTS[number],
                    checksum=_checksum(CONTENTS[number]),
                    status="current" if number == 2 else "archived",
                    created_at=datetime(2026, 11, 15, number, 0, 0),
                )
            )
        session.commit()


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """离线接缝：SQLite 文件 + 记账版向量存储 + 身份可切换的当前 Key。"""

    db_path = tmp_path / "docver-search.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    sync_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Document.__table__.create(sync_engine)
    DocumentVersion.__table__.create(sync_engine)
    # 按稿直查现在要问「这一稿现存最高的代」——那是一次真实 SQL，所以临时库里必须有
    # chunks 表。用裸 DDL 而不是 ``Chunk.__table__.create``：真表的 ``metadata`` 列是
    # PG 的 JSONB，SQLite 渲不出来（同 tests/support/sqlite_schema.py 的做法）。
    with sync_engine.begin() as connection:
        connection.execute(text(CHUNKS_DDL))
    _seed(sync_engine)

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    dispatch = importlib.import_module("app.services.index_dispatch")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    store = _StubVectorStore()
    embeddings = _StubEmbeddings()

    async def _db() -> Any:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine) as session:
                yield session
        finally:
            await engine.dispose()

    monkeypatch.setattr(version_search, "get_vector_store", lambda: store)
    monkeypatch.setattr(version_search, "get_embedding_provider", lambda: embeddings)

    identity: dict[str, DevKeyContext] = {"key": DevKeyContext(tenant_id=TENANT_ID)}
    main.app.dependency_overrides[async_session_mod.get_async_db] = _db
    main.app.dependency_overrides[get_current_key] = lambda: identity["key"]
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                store=store,
                embeddings=embeddings,
                publisher=dispatch.get_task_publisher(),
                set_tenant=lambda tenant_id: identity.update(
                    key=DevKeyContext(tenant_id=tenant_id)
                ),
                document=lambda: _document(sync_engine),
                versions=lambda: _versions(sync_engine),
                set_pointer=lambda version: _set_pointer(sync_engine, version),
            )
    finally:
        main.app.dependency_overrides.clear()
        sync_engine.dispose()


def _document(engine: Any) -> SimpleNamespace:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        doc = session.get(Document, DOC_ID)
        assert doc is not None
        return SimpleNamespace(
            current_version=doc.current_version,
            status=doc.status,
            content=doc.content,
        )


def _versions(engine: Any) -> list[tuple[int, str]]:
    factory = sessionmaker(bind=engine)
    with factory() as session:
        return [
            (row.version, row.status)
            for row in session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == DOC_ID)
                .order_by(DocumentVersion.version)
            ).scalars()
        ]


def _set_pointer(engine: Any, version: int) -> None:
    """把指针挪到某一稿（连同版本行的 current / archived 身份一起，保持不变式）。"""

    factory = sessionmaker(bind=engine)
    with factory() as session:
        doc = session.get(Document, DOC_ID)
        assert doc is not None
        doc.current_version = version
        for row in session.execute(
            select(DocumentVersion).where(DocumentVersion.document_id == DOC_ID)
        ).scalars():
            row.status = "current" if row.version == version else "archived"
        session.commit()


def test_searching_two_drafts_with_the_same_query_returns_each_draft_and_leaves_state_alone(
    api: SimpleNamespace,
) -> None:
    """同一 query 在 v1 与 v3 上分别查：稿号恒为 1 与 3，指针 / 状态 / 版本行全不变。"""

    before = api.document()
    versions_before = api.versions()

    first = api.client.post(SEARCH_URL.format(version=1), json={"query": "退款流程"})
    second = api.client.post(SEARCH_URL.format(version=3), json={"query": "退款流程"})

    assert first.status_code == second.status_code == 200
    body_one, body_three = first.json(), second.json()
    assert body_one["code"] == body_three["code"] == 0, (body_one, body_three)

    assert [item["doc_version"] for item in body_one["data"]["retrieved_chunks"]] == [1, 1]
    assert [item["doc_version"] for item in body_three["data"]["retrieved_chunks"]] == [3, 3]
    assert [item["chunk_id"] for item in body_one["data"]["retrieved_chunks"]] == [
        "chunk-1-0",
        "chunk-1-1",
    ]

    assert body_one["data"]["version"] == 1
    assert body_three["data"]["version"] == 3
    assert body_one["data"]["document_id"] == DOC_ID

    # 服务确实按请求的稿号去问向量存储，一次一个稿号（不是「查完再过滤」）。
    assert [call["doc_version"] for call in api.store.calls] == [1, 3]
    assert {call["document_id"] for call in api.store.calls} == {DOC_ID}
    # 每一稿问的都是**这一稿现存最高的代**（种子数据里两稿都是第 2 代）。
    assert [call["generation"] for call in api.store.calls] == [2, 2]
    assert api.embeddings.queries == ["退款流程", "退款流程"]

    # 只读：指针、状态、版本行一个字节都不动，也没有任何重建被投递。
    assert api.document().__dict__ == before.__dict__
    assert api.versions() == versions_before
    assert api.publisher.calls == []


def test_the_answer_says_whether_the_searched_draft_is_the_current_one(
    api: SimpleNamespace,
) -> None:
    """「是否当前稿」是直查回包的一部分：指针停在 2，于是 v2 为真、v1 / v3 为假。"""

    pointer = api.document().current_version
    assert pointer == 2

    def _ask(version: int) -> dict[str, Any]:
        body = api.client.post(SEARCH_URL.format(version=version), json={"query": "退款"}).json()
        assert body["code"] == 0, body
        return body["data"]

    assert _ask(2)["is_current"] is True
    assert {item["doc_version"] for item in _ask(2)["retrieved_chunks"]} == {2}
    assert _ask(1)["is_current"] is False
    assert _ask(3)["is_current"] is False


def test_the_current_draft_and_an_archived_one_answer_with_their_own_draft_numbers(
    api: SimpleNamespace,
) -> None:
    """票面那条：指针在 3 时，同一 query 在 v1 与 v3 上分别查 → 稿号恒为 1 与 3。

    这一条与上一条的区别是「有一侧是当前稿」：默认召回能查到的那一稿，直查也必须查到，
    且**不是**靠当前稿谓词查到的（谓词只在默认路径上）。
    """

    api.set_pointer(3)
    before = api.document()

    historical = api.client.post(SEARCH_URL.format(version=1), json={"query": "退款"}).json()
    current = api.client.post(SEARCH_URL.format(version=3), json={"query": "退款"}).json()

    assert historical["code"] == current["code"] == 0
    assert [item["doc_version"] for item in historical["data"]["retrieved_chunks"]] == [1, 1]
    assert [item["doc_version"] for item in current["data"]["retrieved_chunks"]] == [3, 3]
    assert historical["data"]["is_current"] is False
    assert current["data"]["is_current"] is True

    assert api.document().__dict__ == before.__dict__
    assert [call["doc_version"] for call in api.store.calls] == [1, 3]


def test_an_unknown_draft_is_not_found(api: SimpleNamespace) -> None:
    """稿不存在（含已被保留策略物理删除的那一稿）→ 20030，且不去查向量库。"""

    from app.core.exceptions import ErrorCode as Codes

    body = api.client.post(SEARCH_URL.format(version=9), json={"query": "退款"}).json()

    assert body["code"] == int(Codes.DOCUMENT_VERSION_NOT_FOUND) == 20030
    assert body["data"] == {"version": 9}
    assert api.store.calls == [], "稿不存在就不该去查向量库"


def test_a_cross_tenant_document_is_not_found(api: SimpleNamespace) -> None:
    from app.core.exceptions import ErrorCode as Codes

    api.set_tenant("tenant-b")

    body = api.client.post(SEARCH_URL.format(version=1), json={"query": "退款"}).json()

    assert body["code"] == int(Codes.NOT_FOUND) == 20016
    assert api.store.calls == []


def test_a_blank_query_is_a_parameter_error(api: SimpleNamespace) -> None:
    from app.core.exceptions import ErrorCode as Codes

    body = api.client.post(SEARCH_URL.format(version=1), json={"query": "   "}).json()

    assert body["code"] == int(Codes.REQUEST_VALIDATION_ERROR) == 20002
    assert api.store.calls == []


def test_a_vector_store_failure_is_a_retrieval_failure_not_a_500(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """向量引擎故障属于「外部服务请求失败」（50002），不是 500。"""

    from app.core.exceptions import ErrorCode as Codes

    class _Broken:
        async def similarity_search_document_version(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("boom: <html>upstream says 502</html>")

    monkeypatch.setattr(version_search, "get_vector_store", lambda: _Broken())

    body = api.client.post(SEARCH_URL.format(version=1), json={"query": "退款"}).json()

    assert body["code"] == int(Codes.RETRIEVAL_FAILED) == 50002
    assert "html" not in body["msg"], "外部响应体不进回包（日志同样只记类型）"


# ── 第 ③ 层：记账（#208）──


def _captured_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把账本写入换成捕获：``version_search`` 只碰这一个符号，替身就够。"""

    rows: list[dict[str, Any]] = []

    async def _capture(**kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(version_search, "record_retrieval_trace", _capture)
    return rows


def test_a_successful_version_search_writes_one_ledger_row(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次成功的直查要留一行账：``endpoint=version_search``、**三本账全 0**、码为 0。

    三本账全 0 是这条契约的重点：直查不扣任何额度（它不过配额门禁），把它记成一次
    ``retrieve`` 才会污染统计。留痕只为回答「谁在拿 Key 白调 embedding」（#208）。
    """

    rows = _captured_rows(monkeypatch)

    body = api.client.post(SEARCH_URL.format(version=1), json={"query": "退款"}).json()

    assert body["code"] == 0
    assert len(rows) == 1, "一次调用一行，不许多写也不许漏"
    row = rows[0]
    assert row["endpoint"] == "version_search"
    assert row["code"] == 0
    assert (row["meter_r"], row["meter_rr"], row["meter_llm"]) == (0, 0, 0)
    assert row["kb_ids"] == [KB_ID]
    # 归属索引这一列现在没有库级答案可记（按稿直查不落在某一版构建上），落的是空映射。
    assert not row["index_versions"], "按稿直查不属于任何一版构建"
    assert row["doc_versions"] == {DOC_ID: 1}
    assert row["chunk_ids"] == ["chunk-1-0", "chunk-1-1"]
    assert row["top_k"] == version_search.VERSION_SEARCH_TOP_K


def test_a_failed_version_search_still_writes_a_row_with_its_business_code(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败也要留一行，码是这次的真实结局（这里是「稿不存在」20030）。

    只记成功的账会给出「一直没有失败」的假象：一个写错稿号的调用方会反复撞 20030，
    而账本上一个字都看不到。
    """

    rows = _captured_rows(monkeypatch)

    body = api.client.post(SEARCH_URL.format(version=99), json={"query": "退款"}).json()

    assert body["code"] == ErrorCode.DOCUMENT_VERSION_NOT_FOUND
    assert len(rows) == 1
    assert rows[0]["code"] == ErrorCode.DOCUMENT_VERSION_NOT_FOUND
    assert (rows[0]["meter_r"], rows[0]["meter_rr"], rows[0]["meter_llm"]) == (0, 0, 0)
