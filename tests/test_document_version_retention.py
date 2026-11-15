"""#178 · 保留窗口：留最近 3 稿，窗口外的**归档**稿连切片带版本行一起清掉。

三层接缝，都是既有的：

* **仓储层**（``sqlite+aiosqlite`` 的真 SQL）：窗口查询只挑「这篇文档、稿号在某条界
  以下、状态是归档」的行；删除只按给定稿号打，且**不提交**（提交点只有事务入口那一处）；
* **服务层**（真 SQL + 一个记账用的切片存储替身）：五稿造出来 → 清理后剩 3/4/5；
  切片的删除键是两个维度（``doc_version`` 给了、``index_version`` 留空）；先删切片再删
  版本行；中途失败整笔回滚，不留「切片已删、版本行还在」的半份；
* **运维脚本**（``main(argv) -> int``）：默认 dry-run 只打印将删什么、``--apply`` 才动手、
  ``--retain`` 覆盖默认 3、退出码沿用 ``_ops_common.exit_code_for``。

脚本通过文件路径加载（``scripts*`` 不在安装包里），顺带验证「导入即安全」：模块顶层不
解析 argv、不连库、不建引擎。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.repositories import document_repository, document_version_repository
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


# ─────────────────────────────────────────────────── 夹具：两张真表（aiosqlite）


@pytest_asyncio.fixture
async def retention_db() -> AsyncIterator[AsyncSession]:
    """``documents`` + ``document_versions`` 两张真表。

    用真 SQL 而不是替身会话：这一票要验的正是「``WHERE`` 的范围条件把哪几行筛出来」
    「``DELETE ... IN (...)`` 真的删掉了几行」——替身会话对这些只会点头。
    """

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
    async with AsyncSession(engine) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


def _document(
    *,
    document_id: str,
    current_version: int = 1,
    kb_id: str = KB_ID,
    tenant_id: str = TENANT_ID,
) -> Document:
    return Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=document_id,
        content=f"{document_id} 的正文",
        status=DocumentStatus.SUCCESS,
        current_version=current_version,
    )


def _version(
    *,
    document_id: str,
    version: int,
    status: str = "archived",
) -> DocumentVersion:
    return DocumentVersion(
        id=f"{document_id}-v{version}",
        document_id=document_id,
        version=version,
        content=f"{document_id} 第 {version} 稿正文",
        checksum="0" * 64,
        status=status,
        created_at=datetime(2026, 11, 16, 0, 0, 0),
    )


async def _seed(db: AsyncSession, *rows: object) -> None:
    for row in rows:
        db.add(row)
    await db.flush()


async def _versions_of(db: AsyncSession, document_id: str) -> list[tuple[int, str]]:
    """这篇文档剩下的 (稿号, 状态)，按稿号升序。"""

    rows = (
        await db.execute(
            select(DocumentVersion.version, DocumentVersion.status)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


# ──────────────────────────────────────────── 仓储：窗口查询（只挑归档稿）


@pytest.mark.asyncio
async def test_window_query_returns_archived_versions_at_or_below_the_bound(
    retention_db: AsyncSession,
) -> None:
    """``version <= 界`` 的**归档**行：当前稿行与更高稿号一律不动。

    「归档」这个条件是「当前稿永不删」的落点——它不是服务里的一句 if，而是查询的
    ``WHERE``：上面那行 ``current`` 就算稿号落在窗口外（库里残留一条低稿号的当前行），
    这条查询也永远不会把它交出去。
    """

    await _seed(
        retention_db,
        _version(document_id="doc-a", version=1, status="archived"),
        _version(document_id="doc-a", version=2, status="archived"),
        _version(document_id="doc-a", version=3, status="archived"),
        _version(document_id="doc-a", version=4, status="current"),
        _version(document_id="doc-b", version=1, status="archived"),
    )

    rows = await document_version_repository.list_archived_at_or_below(
        retention_db, "doc-a", version=2
    )

    assert [row.version for row in rows] == [2, 1], "只挑该文档、该界以下的归档稿"


@pytest.mark.asyncio
async def test_window_query_keeps_a_low_current_pointer_row_even_below_the_bound(
    retention_db: AsyncSession,
) -> None:
    """指针在 v9、当前稿行却还是 v1（历史事故）时，v1 依然不许被扫出来。

    窗口按**指针**算，但「删谁」按**状态**判：两条口径分开写，才会在数据不正常时仍然
    保住「当前稿永不删」。
    """

    await _seed(
        retention_db,
        _document(document_id="doc-a", current_version=9),
        _version(document_id="doc-a", version=1, status="current"),
        _version(document_id="doc-a", version=8, status="archived"),
    )

    rows = await document_version_repository.list_archived_at_or_below(
        retention_db, "doc-a", version=6
    )

    assert [row.version for row in rows] == []


# ──────────────────────────────────────────── 仓储：按稿号删（不提交）


@pytest.mark.asyncio
async def test_deleting_versions_touches_only_the_given_drafts_of_that_document(
    retention_db: AsyncSession,
) -> None:
    """``DELETE ... IN (稿号)`` 只打这篇文档的这几个稿号，别的文档原样留着。"""

    await _seed(
        retention_db,
        _version(document_id="doc-a", version=1),
        _version(document_id="doc-a", version=2),
        _version(document_id="doc-a", version=3),
        _version(document_id="doc-b", version=1),
    )
    await retention_db.commit()

    await document_version_repository.delete_versions(retention_db, "doc-a", (1, 2))

    assert await _versions_of(retention_db, "doc-a") == [(3, "archived")]
    assert await _versions_of(retention_db, "doc-b") == [(1, "archived")]


@pytest.mark.asyncio
async def test_deleting_versions_does_not_commit_and_an_empty_list_is_a_no_op(
    retention_db: AsyncSession,
) -> None:
    """仓储不提交（提交点只有事务入口）；没有要删的稿号时不发语句、不报错。"""

    await _seed(retention_db, _version(document_id="doc-a", version=1))
    await retention_db.commit()

    await document_version_repository.delete_versions(retention_db, "doc-a", ())
    assert await _versions_of(retention_db, "doc-a") == [(1, "archived")]

    await document_version_repository.delete_versions(retention_db, "doc-a", (1,))
    assert await _versions_of(retention_db, "doc-a") == [], "DELETE 已经发出去了"

    await retention_db.rollback()

    assert await _versions_of(retention_db, "doc-a") == [(1, "archived")], (
        "仓储自己提交过就会在这里露出来"
    )


# ──────────────────────────────────────── 文档扫描：窗口可能非空的那些文档


@pytest.mark.asyncio
async def test_scan_returns_only_documents_whose_window_can_be_non_empty(
    retention_db: AsyncSession,
) -> None:
    """``current_version > retain`` 才可能有过期稿（``current - retain >= 1``）。

    边界写成 ``>`` 不是 ``>=``：稿号 3 而窗口留 3 稿时，窗口下界是 0，
    一篇文档都不该被扫出来——``>=`` 会让运行到「第 4 稿」之前的每个文档都白跑一趟。
    """

    await _seed(
        retention_db,
        _document(document_id="doc-3", current_version=3),
        _document(document_id="doc-4", current_version=4),
        _document(document_id="doc-5", current_version=5),
        _document(document_id="doc-6-other-kb", current_version=6, kb_id=OTHER_KB_ID),
    )

    found = await document_repository.list_with_expired_versions(retention_db, retain=3)

    assert {doc.id for doc in found} == {"doc-4", "doc-5", "doc-6-other-kb"}


@pytest.mark.asyncio
async def test_scan_can_be_narrowed_by_kb_and_capped(retention_db: AsyncSession) -> None:
    """``--kb-id`` 收窄范围、``--limit`` 收敛条数：运维先看一小批再决定。"""

    await _seed(
        retention_db,
        _document(document_id="doc-a", current_version=9),
        _document(document_id="doc-b", current_version=9),
        _document(document_id="doc-c", current_version=9, kb_id=OTHER_KB_ID),
    )

    only_kb = await document_repository.list_with_expired_versions(
        retention_db, retain=3, kb_id=KB_ID
    )
    capped = await document_repository.list_with_expired_versions(
        retention_db, retain=3, kb_id=KB_ID, limit=1
    )

    assert {doc.id for doc in only_kb} == {"doc-a", "doc-b"}
    assert len(capped) == 1
