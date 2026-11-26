"""构建子任务的**任务体**集成用例（#215 / 评审补的「唯一新缝」）。

## 这一条用例补的是什么缺口

改造前没有任何用例调用过 ``build_document_task``——只有一条断言它的**路由**是 ``build``
队列（看配置表，不是看行为）。于是「载荷解包 → 跑流水线 → 回来记账 → 尝试收敛」这一跳
从未被执行过，而它正是 ``activate`` 那次回归的邻居：每一段单独测都对，拼起来少一个参数就
静默失效（载荷里漏 ``doc_version`` 会把切片写进错误的稿；``allow_success`` 误写成 False
会让「重建一篇已成功的文档」被抢占直接拒掉——症状是进度条不动、真值不变、最后靠期限判失败）。

## 替换的是「解析 + 嵌入」，**不**替换记账

``IndexingService.index_existing_document`` 是「解析文件 + 切块 + 调模型 + 写两个库」那一段，
它需要真的模型与真的文件。本用例把它换成一个**最小真实实现**：往 ``chunks`` 写一条行
（真表、真的 ``(index_version, doc_version)``），然后返回切片数 1。

分工因此很干净：

* **被替换的**：模型调用（已由 ``test_indexing_task.py`` 与离线端到端用例覆盖）；
* **被保留的**：任务体的参数解析、``_record_build_outcome`` 的记账（真库 UPDATE）、
  ``converge`` 的**真值判定**（去 ``chunks`` 数行）。

于是「这一版到底建成了几篇」这个结论由**真表里的行**决定，而不是由某个替身的口头回答决定。

## 为什么必须在真库上跑

``_record_build_outcome`` 走 ``UPDATE ... RETURNING`` / ``rowcount``（``advance_progress``
与 ``record_failure``），``converge`` 走 ``COUNT(DISTINCT document_id)``。离线替身
（``tests/fake_db.py``）明写「写语句不模拟行的变化」——在那条缝上这两件事都会得到假绿。

与 ``tests/test_index_sweeper.py`` 同一套纪律：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整
模块 skip；只建自己用到的表、只删自己种下的行，绝不 DROP 表、绝不碰别人的数据。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base, now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import IndexBuildVersion
from app.models.knowledge_base import KnowledgeBase
from app.models.tenant import Tenant
from app.tasks import index_build
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_build_task"
KB_ID = "kb-build-task"
BUILD_ID = "build-task-1"
VERSION = "v2"
DRAFT = 2

#: 三篇参与构建的文档（都是 `1 成功`——构建要重建的正是成功文档）。
DOC_IDS = ("bt-doc-1", "bt-doc-2", "bt-doc-3")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


def _sync_url() -> str:
    """同步引擎的 URL：仓库的同步驱动是 psycopg v3（``psycopg[binary]``），不是 psycopg2。

    环境变量给的多半是裸 ``postgresql://``，SQLAlchemy 会按它去找 psycopg2——那个包不在
    依赖里，报的是 ``ModuleNotFoundError``，与业务毫无关系。所以显式带 ``+psycopg``。
    """

    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


def _async_url() -> str:
    """异步引擎的 URL：仓库用 **psycopg3 原生异步**（``postgresql+psycopg``），不是 asyncpg。

    见 ``app/db/async_session.py``：psycopg 3.2.3 自带异步，SQLAlchemy 只把 ``create_engine``
    换成 ``create_async_engine``，类型映射与 SQL 一个字都不用改。
    """

    return INTEGRATION_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert INTEGRATION_DATABASE_URL is not None
    eng = create_engine(_sync_url())
    Base.metadata.create_all(
        bind=eng,
        tables=[
            Document.__table__,
            KnowledgeBase.__table__,
            IndexBuildVersion.__table__,
            Chunk.__table__,
            Tenant.__table__,
        ],
    )
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    session = Session(bind=engine)
    _clean(session)
    _seed(session)
    try:
        yield session
    finally:
        _clean(session)
        session.close()


def _clean(db: Session) -> None:
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
    """一行 ``building`` 构建 + 三篇成功文档（``chunks`` 留空，由流水线替身写）。"""

    started = now()
    db.merge(Tenant(id=TENANT_ID, name="构建任务集成租户", plan_id=1, status="active"))
    db.merge(
        KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="构建任务集成库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            settings={},
            active_index_version="v1",
        )
    )
    for document_id in DOC_IDS:
        db.merge(
            Document(
                id=document_id,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title=f"{document_id}.md",
                status=DocumentStatus.SUCCESS,
                source_type="markdown",
            )
        )
    db.merge(
        IndexBuildVersion(
            id=BUILD_ID,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            version=VERSION,
            status="building",
            # 参与篇数 = 3 就是「起跑快照」：真值要到 3 才 ready。
            documents_planned=len(DOC_IDS),
            doc_version=DRAFT,
            documents_done=0,
            # 期限给宽：这些用例验的是「正常收尾」，不是「逾期收尾」（后者归清扫器）。
            converge_deadline=started + timedelta(hours=1),
            created_at=started,
            params={},
        )
    )
    db.commit()


def _build(db: Session) -> IndexBuildVersion:
    db.expire_all()
    return db.execute(
        select(IndexBuildVersion).where(IndexBuildVersion.id == BUILD_ID)
    ).scalar_one()


def _chunk_documents(db: Session) -> set[str]:
    db.expire_all()
    return set(
        db.execute(
            select(Chunk.document_id).where(
                Chunk.tenant_id == TENANT_ID,
                Chunk.index_version == VERSION,
                Chunk.doc_version == DRAFT,
            )
        ).scalars()
    )


def _payload(document_id: str) -> dict[str, Any]:
    """与 :func:`app.services.index_dispatch.enqueue_build_document` 投出去的那一份同形。"""

    return {
        "document_id": document_id,
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "build_id": BUILD_ID,
        "index_version": VERSION,
        "doc_version": DRAFT,
        "allow_success": True,
    }


@pytest.fixture
def pipeline(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, db: Session
) -> Iterator[dict[str, list[dict[str, Any]]]]:
    """把「解析 + 嵌入」换成一个写一条切片的最小实现，**并让任务体连上真库**。

    两处替换缺一不可：

    1. ``indexing_service.IndexingService.index_existing_document`` —— 换成写一条切片；
    2. ``tasks.indexing.async_session_factory`` —— 换成真库的会话工厂。不换的话，任务体拿到
       的是离线套件注入的替身，``record_outcome`` 的 UPDATE 会「执行成功但一行没变」——
       正是这一票要消灭的那种假绿。
    """

    seen: list[dict[str, Any]] = []
    async_engine = create_async_engine(_async_url())
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    real_session = Session(bind=engine)

    async def fake_index_existing_document(
        self: Any,
        db: AsyncSession,
        document_id: str,
        *,
        reparse: bool = False,
        index_version: str | None = None,
        doc_version: int | None = None,
        allow_success: bool = False,
    ) -> int:
        seen.append(
            {
                "document_id": document_id,
                "reparse": reparse,
                "index_version": index_version,
                "doc_version": doc_version,
                "allow_success": allow_success,
            }
        )
        # 续建：真实实现会先查「这一版这一稿下有没有切片」，有就跳过、返回 0。
        already = real_session.execute(
            select(Chunk.id).where(
                Chunk.tenant_id == TENANT_ID,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
                Chunk.doc_version == int(doc_version or 1),
            )
        ).first()
        if already is not None:
            return 0
        real_session.merge(
            Chunk(
                id=f"chunk-{index_version}-{doc_version}-{document_id}",
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                document_id=document_id,
                index_version=index_version or "v1",
                doc_version=int(doc_version or 1),
                content="切片正文",
                embedding=[0.0] * 1536,
                meta={},
            )
        )
        real_session.commit()
        return 1

    monkeypatch.setattr(
        "app.services.indexing_service.IndexingService.index_existing_document",
        fake_index_existing_document,
    )
    monkeypatch.setattr("app.tasks.indexing.async_session_factory", lambda: factory)
    try:
        yield {"seen": seen}
    finally:
        real_session.close()
        # 引擎归常驻循环所有：在这里 dispose 会跨事件循环操作（未 await 的协程警告就是这么来的）。
        # 进程退出时由 pytest 收掉；用例之间的隔离靠「只删自己种下的行」。


def test_the_task_body_hands_the_whole_payload_to_the_pipeline(
    pipeline: dict[str, Any],
) -> None:
    """任务体必须把载荷**逐个**交给流水线——这是本文件要钉住的第一件事。

    少交一个字段的后果各不相同，而且都不会报错：

    * 漏 ``doc_version`` → 切片写进「当前稿」而不是「这一版的稿」，真值判定永远对不上；
    * 漏 ``index_version`` → 写进**生效版本**，把线上正在用的那一版污染；
    * ``allow_success`` 错成 False → 抢占直接拒掉一篇状态为「成功」的文档（构建要重建的正是
      它们），于是每次构建都静默地一篇都建不成。
    """

    index_build.build_document_task.run(**_payload(DOC_IDS[0]))  # type: ignore[attr-defined]

    assert pipeline["seen"] == [
        {
            "document_id": DOC_IDS[0],
            "reparse": False,  # 构建从不重新解析原文件（内容已经在版本行里）
            "index_version": VERSION,
            "doc_version": DRAFT,
            "allow_success": True,
        }
    ]


def test_the_task_body_records_the_outcome_so_the_build_reaches_ready(
    db: Session, pipeline: dict[str, Any]
) -> None:
    """三篇各跑一次 → 进度到 3、真值到 3、构建自己收敛成 ``ready``。

    本文件的核心：**没有任何一方显式调用 ``converge``**——最后一篇收尾时顺手判的。
    """

    assert _build(db).status == "building"
    assert _chunk_documents(db) == set()

    for document_id in DOC_IDS:
        index_build.build_document_task.run(**_payload(document_id))  # type: ignore[attr-defined]

    build = _build(db)
    assert build.status == "ready", "三篇都真写进了这一版，就该 ready"
    assert build.documents_done == len(DOC_IDS)
    # 真值来自 chunks 表（不是计数器）：这条断言与 converge 用的是同一个读数。
    assert _chunk_documents(db) == set(DOC_IDS)
    assert build.document_count == len(DOC_IDS)


def test_a_redispatch_of_the_same_document_does_not_double_count(
    db: Session, pipeline: dict[str, Any]
) -> None:
    """同一篇被投第二次：流水线跳过（返回 0）、进度不翻倍、构建保持 ``ready``。

    重投在真实世界里很常见（清扫器补投、消息重复消费）。续建的正确性就落在这里：
    判据是**真值**（这一版这一稿下有切片），不是某个「已投过」的标记。
    """

    for document_id in DOC_IDS:
        index_build.build_document_task.run(**_payload(document_id))  # type: ignore[attr-defined]

    assert _build(db).status == "ready"
    before = _chunk_documents(db)

    # 再投一遍第一篇：流水线被调用，但它是「续建」——发现已有切片就返回 0。
    index_build.build_document_task.run(**_payload(DOC_IDS[0]))  # type: ignore[attr-defined]

    build = _build(db)
    assert build.documents_done == len(DOC_IDS), "重投不许把进度推过参与篇数"
    assert _chunk_documents(db) == before, "续建不许重复写切片"
    assert build.status == "ready"
