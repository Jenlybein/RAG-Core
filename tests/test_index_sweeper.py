"""真实 PostgreSQL 上的清扫器集成用例（票 #225）。

## 为什么必须在真库上跑

清扫器的三个分支**全部以「库里那几行变成了什么样」为判据**：

* 逾期构建被收尾 → ``index_build_versions.status`` 从 ``building`` 变成 ``ready`` / ``failed``；
* 缺篇补投 → ``redispatch_count`` 加一，而「缺了哪几篇」是读 ``chunks`` 真值算出来的；
* 租约过期重投 → ``documents.status`` 回到 ``4``、``index_claimed_at`` 变 NULL、
  而判「过期」用的是 ``COALESCE(index_heartbeat_at, index_claimed_at)`` 这个**由数据库求值**
  的表达式。

离线替身（``tests/fake_db.py``）明写「写语句不模拟行的变化」，在那条缝上断言这三件事只会
得到假绿——而假绿正是这一票要消灭的东西（改造前的用例把状态回写换成记录器并断言「调用过」，
真实缺陷是「调用了但没写进去」）。所以这里跑真库。

与 ``tests/test_integration_queue_state.py`` 同一套纪律：``RAG_INTEGRATION_DATABASE_URL``
未设置时整模块 skip；只建自己用到的表、只删自己种下的行（按本文件专用的 ``tenant_id``
收口），绝不 DROP 表、也绝不碰别人的数据。

## 为什么这里直接调 ``sweep_once`` 而不是走线程

线程只是「多久跑一次 + 抢锁」，是本进程的调度形状（那个形状由 ``start_sweeper_thread``
的幂等与 ``_try_lock`` 的语义守，与数据库无关）。这里要验的是**清扫本身对不对**：给它一批
已经卡住的行，跑一遍，看库里的结论。所以直接调协程，不起线程、不碰锁。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base, now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import IndexBuildVersion
from app.models.knowledge_base import KnowledgeBase
from app.models.tenant import Tenant
from app.tasks import index_sweeper
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")


def _sync_url() -> str:
    """同步引擎的 URL：仓库的同步驱动是 **psycopg v3**（`psycopg[binary]`），不是 psycopg2。

    环境变量给的多半是裸 ``postgresql://``，SQLAlchemy 会按它去找 psycopg2——那个包不在
    依赖里，报的是 ``ModuleNotFoundError: No module named 'psycopg2'``，与业务毫无关系。
    所以显式带上 ``+psycopg``。
    """

    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


def _async_url() -> str:
    """异步引擎的 URL：仓库用 **psycopg3 原生异步**（``postgresql+psycopg``），不是 asyncpg。

    见 ``app/db/async_session.py`` 的注释：psycopg 3.2.3 自带异步，SQLAlchemy 只把
    ``create_engine`` 换成 ``create_async_engine``，类型映射与 SQL 一个字都不用改。
    所以这里**不能**写成 ``+asyncpg``（那个驱动根本不在依赖里）。
    """

    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

#: 本文件专用的租户与库：清理只按它们收口，绝不扫别人的行。
TENANT_ID = "tenant_sweeper"
KB_ID = "kb-sweeper"
BUILD_ID = "build-sweeper-1"

#: 三篇文档，各自代表清扫器要处理的一种处境。
DOC_EXPIRED = "sw-doc-expired"
DOC_FRESH = "sw-doc-fresh"
DOC_GIVE_UP = "sw-doc-give-up"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


@pytest.fixture(scope="module")
def sync_engine() -> Iterator[Engine]:
    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(_sync_url())
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Document.__table__,
            KnowledgeBase.__table__,
            IndexBuildVersion.__table__,
            Chunk.__table__,
            Tenant.__table__,
        ],
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(sync_engine: Engine) -> Iterator[Session]:
    session = Session(bind=sync_engine)
    _clean(session)
    try:
        yield session
    finally:
        _clean(session)
        session.close()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[Any]:
    """清扫器的会话工厂：指向同一个真库，好让断言看得见它的写。

    **传进去而不是替换模块级那个**：离线套件的 ``conftest.py`` 会把 ``async_session_factory``
    换成不写库的替身（整个套件的隔离靠它），替换了模块级变量的结果是「谁最后跑谁生效」——
    那是一种随 fixture 顺序漂移的测试。参数注入没有这个竞争。
    """

    engine = create_async_engine(_async_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # 与 ``app.db.session_factory.async_session_factory`` **同型**：一个调用后得到
        # sessionmaker 的函数（生产代码写的是 ``factory()()``）。
        yield lambda: factory
    finally:
        await engine.dispose()


def _clean(db: Session) -> None:
    """只删本文件种下的行：按专用租户 + 专用 id 收口。"""

    try:
        db.execute(delete(Chunk).where(Chunk.tenant_id == TENANT_ID))
        db.execute(delete(IndexBuildVersion).where(IndexBuildVersion.tenant_id == TENANT_ID))
        db.execute(delete(Document).where(Document.tenant_id == TENANT_ID))
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID))
        db.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        db.commit()
    except Exception:
        db.rollback()
        raise


def _seed(db: Session) -> None:
    """种下一个 building 构建 + 三篇处境不同的文档。"""

    claim_at = now()
    db.merge(Tenant(id=TENANT_ID, name="清扫器集成租户", plan_id=1, status="active"))
    db.merge(
        KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="清扫器集成库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            settings={},
            active_index_version="v1",
        )
    )
    db.merge(
        Document(
            id=DOC_EXPIRED,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title="租约过期.md",
            status=DocumentStatus.PROCESSING,
            source_type="markdown",
            # 抢占时间在很久以前、心跳也是：清扫器应当判定「没人跑」。
            index_claimed_at=claim_at - timedelta(seconds=3600),
            index_heartbeat_at=claim_at - timedelta(seconds=3600),
        )
    )
    db.merge(
        Document(
            id=DOC_FRESH,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title="正在跑.md",
            status=DocumentStatus.PROCESSING,
            source_type="markdown",
            # 心跳刚刚刷过：它活着，清扫器**不许**动它（抢走它等于白烧一轮嵌入）。
            index_claimed_at=claim_at - timedelta(seconds=3600),
            index_heartbeat_at=claim_at,
        )
    )
    db.merge(
        Document(
            id=DOC_GIVE_UP,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title="救不回来.md",
            status=DocumentStatus.PROCESSING,
            source_type="markdown",
            index_claimed_at=claim_at - timedelta(seconds=3600),
            index_heartbeat_at=claim_at - timedelta(seconds=3600),
            redispatch_count=index_sweeper.MAX_LEASE_REDISPATCH,
        )
    )
    db.merge(
        IndexBuildVersion(
            id=BUILD_ID,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            version="v2",
            status="building",
            documents_planned=2,
            doc_version=1,
            documents_done=0,
            # 期限已过：清扫器应当按真值收尾，而不是继续等。
            converge_deadline=claim_at - timedelta(seconds=60),
            created_at=claim_at,
            params={},
        )
    )
    # 「已入新版本」的那一篇：让它有一行切片，于是真值 != 0，收尾结论是「不完整」而不是
    # 「一篇都没进来」——两个结论走的是两条不同的文案与异常类，这里验其中一条。
    db.merge(
        Chunk(
            id="sw-chunk-1",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            document_id=DOC_EXPIRED,
            index_version="v2",
            doc_version=1,
            content="清扫器真值样本",
            embedding=[0.0] * 1536,
            meta={},
        )
    )
    db.commit()


def _row(db: Session, document_id: str) -> Document:
    db.expire_all()
    row = db.execute(select(Document).where(Document.id == document_id)).scalar_one()
    return row


def _build(db: Session) -> IndexBuildVersion:
    db.expire_all()
    return db.execute(
        select(IndexBuildVersion).where(IndexBuildVersion.id == BUILD_ID)
    ).scalar_one()


def test_the_sweeper_requeues_an_expired_document_and_leaves_the_live_one_alone(
    db: Session, session_factory: Any
) -> None:
    """租约过期 → 回到排队中、清掉租约、重投计数加一；心跳新鲜的**不动**。"""

    _seed(db)
    assert _row(db, DOC_EXPIRED).status == DocumentStatus.PROCESSING

    asyncio.get_event_loop().run_until_complete(_run_sweep(session_factory))

    expired = _row(db, DOC_EXPIRED)
    assert expired.status == DocumentStatus.QUEUED, "过期的应当回到「排队中」等人抢"
    assert expired.index_claimed_at is None, "不坪租约，重投出去的任务抢不到（静默 no-op）"
    assert expired.redispatch_count == 1, "救过一次就要记一次，到顶才能给出结论"

    fresh = _row(db, DOC_FRESH)
    assert fresh.status == DocumentStatus.PROCESSING, "心跳新鲜的那篇还在跑，清扫器不许动它"
    assert fresh.redispatch_count == 0


def test_the_sweeper_gives_up_on_a_document_that_kept_dying(
    db: Session, session_factory: Any
) -> None:
    """重投次数到顶 → 标成失败。**「救不回来」也是一个必须兑现的结论**。"""

    _seed(db)
    asyncio.get_event_loop().run_until_complete(_run_sweep(session_factory))

    row = _row(db, DOC_GIVE_UP)
    assert row.status == DocumentStatus.FAILED, "一直救不回来就给药一个明确的结论，而不是永远转圈"
    assert row.index_claimed_at is None
    assert row.error_message, "失败必须带一句人话，否则用户只看到一个红点"


def test_the_sweeper_settles_an_overdue_build_against_the_truth(
    db: Session, session_factory: Any
) -> None:
    """逾期构建 → 按**真值**收尾（不是按进度计数），于是「一篇都没真重建」不可能 ready。"""

    _seed(db)
    assert _build(db).status == "building"

    asyncio.get_event_loop().run_until_complete(_run_sweep(session_factory))

    build = _build(db)
    # 真值是 1（只有 DOC_EXPIRED 有切片），参与篇数是 2 → 收成 failed 而不是 ready。
    # 这条断言正是改造前的核心缺陷：计数器说「跑了 2 篇」，真值说「只有 1 篇」。
    assert build.status == "failed"
    assert build.error_message, "失败要留下原因文案（不拼异常原文）"


async def _run_sweep(session_factory: Any) -> dict[str, int]:
    return await index_sweeper.sweep_once(session_factory=session_factory)
