"""#175 / #182 · 版本行的**写入侧**仓储：真 SQL、真唯一约束，但不连 PostgreSQL。

建版本这条路径要动 ``document_versions`` 四件事：算下一个稿号、落新行、把旧稿转归档、
按稿号列版本。它们全部收在仓储里，所以这里用 ``sqlite+aiosqlite`` 的真实会话验——
唯一约束、``UPDATE ... WHERE``、``ORDER BY`` 都是数据库才有答案的东西，替身会话对这些
只会点头（先例：``tests/test_document_version_delete.py``）。

#182 的重解析写回也在这里验：它动的是**同一行**（正文 + 校验和），稿号 / 状态 / 行数
一个都不许变——那是「正文真值在版本表」这条不变量成立的条件。

**这里不验「并发的两个提交拿到不同稿号」**：SQLite 没有行锁，那条保证由 PostgreSQL 的
``SELECT ... FOR UPDATE`` 给（见 :func:`test_the_lock_statement_is_a_locking_read` 与
``tests/test_document_version_apis.py`` 里的冲突映射）。仓储能守的是形状与范围：锁语句
真的带 ``FOR UPDATE``、``UPDATE`` 只碰这篇文档的当前稿。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.repositories import document_repository, document_version_repository
from app.utils.status import DocumentStatus


@pytest_asyncio.fixture
async def version_db() -> AsyncIterator[AsyncSession]:
    """真 SQL 的两张表（``documents`` + ``document_versions``）。

    ``document_versions`` 的 ``SELECT`` 锁语句在 SQLite 上会被方言忽略（SQLite 没有行锁），
    所以这里能验的是「语句形状」与「范围条件」，并发语义靠 PostgreSQL——这一点在模块
    文档里写明，免得后人以为这几条用例钉住了行锁。
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


def _version(
    *,
    version_id: str,
    document_id: str,
    version: int,
    status: str = "current",
    content: str = "退款流程见附件。",
) -> DocumentVersion:
    return DocumentVersion(
        id=version_id,
        document_id=document_id,
        version=version,
        content=content,
        checksum="0" * 64,
        status=status,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )


def _document(*, document_id: str, current_version: int = 1) -> Document:
    return Document(
        id=document_id,
        tenant_id="tenant-a",
        kb_id="kb-1",
        title="积分规则",
        content="",
        status=DocumentStatus.SUCCESS,
        current_version=current_version,
    )


async def _seed(db: AsyncSession, *rows: object) -> None:
    for row in rows:
        db.add(row)
    await db.flush()


# ── 下一个稿号 ──


@pytest.mark.asyncio
async def test_max_version_is_zero_when_the_document_has_no_versions(
    version_db: AsyncSession,
) -> None:
    """没有版本行 → 0（于是下一个稿号是 1），不是 ``None``。

    ``None`` 会让调用方在算 ``+1`` 时炸在别的行上，而「这篇文档还没建过版本」本来就是
    一个合法状态——#172 之前上传的文档全是这样。
    """

    assert await document_version_repository.max_version(version_db, "doc-1") == 0


@pytest.mark.asyncio
async def test_max_version_counts_only_that_document(version_db: AsyncSession) -> None:
    """稿号是**每篇文档各自**的时间轴：别的文档写到第 9 稿，不影响这篇从 2 往下走。"""

    await _seed(
        version_db,
        _version(version_id="va1", document_id="doc-a", version=1, status="archived"),
        _version(version_id="va2", document_id="doc-a", version=2),
        _version(version_id="vb9", document_id="doc-b", version=9),
    )

    assert await document_version_repository.max_version(version_db, "doc-a") == 2


# ── 落新行 ──


@pytest.mark.asyncio
async def test_create_flushes_but_does_not_commit(version_db: AsyncSession) -> None:
    """新行 ``flush`` 之后立刻可读（同一条语句里要按稿号读回来），但**不提交**。

    提交点只在请求边界（``app.db.transaction``）：这里自己提交就会让「建版本失败但旧稿
    已归档」这种半份状态落库。
    """

    await document_version_repository.create(
        version_db, _version(version_id="v1", document_id="doc-1", version=1)
    )

    found = await document_version_repository.get_by_version(version_db, "doc-1", 1)
    assert found is not None
    assert found.id == "v1"

    await version_db.rollback()

    assert await document_version_repository.get_by_version(version_db, "doc-1", 1) is None


# ── 重解析写回同一稿（#182）──


@pytest.mark.asyncio
async def test_rewrite_content_updates_text_and_checksum_on_the_same_row(
    version_db: AsyncSession,
) -> None:
    """重解析写回**同一行**：正文与校验和换掉，稿号 / 状态 / 创建时间 / 行数一个不动。

    「不新增版本行」是这条票的要害：多出来的那一行会在时间轴上凭空多出一稿，而 T10 的
    对账第一条硬检查（文档正文 == 当前稿正文）从此永远红。状态也不许动——重解析不是
    切稿，归档稿重新解析完仍是归档稿。
    """

    from app.utils.checksum import checksum_of

    created_at = datetime(2026, 11, 15, 0, 0, 0)
    original = DocumentVersion(
        id="v1",
        document_id="doc-1",
        version=1,
        content="旧解析结果（2025 版）",
        checksum=checksum_of("旧解析结果（2025 版）"),
        status="archived",
        created_at=created_at,
    )
    await _seed(version_db, original)

    await document_version_repository.rewrite_content(
        version_db, original, content="新解析结果（PDF 解析器）"
    )

    rows = await document_version_repository.list_by_document(version_db, "doc-1")
    assert len(rows) == 1, "重解析不新增版本行"
    row = rows[0]
    assert (row.id, row.version, row.status) == ("v1", 1, "archived"), "行身份、稿号、状态不变"
    assert row.created_at == created_at
    assert row.content == "新解析结果（PDF 解析器）"
    assert row.checksum == checksum_of("新解析结果（PDF 解析器）"), "校验和跟着正文一起换"
    assert row.checksum != checksum_of("旧解析结果（2025 版）")


# ── 归档旧稿 ──


@pytest.mark.asyncio
async def test_archiving_keeps_the_new_draft_current_and_touches_only_current_rows(
    version_db: AsyncSession,
) -> None:
    """把除 ``keep`` 之外的**当前稿**行转归档；归档稿与别的文档原样不动。

    「除 keep 之外」而不是「比 keep 小的」：万一库里残留一条更高稿号的当前行（历史事故），
    「每篇文档恰好一条当前稿版本行」也不能被它破掉。
    """

    await _seed(
        version_db,
        _version(version_id="va1", document_id="doc-a", version=1, status="archived"),
        _version(version_id="va2", document_id="doc-a", version=2),
        _version(version_id="va3", document_id="doc-a", version=3),
        _version(version_id="vb2", document_id="doc-b", version=2),
    )

    await document_version_repository.archive_current_except(version_db, "doc-a", keep=3)

    rows = (
        await version_db.execute(
            select(DocumentVersion).order_by(DocumentVersion.document_id, DocumentVersion.version)
        )
    ).scalars()
    assert [(row.document_id, row.version, row.status) for row in rows] == [
        ("doc-a", 1, "archived"),
        ("doc-a", 2, "archived"),
        ("doc-a", 3, "current"),
        ("doc-b", 2, "current"),
    ]


# ── 锁文档行 ──


def test_the_lock_statement_is_a_locking_read() -> None:
    """写路径的串行化靠这一句：``SELECT ... FOR UPDATE``。

    没有它，两个提交会各自算出同一个 ``max+1``，然后由唯一约束把**其中一个**打回去——
    用户看到的是「请重试」而本不该看到。SQL 形状是这条保证的唯一可离线断言处，所以
    期望值写成 SQL 关键字，而不是「调用过某个函数」。
    """

    statement = document_repository.lock_statement("doc-1", tenant_id="tenant-a")
    sql = str(statement)

    assert statement._for_update_arg is not None, "不是锁读：两个提交会算出同一个稿号"
    assert "tenant_id" in sql, "跨租户必须一律「不存在」"
    assert sorted(statement.compile().params.values()) == ["doc-1", "tenant-a"]


@pytest.mark.asyncio
async def test_locking_reads_the_row_and_misses_other_tenants(version_db: AsyncSession) -> None:
    """锁读同样带租户条件：别的租户来锁这篇文档，什么都拿不到（不泄露存在性）。"""

    await _seed(version_db, _document(document_id="doc-1"))

    locked = await document_repository.lock_for_version(version_db, "doc-1", tenant_id="tenant-a")
    assert locked is not None
    assert locked.id == "doc-1"

    assert (
        await document_repository.lock_for_version(version_db, "doc-1", tenant_id="tenant-b")
        is None
    )


# ── 列版本 ──


@pytest.mark.asyncio
async def test_listing_returns_the_newest_draft_first(version_db: AsyncSession) -> None:
    """按稿号**倒序**：调用方打开版本列表先看到的是现在这一稿。"""

    await _seed(
        version_db,
        _version(version_id="v1", document_id="doc-1", version=1, status="archived"),
        _version(version_id="v3", document_id="doc-1", version=3),
        _version(version_id="v2", document_id="doc-1", version=2, status="archived"),
        _version(version_id="other", document_id="doc-2", version=7),
    )

    rows = await document_version_repository.list_by_document(version_db, "doc-1")

    assert [row.version for row in rows] == [3, 2, 1]
