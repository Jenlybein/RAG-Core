"""#176 · 跨层：回滚之后这份文档**立刻又能被检索到**（票面第 2 条验收）。

回滚切的是指针，而检索按 ``chunks.doc_version = documents.current_version`` 过滤
（:func:`app.models.chunk.current_draft_filter`，唯一一处「当前稿」谓词）。于是「回滚之后
能检索到」等价于三件事同时成立：

1. 指针 == 目标稿（回滚这一笔写的）；
2. 目标稿的切片真的存在（Worker 重建写的）；
3. 那批切片每一片的 ``doc_version`` 都 == 指针（否则被谓词挡掉，检索为空）。

这条用例把 1 到 3 串起来跑一遍：ASGI 发一次真实回滚请求 → 用**真的**
``IndexingService.index_existing_document`` 跑一遍重建（切块器 / embedding / 存储换成
离线替身）→ 再检查落到存储里的切片与库里的指针。

## 为什么「检索非空」是拼起来钉的，而不是真跑一次检索

pgvector 的 ``Vector(1536)`` 在 SQLite 上建不出表（DDL 编不过），所以本仓库的检索侧
断言一律是**结构 + 数据**两条：谓词本身由
``tests/test_retrieval_current_draft.py`` 钉住（它编译真的 ``similarity_search_scope``
语句），这里钉「被谓词筛的那批行确实存在、且稿号 == 指针」。两边合起来就是
「检索非空且稿号全为 1」——不是用替身假装查了一次。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.chunk import DEFAULT_INDEX_VERSION, Chunk, current_draft_filter
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import PLAN_FREE, Plan
from app.models.tenant import Tenant
from app.repositories import knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece

TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
DOC_ID = "doc-rollback-e2e"

CONTENTS = {
    1: "第 1 稿正文：退款流程见附件。",
    2: "第 2 稿正文：退款流程见附件，另需工单号。",
    3: "第 3 稿正文：退款流程改为线上申请。",
}

PIECES = [
    SplitPiece(text="first piece of draft one", metadata={"heading_path": "退款"}),
    SplitPiece(text="second piece of draft one", metadata={"heading_path": "退款"}),
]


def _checksum(text: str) -> str:
    from app.utils.checksum import checksum_of

    return checksum_of(text)


class _StubEmbeddings:
    """固定向量：数量与切片一一对应，绝不发模型请求。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * 3 for index in range(len(texts))]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _StubSplitter:
    def split(self, _text: str) -> list[SplitPiece]:
        return list(PIECES)


class _RecordingStore:
    """记录 purge / add：purge 的范围与写入的稿号都是这条用例要看的。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.purged: list[dict[str, Any]] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(
        self,
        _db: Any,
        document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        self.calls.append("purge")
        self.purged.append(
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "index_version": index_version,
                "doc_version": doc_version,
            }
        )

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append("add")
        self.written.append(chunks)

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("索引链路不应调用检索")


@pytest.fixture
def rollback_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """真回滚路由（临时文件 SQLite）+ 离线 Worker 装配，文档已经改到第 3 稿。"""

    from app.db.async_session import get_async_db

    main = importlib.import_module("app.main")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    database_path = tmp_path / "rollback.db"
    seed_engine = create_engine(f"sqlite:///{database_path}")
    Document.__table__.create(seed_engine)
    DocumentVersion.__table__.create(seed_engine)
    # 租户与套餐（#220）：索引流水线要读租户套餐来判单库 token 容量。
    Tenant.__table__.create(seed_engine)
    Plan.__table__.create(seed_engine)
    with sessionmaker(bind=seed_engine)() as seed_session:
        seed_session.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title="积分规则",
                content=CONTENTS[3],
                source_type="text",
                status=DocumentStatus.SUCCESS,
                current_version=3,
            )
        )
        for number in (1, 2, 3):
            seed_session.add(
                DocumentVersion(
                    id=f"v{number}",
                    document_id=DOC_ID,
                    version=number,
                    content=CONTENTS[number],
                    checksum=_checksum(CONTENTS[number]),
                    status="current" if number == 3 else "archived",
                )
            )
        seed_session.commit()
    seed_engine.dispose()

    request_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    async def _request_session() -> Any:
        async with AsyncSession(request_engine) as session:
            yield session

    # 写入目标版本来自库上的生效版本（#142）。这条用例的临时库里没有 knowledge_bases 表，
    # 于是把「问库要版本」换成固定的 v1——与迁移回填、索引流水线的老行为同值。
    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return DEFAULT_INDEX_VERSION

    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )

    main.app.dependency_overrides[get_async_db] = _request_session
    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                store=_RecordingStore(),
                database_path=database_path,
            )
    finally:
        main.app.dependency_overrides.clear()
        asyncio.run(request_engine.dispose())


def _read_state(database_path: Path) -> SimpleNamespace:
    """另开一条连接读这一次的终态（请求与 Worker 都已提交）。"""

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with sessionmaker(bind=engine)() as session:
            doc = session.get(Document, DOC_ID)
            assert doc is not None
            versions = [
                (row.version, row.status)
                for row in session.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == DOC_ID)
                    .order_by(DocumentVersion.version)
                ).scalars()
            ]
            return SimpleNamespace(
                pointer=doc.current_version,
                status=doc.status,
                content=doc.content,
                versions=versions,
            )
    finally:
        engine.dispose()


async def _run_worker_pipeline(service: IndexingService, database_path: Path) -> int:
    """在**异步**引擎上跑真实流水线（Worker 那一侧的会话类型），跑完即 dispose。"""

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as session:
            return await service.index_existing_document(session, DOC_ID, doc_version=1)
    finally:
        await engine.dispose()


def test_after_rolling_back_to_draft_one_every_retrievable_slice_is_draft_one(
    rollback_env: SimpleNamespace,
) -> None:
    """回滚到第 1 稿 → 重建 → 指针、切片稿号、检索谓词三者对齐。

    「检索非空」= 写入的切片数 > 0；「稿号全为 1」= 每片 ``doc_version`` 都是 1 且
    ``is_current`` 为真（关键词路的召回标记）；两者都与 ``documents.current_version``
    相等，而检索谓词正是这条列与列的比较。
    """

    response = rollback_env.client.post(f"/api/v1/documents/{DOC_ID}/versions/1/rollback")
    body = response.json()
    assert body["code"] == 0, body

    state = _read_state(rollback_env.database_path)
    assert state.pointer == 1
    assert state.status == DocumentStatus.QUEUED, "回滚之后要先重建，所以置排队中"
    assert state.content == CONTENTS[1], "文档行先同步成目标稿正文（热路径快照）"
    assert state.versions == [(1, "current"), (2, "archived"), (3, "archived")]

    writer = rollback_env.store
    service = IndexingService(
        splitter=_StubSplitter(),
        embeddings=_StubEmbeddings(),
        stores=[writer],
    )
    count = asyncio.run(_run_worker_pipeline(service, rollback_env.database_path))

    assert count == len(PIECES) > 0, "重建必须真的写出切片（检索才可能非空）"
    assert writer.calls == ["purge", "add"], "先删旧切片，再写新切片"
    assert writer.purged == [
        {
            "document_id": DOC_ID,
            "tenant_id": TENANT_ID,
            "index_version": DEFAULT_INDEX_VERSION,
            # 清除按「这一稿」收窄：第 3 稿的切片一个字节都不动（#173）。
            "doc_version": 1,
        }
    ]

    written = writer.written[0]
    state = _read_state(rollback_env.database_path)
    assert state.status == DocumentStatus.SUCCESS
    assert state.pointer == 1, "Worker 不回读指针、也不改指针"

    assert {row["doc_version"] for row in written} == {1}, "检索谓词按稿号筛，写错稿就查不到"
    assert {row["is_current"] for row in written} == {True}, (
        "关键词路按 is_current 召回：写入侧必须把它算成「属于当前稿」"
    )
    # 检索谓词本身（唯一一处「当前稿」判定）就是这条列与列的比较——两边一合，
    # 「刚写出的这一批全部命中谓词」= 检索非空且稿号全为指针那一稿。
    predicate = str(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(current_draft_filter())
        .compile(compile_kwargs={"literal_binds": True})
    )
    assert "chunks.doc_version = documents.current_version" in predicate
    assert all(row["doc_version"] == state.pointer for row in written)
