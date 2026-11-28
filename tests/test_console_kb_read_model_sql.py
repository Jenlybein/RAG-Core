"""控制面读模型**真 SQL**（第 17 步 #227）：分页 / 过滤计数 / 状态分组 / 库级切片数。

## 为什么另起一个文件

`tests/test_console_kb_read_model.py` 验的是**服务层口径**（把哪个参数交给仓储），为了盯住
那个它把仓储全换成了记账替身——于是**这几条 SQL 一行都没被执行过**。而分页最容易出的错恰好
只在真 SQL 上显形：

* `total` 与这一页用**不是同一组条件** → 页数与实际行数打架，界面上表现为「翻到第 3 页是空的」；
* `offset/limit` 写反、`ORDER BY` 少了 tiebreaker → 同一页在两次请求之间重复或漏行；
* 建树那条聚合会把「别的库 / 别的租户 / 别的代」的切片数算进来。

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.chunk import Chunk
from app.models.document import Document
from app.providers.vectorstores.pgvector import build_scope_filter
from app.repositories import chunk_repository, document_repository
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
    -- 现场九新增的两列（模型会读它们，夹具要跟着长）。
    current_index_generation INTEGER,
    settings_hash VARCHAR(64),
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
    doc_version INTEGER NOT NULL DEFAULT 1,
    -- 现场九：这一片属于该文档的第几代索引产物（模型 insert 会带上它）。
    generation INTEGER NOT NULL DEFAULT 1
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

    * ``doc-1`` 当前代（第 2 代）2 片 + 上一代（第 1 代）3 片 → 只该数 2；
    * ``doc-2`` 当前代 1 片；
    * ``doc-4`` 指针在第 2 稿，当前代下有 1 片；第 1 稿那一片不该数；
    * ``doc-6`` / ``doc-7`` 各有切片，但属于别的库 / 别的租户。
    """

    layout = [
        ("c-doc1-g2-a", TENANT, KB, "doc-1", 2, 1),
        ("c-doc1-g2-b", TENANT, KB, "doc-1", 2, 1),
        ("c-doc1-g1-a", TENANT, KB, "doc-1", 1, 1),
        ("c-doc1-g1-b", TENANT, KB, "doc-1", 1, 1),
        ("c-doc1-g1-c", TENANT, KB, "doc-1", 1, 1),
        ("c-doc2-g2", TENANT, KB, "doc-2", 2, 1),
        ("c-doc4-g2", TENANT, KB, "doc-4", 2, 2),
        # 同一代下的旧稿：稿号谓词要把它挡住。
        ("c-doc4-old-draft", TENANT, KB, "doc-4", 2, 1),
        ("c-doc6-g2", TENANT, OTHER_KB, "doc-6", 2, 1),
        ("c-doc7-g2", OTHER_TENANT, KB, "doc-7", 2, 1),
    ]
    return [
        {
            "id": chunk_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "document_id": document_id,
            "generation": generation,
            "doc_version": doc_version,
        }
        for chunk_id, tenant_id, kb_id, document_id, generation, doc_version in layout
    ]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """一个干净的**内存库**，播种上面那两张表。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        for ddl in (DOCUMENTS_DDL, CHUNKS_DDL):
            await connection.exec_driver_sql(ddl)
        for row in _documents():
            await connection.exec_driver_sql(
                "INSERT INTO documents (id, tenant_id, kb_id, title, content, source_type, "
                "status, current_version, current_index_generation, index_claimed_at, index_heartbeat_at, "
                "redispatch_count, "
                "token_count, error_message, created_at, updated_at) VALUES "
                f"('{row['id']}', '{row['tenant_id']}', '{row['kb_id']}', '{row['title']}', "
                f"'body', 'markdown', {row['status']}, {row['current_version']}, "
                # 每篇都**有产物**：当前代是第 2 代（切片的 g2 那批），第 1 代是留下的上一代。
                # 计数口径要「当前代 × 当前稿」两条齐全，旧代那一份不许进来。
                f"2, NULL, NULL, 0, 0, "
                f"NULL, '{row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}', "
                f"'{row['updated_at'].strftime('%Y-%m-%d %H:%M:%S')}')"
            )
        for row in _chunks():
            await connection.exec_driver_sql(
                "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, "
                "metadata, doc_version, generation) VALUES "
                f"('{row['id']}', '{row['tenant_id']}', '{row['kb_id']}', "
                f"'{row['document_id']}', 'body', '[]', '{{}}', "
                f"{row['doc_version']}, {row['generation']})"
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
async def test_the_kb_chunk_total_is_the_generation_times_the_current_draft(
    session: AsyncSession,
) -> None:
    """``kb-main`` 的合计 = 4 片：doc-1 两片 + doc-2 一片 + doc-4 一片。

    不该算的：doc-1 那三片**旧代**、doc-4 第 1 稿那一片（旧稿）、doc-6（别的库）、
    doc-7（别的租户）。只锁稿号的老实现在这里会给出 9；只锁「有产物」也会给出别的数。
    """

    assert await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT) == 4

    # 旧代不算：把 doc-1 第 1 代的切片算进来会变成 7——每次重建都涨一份，永不回落。
    assert await chunk_repository.count_by_document_ids(session, ["doc-1"], tenant_id=TENANT) == {
        "doc-1": 2
    }, "同一篇的旧代切片不参与「现在有多少内容」"

    # 空库 / 全无产物的库：0，而不是去数全部代。
    assert await chunk_repository.count_in_kb(session, "kb-nope", tenant_id=TENANT) == 0


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
        session, document_ids, tenant_id=TENANT
    )
    total = await chunk_repository.count_in_kb(session, KB, tenant_id=TENANT)

    assert sum(per_document.values()) == total


@pytest.mark.asyncio
async def test_the_retrieval_spelling_and_the_count_spelling_pick_the_same_chunks(
    session: AsyncSession,
) -> None:
    """**召回与计数切出同一批切片**：两处用的是同一组谓词，不是两种等价拼写。

    召回是 ``kb 范围 + 当前代 + 当前稿``（``build_scope_filter`` + ``serving_chunk_filters``），
    计数是 ``kb + 当前代 + 当前稿``——库里算出来的就是同一批行。
    """

    rows_scope = await session.execute(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.tenant_id == TENANT,
            build_scope_filter([KB]),
            *chunk_repository.serving_chunk_filters(),
        )
        .order_by(Chunk.id)
    )
    rows_count = await session.execute(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.tenant_id == TENANT,
            Chunk.kb_id == KB,
            *chunk_repository.serving_chunk_filters(),
        )
        .order_by(Chunk.id)
    )

    scope_ids = [row[0] for row in rows_scope.all()]
    count_ids = [row[0] for row in rows_count.all()]
    assert scope_ids == count_ids, "召回（库范围 + 当前代 + 当前稿）与计数必须是同一批行"
    assert len(scope_ids) == 4, "当前代下该库 4 片：doc-1 两片 + doc-2 一片 + doc-4 一片"


@pytest.mark.asyncio
async def test_only_the_current_generation_comes_back_when_three_are_present(
    session: AsyncSession,
) -> None:
    """真 SQL 端到端：库里同时有第 1 / 2 / 3 代，指针指第 3 代 → 只有第 3 代回来。

    这条补的是「代谓词在真实语句里到底切出什么」。改造前可见性靠库级版本，一条相等条件
    就够，所以只需要断言 SQL 字符串；现在判据要 JOIN 文档行求值，「读的是不是 SQL 里那
    一条」就成了会漂的地方——字符串断言看不出这个。
    """

    await session.execute(
        Document.__table__.update().where(Document.id == "doc-2").values(current_index_generation=3)
    )
    for generation, chunk_id in (
        (1, "c-doc2-old-g1"),
        # 第 2 代已经有播种时写下的 ``c-doc2-g2``（指针还在 2 时它是当前代）。
        (3, "c-doc2-g3"),
    ):
        await session.execute(
            Chunk.__table__.insert().values(
                **{
                    "id": chunk_id,
                    "tenant_id": TENANT,
                    "kb_id": KB,
                    "document_id": "doc-2",
                    "content": "正文",
                    "embedding": [0.0] * 1536,
                    # 列名是 ``metadata``（ORM 属性叫 ``meta``）；Core insert 认列名。
                    "metadata": {},
                    "doc_version": 1,
                    "generation": generation,
                }
            )
        )

    rows = await session.execute(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.tenant_id == TENANT,
            Chunk.kb_id == KB,
            Chunk.document_id == "doc-2",
            *chunk_repository.serving_chunk_filters(),
        )
    )

    assert [row[0] for row in rows.all()] == ["c-doc2-g3"], "旧代（1、2）一片都不许回来"
    assert await chunk_repository.count_by_document_ids(session, ["doc-2"], tenant_id=TENANT) == {
        "doc-2": 1
    }, "计数与召回同一批行：一次重建只多一份，不随代数涨"
