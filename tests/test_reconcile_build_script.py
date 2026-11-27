"""运维脚本 `scripts/reconcile_build.py` 的判据集成用例（现场 #226 的回归）。

## 为什么这个脚本需要自己的用例

它是**运维照着一个结论动手**的入口：`--apply` 会把一条 `building` 行按脚本算出来的
verdict 落库——`ready` 意味着「这个版本可以激活了」，`failed` 是终态。所以「脚本算出来的
结论」与「服务写下去的结论」必须是同一个，否则就会出现两种都很像对的答案：

* 脚本说 `ready`，运维照着激活 → 上线一个**空版本**（现场 #226 正是这个：那个库里有 10 篇
  文档，而 10 篇全是失败，`truth = planned = 0`，纯判据给出 `ready`）；
* 脚本说 `failed`、服务说 `ready` → 一次正常的构建被脚本提前判死。

两条判据（真值 + 空版本守卫 + 失败明细）现在都是共用的纯函数，但「脚本有没有真的接上它们」
只能靠跑一次才知道——所以这里用真库把 #226 那一格钉死。

## 纪律

与 `tests/test_index_sweeper.py` 同一套：``RAG_INTEGRATION_DATABASE_URL`` 未设置时整模块
skip；只建自己用到的表、只删自己种下的行（按本文件专用的 ``tenant_id`` 收口），
绝不 DROP 表、也绝不碰别人的数据。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base, now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import INDEX_BUILD_BUILDING, IndexBuildVersion
from app.models.knowledge_base import KnowledgeBase
from app.models.tenant import Tenant
from app.services.index_build_service import IndexBuildService
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_reconcile_script"
KB_ID = "kb-reconcile-script"
BUILD_ID = "build-reconcile-script"
VERSION = "v2"
DRAFT = 1

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]


def _script() -> ModuleType:
    """把 `scripts/reconcile_build.py` 当模块加载（它不在安装包里）。

    `scripts/` 不是包，所以按文件路径加载。这条路也顺带证明脚本的**顶层导入**是好的
    ——它一旦引用了不存在的东西（例如删掉的构建任务），第一个用例就会失败。
    """

    path = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_build.py"
    spec = importlib.util.spec_from_file_location("reconcile_build_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记再执行：脚本里有 ``@dataclass``，而 dataclasses 生成代码时要从
    # ``sys.modules`` 里回查模块的 ``__dict__``——不登记的话拿到的是 None，
    # 报的是一句与业务毫无关系的 `'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sync_url() -> str:
    """同步引擎用 psycopg v3（仓库没有 psycopg2），所以显式带 ``+psycopg``。"""

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
    try:
        yield session
    finally:
        _clean(session)
        session.close()


def _clean(session: Session) -> None:
    session.rollback()
    session.execute(delete(Chunk).where(Chunk.tenant_id == TENANT_ID))
    session.execute(delete(IndexBuildVersion).where(IndexBuildVersion.tenant_id == TENANT_ID))
    session.execute(delete(Document).where(Document.tenant_id == TENANT_ID))
    session.execute(delete(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID))
    session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
    session.commit()


def _seed(db: Session, *, documents: int, failed: bool, planned: int) -> IndexBuildVersion:
    """种一个库 + 若干文档 + 一条 `building` 行。

    ``failed=True`` 时所有文档都是 `2 失败`——现场 #226 的形状：库里明明有十篇，
    但没有一篇可参与构建。
    """

    started = now()
    db.merge(Tenant(id=TENANT_ID, name="运维脚本集成租户", plan_id=1, status="active"))
    db.merge(
        KnowledgeBase(
            id=KB_ID,
            tenant_id=TENANT_ID,
            name="运维脚本集成库",
            split_strategy="structure",
            allowed_formats=["markdown"],
            active_index_version=None,
        )
    )
    for index in range(documents):
        db.merge(
            Document(
                id=f"rs-doc-{index}",
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title=f"rs-doc-{index}.md",
                status=DocumentStatus.FAILED if failed else DocumentStatus.SUCCESS,
                source_type="markdown",
                current_version=DRAFT,
            )
        )
    row = IndexBuildVersion(
        id=BUILD_ID,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=VERSION,
        status=INDEX_BUILD_BUILDING,
        documents_planned=planned,
        doc_version=DRAFT,
        documents_done=0,
        converge_deadline=started + timedelta(hours=1),
        created_at=started,
        params={},
    )
    db.merge(row)
    db.commit()
    return row


def _inspect(db: Session, row: IndexBuildVersion) -> Any:
    """跑脚本的只读那一步（`inspect_build`），返回它算出来的报告。"""

    module = _script()
    engine = create_async_engine(_sync_url())

    async def _run() -> Any:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await module.inspect_build(
                session, IndexBuildService(), row, tenant_id=TENANT_ID
            )

    try:
        return asyncio.run(_run())
    finally:
        engine.dispose()


def test_the_script_says_failed_for_a_library_whose_documents_all_failed(
    db: Session,
) -> None:
    """现场 #226 的那一格：库里 10 篇全失败、`planned=0` → 脚本必须说 **failed**。

    纯判据在这里给的是 `ready`（真值 0 = 计划 0），运维照着激活就会得到一个空版本：
    线上检索一条都查不到，而界面写着「构建成功」。这条用例守的是脚本真的接上了
    `guard_against_an_empty_version`（与 `converge` 共用的那个纯函数）。
    """

    row = _seed(db, documents=10, failed=True, planned=0)

    report = _inspect(db, row)

    assert report.verdict == "failed", (
        "库里有 10 篇文档、但没有一篇可参与：这不叫「空库」，空版本不许激活"
    )
    assert report.truth.planned == 0
    assert report.truth.documents == 0
    assert report.truth.documents_in_kb == 10


def test_the_script_still_says_ready_for_a_genuinely_empty_library(db: Session) -> None:
    """**真空库**（一篇文档都没有）产出空版本是 ADR-0025 允许的——脚本不许把它判死。"""

    row = _seed(db, documents=0, failed=False, planned=0)

    report = _inspect(db, row)

    assert report.verdict == "ready"
    assert report.truth.documents_in_kb == 0


def test_the_script_reads_the_failure_detail_the_same_way_converge_does(db: Session) -> None:
    """失败明细必须与 `converge` 同源。

    脚本原来把 `failed=0` 写死，而服务读 `row.failed_documents`：于是「真值够、但有明细」
    的构建会被脚本判 `ready`、被服务判 `failed`。这条用例把那个分歧钉死——真值够（唯一
    一篇已经建好）而明细非空时，两边都该是 `failed`。
    """

    row = _seed(db, documents=1, failed=False, planned=1)
    row.failed_documents = [
        {"document_id": "rs-doc-0", "title": "rs-doc-0.md", "reason": "document indexing failed"}
    ]
    db.merge(row)
    db.commit()
    # 这一篇**已经建好**了（真值够），但明细里还挂着一条失败记录。
    db.merge(
        Chunk(
            id=f"rs-chunk-{VERSION}-{DRAFT}",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            document_id="rs-doc-0",
            index_version=VERSION,
            doc_version=DRAFT,
            content="切片",
            embedding=[0.0] * 1536,
            meta={},
        )
    )
    db.commit()
    stored = db.execute(
        select(IndexBuildVersion).where(IndexBuildVersion.id == BUILD_ID)
    ).scalar_one()

    report = _inspect(db, stored)

    assert report.truth.documents == 1, "真值够"
    assert report.verdict == "failed", (
        "有失败明细就不许上线（部分版本永不上线）——脚本与服务必须给同一个结论"
    )
