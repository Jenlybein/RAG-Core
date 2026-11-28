"""真实 PostgreSQL 上的索引回写与租约令牌用例（#212 / #213）。

**为什么这两条行为只能在真库上验**：离线会话替身（``tests/fake_db``）明确写着「写语句
（INSERT / UPDATE / DELETE）不模拟行的变化」。而 #212 的缺陷机制正是「SQL 改了库、内存
对象不知道」——状态回写被 ORM 脏检查静默吞掉。#213 的缺陷机制是租约令牌与 ``SELECT ...
FOR UPDATE`` 的行锁判定。两条都落在**库里那几行**上，在看不见写语句的缝上写回归用例只会
得到假绿（这正是本改造的教训：套件曾经全绿，而线上文档永远停在「索引中」）。

覆盖：

* 构建形状（#212 的现场）：``status=1`` 的文档跑一次 ``allow_success=True`` 的索引，
  库里必须仍是成功、且 ``updated_at`` 晚于抢占时刻——修复前这一条必红；
* 单篇形状（不回归）：``status=3`` 且租约为空的文档跑完必须到成功；
* 失败收尾（镜像问题）：``_mark_failed`` 必须真的把失败写进库、并且清掉租约；
* fencing（#213）：A 抢占 → 租约过期 → B 抢占并跑完 → A 醒来继续写，最终库里的切片与
  状态必须是 B 的，A 一行都没写。

默认离线套件不会碰它：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块 skip。

**Windows 上为什么要换循环**：psycopg 的异步模式不支持缺省的 ``ProactorEventLoop``
（ADR-0023），这组用例因此显式用 ``SelectorEventLoop``；容器里是 Linux，缺省即 Selector。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, TypeVar

import pytest
from sqlalchemy import Engine, create_engine, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, now
from app.db.transaction import async_commit_now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.providers.vectorstores.pgvector import PgVectorStore
from app.repositories import document_repository
from app.repositories.document_repository import DOCUMENT_LEASE_SECONDS
from app.services.indexing_service import INDEXING_FAILED_MESSAGE, IndexingService
from app.tasks import indexing as tasks
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_it_lease"
KB_ID = "kb-it-lease"
SEED_IDS = (
    "it-state-build-shape",
    "it-state-single-document",
    "it-state-failed-marking",
    "it-lease-zombie",
)
EMBEDDING_DIM = 1536

T = TypeVar("T")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


# ─────────────────────────────────────────────────────────────── 夹具与种子行


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """在真实库上建这三张表；只删本文件种下的行，绝不 DROP 表。

    ``chunks`` 的 ``embedding`` 是 ``Vector(1536)``，所以建表之前先要 pgvector 扩展——真库
    上它本来就有（检索的向量列），新建的空库则需要这一行。
    """

    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(INTEGRATION_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(
        bind=engine,
        tables=[Document.__table__, DocumentVersion.__table__, Chunk.__table__],
    )
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
        db.execute(delete(Chunk).where(Chunk.document_id.in_(SEED_IDS)))
        db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(SEED_IDS)))
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
    content: str = "# 退款政策\n\n签收后 7 天内可申请无理由退款。\n",
    claimed_at: datetime | None = None,
) -> None:
    """种一行文档（正文走文档行，版本表留空：重建时按「没有版本行」回落文档行）。"""

    db.add(
        Document(
            id=document_id,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title=document_id,
            content=content,
            source_type="text",
            status=status,
            index_claimed_at=claimed_at,
        )
    )
    db.commit()


def _reload_document(db: Session, document_id: str) -> Document:
    """把库里的文档行重新读一遍（异步那一笔提交过，身份映射里的旧值不算数）。"""

    db.expire_all()
    row = db.get(Document, document_id)
    assert row is not None, "种子行不见了：这条用例自己删掉了它"
    return row


def _chunk_contents(db: Session, document_id: str) -> list[str]:
    db.expire_all()
    rows = db.execute(select(Chunk).where(Chunk.document_id == document_id)).scalars().all()
    return [row.content for row in rows]


# ─────────────────────────────────────────────────────────────── 流水线替身


class _StubSplitter:
    """注入的切块器：切片内容由用例给定，切块算法本身不在这条用例的验收范围里。"""

    def __init__(self, pieces: list[SplitPiece]) -> None:
        self._pieces = pieces

    def split(self, _text: str) -> list[SplitPiece]:
        return self._pieces


class _StubEmbeddings:
    """固定维度的离线向量：绝不发模型请求。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        # 每个分量都不一样一点点，避免「全零向量」在别的断言里被当成没写进去。
        return [[0.01 + index * 0.001] * EMBEDDING_DIM for index in range(len(texts))]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应该做单条查询向量化")


class _HookEmbeddings(_StubEmbeddings):
    """向量化时插一段「别的事情已经发生」的钩子，只钩一次。

    这是 #213 fencing 用例的时序开关：``embed_texts`` 被调用的那一刻，Tx1 已经提交、
    Tx2 还没开始——正是真实世界里「这边的 Worker 还在向量化，那边的清扫器已经把租约接走」
    的那个窗口。测试不需要注入时钟，只要在这个既有的缝里把另一件事做完。
    """

    def __init__(self, hook: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self._hook: Callable[[], Awaitable[None]] | None = hook

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        hook, self._hook = self._hook, None
        if hook is not None:
            await hook()
        return await super().embed_texts(texts)


def _service(
    *,
    pieces: list[SplitPiece],
    embeddings: Any | None = None,
) -> IndexingService:
    """真库上的流水线：切块器与 embedding 换成替身，**切片存储是真的**（``PgVectorStore``）。

    存储必须是真的：本文件断言的就是「库里那几行切片的归属」，把存储换成记录器等于又把
    证据搬回内存里。
    """

    return IndexingService(
        splitter=_StubSplitter(pieces),
        embeddings=embeddings if embeddings is not None else _StubEmbeddings(),
        stores=[PgVectorStore()],
    )


def _piece(content: str) -> SplitPiece:
    return SplitPiece(text=content, metadata={"heading_path": "退款"})


# ─────────────────────────────────────────────────────────────── 异步执行缝


def _use_selector_loop_on_windows() -> None:
    """psycopg 的异步模式不支持缺省的 ``ProactorEventLoop``（ADR-0023），本机要显式换。"""

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run(scenario: Callable[..., Awaitable[T]], *, sessions: int = 1) -> T:
    """在**一个**循环里跑完整个场景：异步会话不能跨循环复用，一次 run 建一次引擎。

    ``sessions`` 是这个场景同时需要的会话数（fencing 用例要两个「Worker」，各自一条连接）。
    """

    url = INTEGRATION_DATABASE_URL
    assert url is not None, "整模块已在没有 RAG_INTEGRATION_DATABASE_URL 时 skip"

    async def _main() -> T:
        engine = create_async_engine(url)
        try:
            opened = [AsyncSession(bind=engine, expire_on_commit=False) for _ in range(sessions)]
            try:
                return await scenario(*opened)
            finally:
                for session in opened:
                    await session.close()
        finally:
            await engine.dispose()

    _use_selector_loop_on_windows()
    return asyncio.run(_main())


# ─────────────────────────────────────── #212：构建形状的状态回写（修复前必红）


def test_build_shaped_rebuild_leaves_the_document_success_with_a_fresh_timestamp(
    db: Session,
) -> None:
    """构建形状：``status=1`` 的文档被 ``allow_success=True`` 重建之后仍是成功。

    这一条是 #212 的现场，修复前必红：抢占是一条 ``synchronize_session=False`` 的
    ``UPDATE``（库里变 3），会话又 ``expire_on_commit=False``（内存对象仍是 1），收尾那句
    「回写成功」只是把属性赋成本来就有的值 → ORM 判定无脏属性 → 一条 ``UPDATE`` 都不发。
    症状：切片与向量都在，而状态与 ``updated_at`` 永远停在抢占那一刻。
    """

    _seed(db, "it-state-build-shape", status=DocumentStatus.SUCCESS)

    written = _run(
        lambda session: _service(
            pieces=[_piece("第一片"), _piece("第二片")]
        ).index_existing_document(
            session,
            "it-state-build-shape",
            allow_success=True,
        )
    )

    assert written == 2, "两片都写进去了：这条用例的其余断言才有意义"
    row = _reload_document(db, "it-state-build-shape")
    assert row.status == DocumentStatus.SUCCESS, "构建形状跑完，库里必须是成功（不能停在 3）"
    assert row.index_claimed_at is not None
    assert row.updated_at > row.index_claimed_at, (
        "更新时间必须晚于抢占时刻：相等就是「收尾那句回写被静默吞掉了」"
    )
    assert len(_chunk_contents(db, "it-state-build-shape")) == 2


# ─────────────────────────────────────── #212：单篇形状不回归（令牌匹配路径）


def test_single_document_shape_still_reaches_success(db: Session) -> None:
    """单篇形状（``status=3``、租约为空）跑完必须到成功——这条路径以前就是对的。

    它同时是 #213 的**正常路径**：租约令牌由这一次抢占自己写下，Tx2 开头的核对必须通过
    （不通过就会返回 0 且一行都不写，下面的切片断言会当场红）。
    """

    _seed(db, "it-state-single-document", status=DocumentStatus.PROCESSING)

    written = _run(
        lambda session: _service(pieces=[_piece("唯一一片")]).index_existing_document(
            session, "it-state-single-document"
        )
    )

    assert written == 1, "令牌匹配的正常路径若被挡掉，这里就是 0"
    row = _reload_document(db, "it-state-single-document")
    assert row.status == DocumentStatus.SUCCESS
    assert row.index_claimed_at is not None
    assert row.updated_at > row.index_claimed_at
    assert _chunk_contents(db, "it-state-single-document") == ["唯一一片"]


# ─────────────────────────────────────── #212：失败收尾的镜像问题（失败要真写进去）


def test_marking_failed_really_writes_the_row_and_clears_the_lease(db: Session) -> None:
    """失败收尾也是「写语句」：状态、固定文案、租约必须同时落在库里。

    这条路径**不受 fencing 约束**（它另起一笔事务、重新读行），所以它必须照常写成功——
    否则镜像问题就是「失败的文档永远停在索引中」，比状态回写静默失效更难查。
    """

    _seed(
        db,
        "it-state-failed-marking",
        status=DocumentStatus.PROCESSING,
        claimed_at=now(),
    )
    seeded_updated_at = _reload_document(db, "it-state-failed-marking").updated_at

    _run(lambda session: tasks._mark_failed(session, "it-state-failed-marking"))

    row = _reload_document(db, "it-state-failed-marking")
    assert row.status == DocumentStatus.FAILED
    assert row.error_message == INDEXING_FAILED_MESSAGE, "库里只留脱敏固定文案"
    assert row.index_claimed_at is None, "失败终态不保留租约，否则重试永远抢不到"
    # 时间戳是值类型，先存下来：``_reload_document`` 返回的是同一个身份映射对象。
    assert row.updated_at > seeded_updated_at


# ─────────────────────────────────────── #213：僵尸任务不许覆盖新主人的结果


def test_a_zombie_worker_cannot_overwrite_the_new_owner(db: Session) -> None:
    """A 抢占 → 租约过期 → B 抢占并跑完 → A 醒来写库：库里必须全是 B 的。

    时序（全部落在真实的那两段事务之间，不需要注入时钟）：

    1. A 抢占成功（Tx1 提交），手里拿着租约令牌；
    2. A 在向量化时，另一条连接把 ``index_claimed_at`` 改成过期时刻——模拟「A 卡了超过
       租约窗口」；随后 B 抢占并完整跑完（写切片、回写成功）；
    3. A 醒来继续走 Tx2：令牌核对读到 B 的时间戳，与手里的不等 → 整笔放弃。

    没有 fencing 时的症状：A 先 purge 掉 B 刚写的切片，再把自己的内容写回去，最后把状态
    从「索引中」改回「成功」——构建看起来成功了，检索到的却是旧内容。
    """

    _seed(db, "it-lease-zombie", status=DocumentStatus.SUCCESS)
    zombie_pieces = [_piece("A 写下的旧内容")]
    owner_pieces = [_piece("B 写下的新内容")]

    async def _takeover(owner_session: AsyncSession) -> None:
        """「另一个任务」：租约先过期，再由它抢占并跑完一整篇。"""

        await owner_session.execute(
            update(Document)
            .where(Document.id == "it-lease-zombie")
            .values(index_claimed_at=now() - timedelta(seconds=DOCUMENT_LEASE_SECONDS * 2))
        )
        await async_commit_now(owner_session)
        replaced = await _service(pieces=owner_pieces).index_existing_document(
            owner_session, "it-lease-zombie"
        )
        assert replaced == 1, "接管者自己必须写成功，否则这条用例验的是别的东西"

    async def _scenario(zombie_session: AsyncSession, owner_session: AsyncSession) -> int:
        async def _hook() -> None:
            await _takeover(owner_session)

        return await _service(
            pieces=zombie_pieces, embeddings=_HookEmbeddings(_hook)
        ).index_existing_document(
            zombie_session,
            "it-lease-zombie",
            allow_success=True,
        )

    written = _run(_scenario, sessions=2)

    assert written == 0, "令牌对不上就该整笔放弃：调用方按「跳过」处理，返回 0"
    assert _chunk_contents(db, "it-lease-zombie") == ["B 写下的新内容"], (
        "库里必须是接管者的切片：僵尸任务一行都不许写"
    )
    row = _reload_document(db, "it-lease-zombie")
    assert row.status == DocumentStatus.SUCCESS, "状态停在接管者跑完的样子"
    assert row.index_claimed_at is not None


# ─────────────────────────────────────── #213：抢占把令牌交回调用方


def test_the_claim_hands_back_the_lease_it_wrote(db: Session) -> None:
    """抢占函数返回的令牌必须**就是**库里的 ``index_claimed_at``（不是另取一次时间）。

    令牌若另取一次时间，它与库里那个值会差几微秒，Tx2 的核对永远不等——症状是「所有文档
    都索引不出东西」，而这一条用例就是它的守门人。
    """

    _seed(db, "it-state-single-document", status=DocumentStatus.FAILED)

    async def _scenario(session: AsyncSession) -> datetime | None:
        token = await document_repository.try_claim_for_indexing_async(
            session, "it-state-single-document", tenant_id=TENANT_ID, allow_success=False
        )
        await async_commit_now(session)
        return token

    token = _run(_scenario)

    assert token is not None, "失败文档可抢占"
    row = _reload_document(db, "it-state-single-document")
    assert row.index_claimed_at == token, "返回的令牌与写进库的那个值必须逐微秒相同"
    assert row.status == DocumentStatus.PROCESSING

    async def _second(session: AsyncSession) -> datetime | None:
        return await document_repository.try_claim_for_indexing_async(
            session, "it-state-single-document", tenant_id=TENANT_ID, allow_success=False
        )

    assert _run(_second) is None, "有效租约下第二次抢占必须回 None（原来是回 False）"
