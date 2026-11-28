"""「N 篇待更新」的判据（现场九·第三步）：真 SQL 上数一遍。

## 这个数字为什么值得单独验

它是**唯一**一个「按篇看得出的结论」：库这一级的 `needs_rebuild` 只能说「要重建」，说不出
「差几篇」。而这个数字一旦算错，方向只有一个——**说少了**（把待更新的篇算成最新），于是人
不重建，错的索引永远留在线上；说多了则是噪音，噪音会让人学会永久忽略它。

所以三条边界都要有用例：指纹一致 / 指纹不一致 / **从没记过指纹**（NULL）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.repositories import document_repository
from app.services.index_generation import settings_fingerprint
from app.utils.status import DocumentStatus

TENANT = "tenant_demo"
KB = "kb-1"

#: 与 ``app/models/document.py`` 同形（只列这一条查询用得到的列）。
#: 手写 DDL 的理由见 ``tests/support/sqlite_schema.py``：JSONB 之类 SQLite 渲染不了，
#: 而这条用例要验的只是 WHERE 子句。
DOCUMENTS_DDL = """
CREATE TABLE documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT 't',
    content TEXT,
    source_type VARCHAR(32) NOT NULL DEFAULT 'markdown',
    source_filename VARCHAR(255),
    status INTEGER NOT NULL DEFAULT 1,
    current_version INTEGER NOT NULL DEFAULT 1,
    current_index_generation INTEGER,
    settings_hash VARCHAR(64),
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CURRENT = settings_fingerprint(
    {"split_strategy": "structure", "chunk_size": 512, "section_part_max_chars": 2000}
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(text(DOCUMENTS_DDL))
    async with AsyncSession(engine) as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


async def _add(
    db: AsyncSession,
    document_id: str,
    *,
    settings_hash: str | None,
    status: int = DocumentStatus.SUCCESS,
) -> None:
    await db.execute(
        text(
            "INSERT INTO documents (id, tenant_id, kb_id, status, settings_hash) "
            "VALUES (:id, :tenant, :kb, :status, :hash)"
        ),
        {"id": document_id, "tenant": TENANT, "kb": KB, "status": status, "hash": settings_hash},
    )


async def _count(db: AsyncSession, fingerprint: str | None) -> int:
    return await document_repository.count_stale_generations(
        db, KB, tenant_id=TENANT, settings_hash=fingerprint
    )


@pytest.mark.asyncio
async def test_a_document_built_with_the_current_settings_is_not_stale(
    session: AsyncSession,
) -> None:
    await _add(session, "doc-ok", settings_hash=CURRENT)

    assert await _count(session, CURRENT) == 0


@pytest.mark.asyncio
async def test_a_document_built_with_other_settings_is_stale(session: AsyncSession) -> None:
    await _add(session, "doc-ok", settings_hash=CURRENT)
    await _add(session, "doc-old", settings_hash="f" * 64)

    assert await _count(session, CURRENT) == 1


@pytest.mark.asyncio
async def test_a_document_without_any_fingerprint_counts_as_stale(session: AsyncSession) -> None:
    """``NULL`` = 「说不清它用的是哪套设置」。

    不能默认它是最新的：那会让第一批上线前的存量文档全部显示「已最新」，而它们恰恰是
    最可能用旧切法建出来的那批。
    """

    await _add(session, "doc-legacy", settings_hash=None)

    assert await _count(session, CURRENT) == 1


@pytest.mark.asyncio
async def test_an_unknown_current_fingerprint_counts_only_the_ones_never_recorded(
    session: AsyncSession,
) -> None:
    """库的当前设置读不到时，只数「从没记过指纹」的篇，**不**做全库推断。

    那种状态（库被删、跨租户）下真正的问题是「这个库不见了」，界面另有提示；而在异常态里
    把整库标红只会让这条平时都是对的提示失去可信度。
    """

    await _add(session, "doc-recorded", settings_hash=CURRENT)
    await _add(session, "doc-never", settings_hash=None)

    assert await _count(session, None) == 1


@pytest.mark.asyncio
async def test_only_successful_documents_are_counted(session: AsyncSession) -> None:
    """排队 / 在跑 / 失败的篇不进这个数。

    它们本来就会被别的机制处理（队列在跑、清扫器在捡），混进来只会让这个数字跳动而没有
    可对应的动作——而一个不会稳定下来的数字，人看一眼就不再看了。
    """

    await _add(session, "doc-ok", settings_hash=CURRENT)
    await _add(session, "doc-queued", settings_hash=CURRENT, status=DocumentStatus.QUEUED)
    await _add(session, "doc-failed", settings_hash=CURRENT, status=DocumentStatus.FAILED)

    assert await _count(session, CURRENT) == 0


@pytest.mark.asyncio
async def test_another_kb_or_tenant_is_not_counted(session: AsyncSession) -> None:
    """跨库、跨租户一行都不能数进来：这个数字是要拿去指导「点哪个按钮」的。"""

    await _add(session, "doc-ok", settings_hash=CURRENT)
    await session.execute(
        text(
            "INSERT INTO documents (id, tenant_id, kb_id, status, settings_hash) "
            "VALUES ('doc-other-kb', :tenant, 'kb-2', 1, NULL)"
        ),
        {"tenant": TENANT},
    )
    await session.execute(
        text(
            "INSERT INTO documents (id, tenant_id, kb_id, status, settings_hash) "
            "VALUES ('doc-other-tenant', 'tenant_other', :kb, 1, NULL)"
        ),
        {"kb": KB},
    )

    assert await _count(session, CURRENT) == 0
