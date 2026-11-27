"""控制面读模型**真 SQL**（第 17 步 #227）：分页 / 过滤计数 / 状态分组 / 库级切片数 / 构建行。

## 为什么另起一个文件

`tests/test_console_kb_read_model.py` 验的是**服务层口径**（把哪个参数交给仓储、第二根轴
怎么判），为了盯住那个它把仓储全换成了记账替身——于是**这四条新 SQL 一行都没被执行过**。
而分页最容易出的错恰好只在真 SQL 上显形：

* `total` 与这一页用**不是同一组条件** → 页数与实际行数打架，界面上表现为「翻到第 3 页是空的」；
* `offset/limit` 写反、`ORDER BY` 少了 tiebreaker → 同一页在两次请求之间重复或漏行；
* 建树那条聚合会把「别的库 / 别的租户 / 别的版本」的切片数算进来。

所以这里在**内存 SQLite** 上真跑一遍（与 `test_document_chunk_count.py` 同一套办法：
`create_async_engine("sqlite+aiosqlite://")` + 手写 DDL + 播种）。SQLite 与 PostgreSQL 在这
几条语句上没有方言差异（都是 `WHERE / ORDER BY / OFFSET / LIMIT / GROUP BY`），而它能在 CI
的离线 job 里跑——真库端到端另在容器里做一次（见改造报告）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
)
from app.repositories import chunk_repository, document_repository, index_version_repository
from app.utils.status import DocumentStatus

TENANT = "tenant-read-model-sql"
OTHER_TENANT = "tenant-other"
KB = "kb-main"
OTHER_KB = "kb-other"

DOCUMENTS_DDL = """
CREATE TABLE documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    title VARCHAR(512) NOT NULL,
    content TEXT NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_file_path VARCHAR(1024),
    source_filename VARCHAR(512),
    status INTEGER NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    index_claimed_at DATETIME,
    index_heartbeat_at DATETIME,
    redispatch_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

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
    doc_version INTEGER NOT NULL DEFAULT 1
)
"""

BUILD_VERSIONS_DDL = """
CREATE TABLE index_build_versions (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    version VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    document_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    activated_at DATETIME,
    archived_at DATETIME,
    redispatch_count INTEGER NOT NULL DEFAULT 0,
    documents_planned INTEGER NOT NULL DEFAULT 0,
    documents_done INTEGER NOT NULL DEFAULT 0,
    failed_documents JSON,
    doc_version INTEGER,
    params JSON,
    auto_activate BOOLEAN NOT NULL DEFAULT 0,
    converge_deadline DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

BASE = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


def _documents() -> list[dict[str, Any]]:
    """七篇文档，把分页/筛选/状态分组三条口径需要的形状都摆上：

    * 五篇属于 ``kb-main``，其中成功 3 / 失败 1 / 排队中 1；
    * 一篇属于 ``kb-other``（不该出现在这个库的任何计数里）；
    * 一篇属于**别的租户**（同理）；
    * 标题里只有两篇含 ``policy``（服务端关键字筛选要用到）。
    """

    rows: list[dict[str, Any]] = []
    layout = [
        ("doc-1", TENANT, KB, "api_reference.md", DocumentStatus.SUCCESS, 1),
        ("doc-2", TENANT, KB, "billing_policy.md", DocumentStatus.SUCCESS, 1),
        ("doc-3", TENANT, KB, "refund_policy.md", DocumentStatus.FAILED, 1),
        ("doc-4", TENANT, KB, "faq.md", DocumentStatus.SUCCESS, 2),
        ("doc-5", TENANT, KB, "notes.md", DocumentStatus.QUEUED, 1),
        ("doc-6", TENANT, OTHER_KB, "elsewhere_policy.md", DocumentStatus.SUCCESS, 1),
        ("doc-7", OTHER_TENANT, KB, "foreign_policy.md", DocumentStatus.SUCCESS, 1),
    ]
    for index, (document_id, tenant_id, kb_id, title, status, current_version) in enumerate(layout):
        # 创建时间递增：分页按 `created_at DESC` 排序，顺序必须是确定的，
        # 否则「第 1 页有哪些行」会在两次请求之间变化。
        created = BASE + timedelta(minutes=index)
        rows.append(
            {
                "id": document_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "title": title,
                "status": int(status),
                "current_version": current_version,
                "created_at": created,
                "updated_at": created,
            }
        )
    return rows


def _chunks() -> list[dict[str, Any]]:
    """切片：**两轴各留一个"不该数"的格子"**，外加别的库 / 别的租户。

    * ``doc-1`` 当前稿 2 片（v2 生效版本）+ v1 旧版本 3 片 → 只该数 2；
    * ``doc-2`` 当前稿 1 片；
    * ``doc-3`` 当前稿在 v2 下 0 片（失败那篇）；
    * ``doc-4`` 指针在第 2 稿，v2 下有 1 片；第 1 稿的两片不该数；
    * ``doc-6`` / ``doc-7`` 各有切片，但属于别的库 / 别的租户。
    """

    layout = [
        ("c-doc1-v2-a", TENANT, KB, "doc-1", "v2", 1),
        ("c-doc1-v2-b", TENANT, KB, "doc-1", "v2", 1),
        ("c-doc1-v1-a", TENANT, KB, "doc-1", "v1", 1),
        ("c-doc1-v1-b", TENANT, KB, "doc-1", "v1", 1),
        ("c-doc1-v1-c", TENANT, KB, "doc-1", "v1", 1),
        ("c-doc2-v2", TENANT, KB, "doc-2", "v2", 1),
        ("c-doc4-v2", TENANT, KB, "doc-4", "v2", 2),
        ("c-doc4-v1-old", TENANT, KB, "doc-4", "v2", 1),
        ("c-doc6-v2", TENANT, OTHER_KB, "doc-6", "v2", 1),
        ("c-doc7-v2", OTHER_TENANT, KB, "doc-7", "v2", 1),
    ]
    return [
        {
            "id": chunk_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "document_id": document_id,
            "index_version": index_version,
            "doc_version": doc_version,
        }
        for chunk_id, tenant_id, kb_id, document_id, index_version, doc_version in layout
    ]


def _build_versions() -> list[dict[str, Any]]:
    """四行构建：``building`` 最旧、``failed`` 最新——``latest_for_kb`` 必须选 ``building``。

    「最新」与「进行中」在这里**故意冲突**：若实现按时间取最新，它会拿到 ``failed``，
    而那正是要防的错（用户点进详情时，界面要看的往往是还在跑的那次）。
    """

    layout = [
        ("build-v1", "v1", INDEX_BUILD_ARCHIVED, 5, 5, BASE),
        ("build-v2", "v2", INDEX_BUILD_ACTIVE, 5, 5, BASE + timedelta(hours=1)),
        ("build-v3", "v3", INDEX_BUILD_BUILDING, 5, 2, BASE + timedelta(hours=2)),
        ("build-v4", "v4", INDEX_BUILD_FAILED, 5, 5, BASE + timedelta(hours=3)),
    ]
    return [
        {
            "id": build_id,
            "tenant_id": TENANT,
            "kb_id": KB,
            "version": version,
            "status": status,
            "planned": planned,
            "done": done,
            "created_at": created,
            "updated_at": created,
        }
        for build_id, version, status, planned, done, created in layout
    ]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """一个干净的**内存库**，播种上面那三张表。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        for ddl in (DOCUMENTS_DDL, CHUNKS_DDL, BUILD_VERSIONS_DDL):
            await connection.exec_driver_sql(ddl)
        for row in _documents():
            await connection.exec_driver_sql(
                "INSERT INTO documents (id, tenant_id, kb_id, title, content, source_type, "
                "status, current_version, index_claimed_at, index_heartbeat_at, redispatch_count, "
                "token_count, error_message, created_at, updated_at) VALUES "
                f"('{row['id']}', '{row['tenant_id']}', '{row['kb_id']}', '{row['title']}', "
                f"'body', 'markdown', {row['status']}, {row['current_version']}, NULL, NULL, 0, 0, "
                f"NULL, '{row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}', "
                f"'{row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')}')"
            )
        for row in _chunks():
            await connection.exec_driver_sql(
                "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, "
                "metadata, index_version, doc_version) VALUES "
                f"('{row['id']}', '{row['tenant_id']}', '{row['kb_id']}', "
                f"'{row['document_id']}', 'body', '[]', '{{}}', "
                f"'{row['index_version']}', {row['doc_version']})"
            )
        for row in _build_versions():
            await connection.exec_driver_sql(
                "INSERT INTO index_build_versions (id, tenant_id, kb_id, version, status, "
                "documents_planned, documents_done, failed_documents, doc_version, params, "
                "auto_activate, converge_deadline, created_at, updated_at) VALUES "
                f"('{row['id']}', '{row['tenant_id']}', '{row['kb_id']}', '{row['version']}', "
                f"'{row['status']}', {row['planned']}, {row['done']}, '[]', 1, NULL, 1, NULL, "
                f"'{row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}', "
                f"'{row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')}')"
            )
    async with AsyncSession(engine, expire_on_commit=False) as active:
        yield active
    await engine.dispose()


# ── 分页 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_page_is_sliced_in_sql_and_excludes_other_kbs_and_tenants(
    session: AsyncSession,
) -> None:
    """``kb-main`` 只有 5 篇（别的库 1 篇、别的租户 1 篇都不算）。

    第 1 页每页 2 行 → 拿到**最新**的两篇（`created_at DESC`）：doc-5、doc-4。
    """

    rows = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=0, limit=2
    )
    assert [row.id for row in rows] == ["doc-5", "doc-4"], (
        "按创建时间倒序：最新的两篇；doc-6（别的库）与 doc-7（别的租户）不许出现"
    )

    page3 = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=4, limit=2
    )
    assert [row.id for row in page3] == ["doc-1"], "第 3 页只剩 1 行（5 行里的最后一个）"


@pytest.mark.asyncio
async def test_the_total_uses_the_same_conditions_as_the_page(session: AsyncSession) -> None:
    """``total`` 与这一页用同一组条件：不带筛选时 5，带 ``policy`` 时 2。

    这条是「页数与实际行数打架」的直接判据：总数用一套条件、这一页用另一套时，两者会对不上。
    """

    assert await document_repository.count_by_kb_filtered(session, KB, tenant_id=TENANT) == 5
    assert (
        await document_repository.count_by_kb_filtered(
            session, KB, tenant_id=TENANT, keyword="policy"
        )
        == 2
    )

    page = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=0, limit=10, keyword="policy"
    )
    assert [row.id for row in page] == ["doc-3", "doc-2"], (
        "billing_policy / refund_policy：最新的那篇在前"
    )


@pytest.mark.asyncio
async def test_a_status_filter_narrows_both_the_page_and_the_total(
    session: AsyncSession,
) -> None:
    """按状态筛：``documents`` 与 ``total`` 一起变（不会出现「总数 5、这一页 1 行」）。"""

    rows = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=0, limit=10, status=DocumentStatus.SUCCESS
    )
    assert [row.id for row in rows] == ["doc-4", "doc-2", "doc-1"]
    assert (
        await document_repository.count_by_kb_filtered(
            session, KB, tenant_id=TENANT, status=DocumentStatus.SUCCESS
        )
        == 3
    )


@pytest.mark.asyncio
async def test_a_keyword_filter_is_case_insensitive(session: AsyncSession) -> None:
    """标题是中文 + 英文混排，用户不会为一次搜索去在意大小写（``ILIKE``）。"""

    rows = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=0, limit=10, keyword="POLICY"
    )
    assert {row.id for row in rows} == {"doc-2", "doc-3"}


@pytest.mark.asyncio
async def test_a_keyword_with_a_wildcard_character_still_matches_literally(
    session: AsyncSession,
) -> None:
    """``%`` 与 ``_`` 是 LIKE 的通配符——用户搜它就是搜这个字符，不该命中全部。

    这一条钉的是「参数直接拼进 LIKE 模式」这种写法：它不会报错，只会把「搜一个下划线」
    变成「返回整库」。
    """

    assert (
        await document_repository.count_by_kb_filtered(session, KB, tenant_id=TENANT, keyword="_")
    ) <= 5
    # 转义之后 `policy_` 是**字面量**：没有标题以 `policy_` 开头的一段，
    # 而**不转义**时会命中 `billing_policy.md`（`_` 吃掉了那个句点）。
    rows = await document_repository.list_page_by_kb(
        session, KB, tenant_id=TENANT, offset=0, limit=10, keyword="policy_"
    )
    assert rows == [], (
        "转义之后 `policy_` 是字面量：没有标题含 `policy_`，一片都不该命中。"
        "不转义时它会命中 `billing_policy.md`（`_` 吃掉那个句点）——那才是转义失效的样子"
    )

    # 同一个判据的另一个方向：`%` 不许把整库搜出来。
    assert (
        await document_repository.count_by_kb_filtered(session, KB, tenant_id=TENANT, keyword="%")
        == 0
    ), "搜 `%` 是在搜这个字符本身，不是「全部」"


# ── 状态分组 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_status_grouping_only_counts_this_kb(session: AsyncSession) -> None:
    """四态分组：成功 3 / 失败 1 / 索引中 0 / 排队中 1（别的库与别的租户都不算）。"""

    counts = await document_repository.count_by_kb_by_status(session, KB, tenant_id=TENANT)
    assert counts == {
        int(DocumentStatus.SUCCESS): 3,
        int(DocumentStatus.FAILED): 1,
        int(DocumentStatus.QUEUED): 1,
    }, "索引中 0 篇 → 这个键根本不出现（调用方回落 0）"


@pytest.mark.asyncio
async def test_the_status_grouping_is_empty_for_an_unknown_kb(session: AsyncSession) -> None:
    """不存在的库回空字典（不是一条「0 篇」的假行）。"""

    assert (
        await document_repository.count_by_kb_by_status(session, "kb-nope", tenant_id=TENANT) == {}
    )


# ── 库级切片数（两轴）────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_kb_chunk_total_is_the_active_version_and_the_current_draft(
    session: AsyncSession,
) -> None:
    """``kb-main`` 在 v2 下共 4 片：doc-1 两片 + doc-2 一片 + doc-4 一片。

    不该算的：doc-1 在 v1 的三片（旧版本）、doc-4 第 1 稿那一片（旧稿）、doc-6（别的库）、
    doc-7（别的租户）。旧实现（只锁稿号）在这里会给出 9。
    """

    assert (
        await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT, index_version="v2") == 4
    )
    assert (
        await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT, index_version="v1") == 3
    ), "换成 v1 就是另一版的读数（3 片旧版本切片）"
    assert (
        await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT, index_version=None) == 0
    ), "没有生效版本 → 0，而不是去数全部版本"


@pytest.mark.asyncio
async def test_the_kb_chunk_total_matches_the_sum_of_the_per_document_counts(
    session: AsyncSession,
) -> None:
    """**库级合计 = 逐行之和**：两个数必须对得上，否则界面会自相矛盾。

    卡片上的「个分块」与详情表里每行「分块数」之和对不上时，用户只会觉得「两个数都在瞎报」。
    它们共用 `serving_chunk_filters`，这条用例就是那个「共用」的证据。
    """

    document_ids = [
        row.id
        for row in await document_repository.list_page_by_kb(
            session, KB, tenant_id=TENANT, offset=0, limit=10
        )
    ]
    per_document = await chunk_repository.count_by_document_ids(
        session, document_ids, tenant_id=TENANT, index_version="v2"
    )
    total = await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT, index_version="v2")

    assert sum(per_document.values()) == total


# ── 构建行 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latest_returns_the_building_row_even_when_a_newer_one_exists(
    session: AsyncSession,
) -> None:
    """进行中优先于更新：``v3``（building）赢过更晚创建的 ``v4``（failed）。

    只看时间会让界面在「还在跑」的时候显示那条已经失败的行——进度条变成一句「未完成」，
    而实际上工人正在跑。
    """

    row = await index_version_repository.latest_for_kb(session, KB, tenant_id=TENANT)
    assert row is not None
    assert row.version == "v3"
    assert row.status == INDEX_BUILD_BUILDING


@pytest.mark.asyncio
async def test_when_nothing_is_building_latest_returns_the_newest_row(
    session: AsyncSession,
) -> None:
    """没有进行中的行时取最新：``v4``。收尾之后详情页仍需要上下文（刚才那次成没成）。"""

    # 把 building 那行改成 archived，模拟「这一次收尾了」。
    await session.execute(
        text("UPDATE index_build_versions SET status = 'archived' WHERE id = 'build-v3'")
    )
    row = await index_version_repository.latest_for_kb(session, KB, tenant_id=TENANT)
    assert row is not None
    assert row.version == "v4"


@pytest.mark.asyncio
async def test_latest_never_crosses_tenant_or_kb(session: AsyncSession) -> None:
    """别的库 / 别的租户的构建行不参与：不存在的组合回 ``None``。"""

    assert await index_version_repository.latest_for_kb(session, OTHER_KB, tenant_id=TENANT) is None
    assert await index_version_repository.latest_for_kb(session, KB, tenant_id=OTHER_TENANT) is None
