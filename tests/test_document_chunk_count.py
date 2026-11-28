"""文档列表的 ``chunk_count`` 只数**当前稿**（数据面 ``GET /documents`` 与控制面的知识库树）。

同一个谓词（``chunks.doc_version = documents.current_version``）在检索链路上有五个落点
（``tests/test_retrieval_current_draft.py``），而文档列表的**对外数字**是第六处：它数的是
「这份文档现在有几片可召回」。ADR-0005 选 B（每稿各留一套切片）之后，库里同一篇文档可以
有两三稿的切片同时存在——这一处漏掉谓词时，``chunk_count`` 会变成各留存稿之和（留 3 稿就
约 3 倍），而**没有任何地方会报错**：调用方只是看到一个比实际召回量大的数字，前端拿它做
「这份文档有没有内容」的判断，于是「三稿共 7 片」与「当前稿 2 片」看起来一样可信。

接缝：与 ``test_retrieval_current_draft.py`` 的后半段同一套做法——内存 SQLite 里建真表、
真的写下两稿切片，然后跑**仓储里那条生产语句**（不是复制品）。不上真库、不跑 ASGI：
这里要钉的是「数的是哪些行」，接口层只是把这个字典填进响应（``test_upload_async`` 已钉）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.repositories import chunk_repository

TENANT = "tenant_demo"
OTHER_TENANT = "tenant_other"
KB = "kb-a"

DOCUMENTS_DDL = """
CREATE TABLE documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    current_version INTEGER NOT NULL DEFAULT '1',
    current_index_generation INTEGER,
    settings_hash VARCHAR(64)
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
    doc_version INTEGER NOT NULL DEFAULT '1',
    generation INTEGER NOT NULL DEFAULT 1
)
"""

#: 三篇文档、**留两稿 × 留两版**的现场（两轴各有一个"不该数"的格子）：
#:
#: * ``doc-1``：指针在第 2 稿，第 1 稿 3 片（保留窗口内还在）、第 2 稿 2 片 → 只该数 **2**；
#: * ``doc-2``：只长到第 1 稿、1 片 → **1**；
#: * ``doc-3``：指针已切到第 2 稿、但第 2 稿的切片还没建好（D5 的在途窗口）→ 数 **0**，
#:   且按仓储契约「没有切片的文档不在字典里」，它整条不该出现。
#:
#: 另外每篇的当前稿都多留了一份**第 0 代**（上一次重建替下来的旧代，留着给回滚）：
#: 那几片属于「同一稿、别的代」，**也不该被数进来**——否则每重建一次，界面上的分块数
#: 就涨一份（实测 152 vs 38）。
SEEDED_ROWS = (
    # ``current_index_generation`` 也写成 1：这些文档**有产物**，而计数口径要
    # 「版本 × 当前稿 × 当前代」三条齐全（现场九·第三步）。写成 NULL 等于「还没有产物」，
    # 计数会诚实地回 0——那是另一条用例要覆盖的情形。
    f"INSERT INTO documents (id, tenant_id, kb_id, current_version, current_index_generation) "
    f"VALUES ('doc-1', '{TENANT}', '{KB}', 2, 1)",
    f"INSERT INTO documents (id, tenant_id, kb_id, current_version, current_index_generation) "
    f"VALUES ('doc-2', '{TENANT}', '{KB}', 1, 1)",
    f"INSERT INTO documents (id, tenant_id, kb_id, current_version, current_index_generation) "
    f"VALUES ('doc-3', '{TENANT}', '{KB}', 2, 1)",
    # doc-1 第 1 稿：三片（旧稿，读不到但要占着存储）
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c1-v1', '{TENANT}', '{KB}', 'doc-1', '旧稿一', '[]', "
    f"'{{}}', 'v1', 1)",
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c2-v1', '{TENANT}', '{KB}', 'doc-1', '旧稿二', '[]', "
    f"'{{}}', 'v1', 1)",
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c3-v1', '{TENANT}', '{KB}', 'doc-1', '旧稿三', '[]', "
    f"'{{}}', 'v1', 1)",
    # doc-1 第 2 稿：两片（当前稿）
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c1-v2', '{TENANT}', '{KB}', 'doc-1', '当前稿一', '[]', "
    f"'{{}}', 'v1', 2)",
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c2-v2', '{TENANT}', '{KB}', 'doc-1', '当前稿二', '[]', "
    f"'{{}}', 'v1', 2)",
    # doc-2 第 1 稿：一片
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c1-v1-d2', '{TENANT}', '{KB}', 'doc-2', '只有一稿', "
    f"'[]', '{{}}', 'v1', 1)",
    # doc-3：上一稿的切片还在，当前稿那一片还没建出来
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version) VALUES ('c1-v1-d3', '{TENANT}', '{KB}', 'doc-3', '上一稿', '[]', "
    f"'{{}}', 'v1', 1)",
    # 同一稿、**旧代**（第 0 代）的三片：留着给回滚，但一个都不许数进来
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version, generation) VALUES ('c1-v0-old', '{TENANT}', '{KB}', 'doc-1', '旧版一', '[]', "
    f"'{{}}', 'v1', 2, 0)",
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version, generation) VALUES ('c2-v0-old', '{TENANT}', '{KB}', 'doc-1', '旧版二', '[]', "
    f"'{{}}', 'v1', 2, 0)",
    f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
    f"index_version, doc_version, generation) VALUES ('c1-v0-d2', '{TENANT}', '{KB}', 'doc-2', '旧版', '[]', "
    f"'{{}}', 'v1', 1, 0)",
)


@pytest_asyncio.fixture
async def drafts() -> AsyncIterator[AsyncSession]:
    """一个干净的**内存库**：文档行带当前稿指针，切片分属两稿（一稿一份语句，生产形状）。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        for statement in (DOCUMENTS_DDL, CHUNKS_DDL, *SEEDED_ROWS):
            await connection.execute(text(statement))
    async with AsyncSession(engine) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


def _sql(statement: Any) -> str:
    """编译成压平空白的 SQL（只对结构敏感，不对排版敏感）。"""

    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


async def _counts(drafts: AsyncSession) -> dict[str, int]:
    return await chunk_repository.count_by_document_ids(
        drafts,
        ["doc-1", "doc-2", "doc-3"],
        tenant_id=TENANT,
        # 生效版本是 v1；v0 是被重建替下来的那一版（留着给回滚），不参与计数。
    )


@pytest.mark.asyncio
async def test_the_count_covers_only_the_current_draft(drafts: AsyncSession) -> None:
    """同一文档两稿各若干片：``chunk_count`` 只算**当前稿**，不是各稿之和。

    旧口径（没有当前稿条件）在这里会给出 ``doc-1: 5``——三片旧稿一起被数进去。这个数字是
    对外可见的（``GET /documents`` 与控制面知识库树都用它），前端据此判断「这份文档有没有
    内容」，所以「多了几片」不会被任何人当成故障。
    """

    counts = await _counts(drafts)

    assert counts == {"doc-1": 2, "doc-2": 1}, (
        "doc-1 的两片当前稿 + doc-2 的一片；旧稿的三片一片都不许数进来"
    )


@pytest.mark.asyncio
async def test_a_document_whose_current_draft_has_no_chunks_counts_zero(
    drafts: AsyncSession,
) -> None:
    """指针那一稿还没切片（在途窗口）：数 0，且整条不出现在字典里（仓储契约）。

    「不在字典里」与「0」在调用方那边等价（``counts.get(id, 0)``），但**不能**退化成
    「数上一稿的三片」——那正是切稿瞬间最容易出现的读数，而它说的是「这份文档现在有内容」。
    """

    assert "doc-3" not in await _counts(drafts), "没有当前稿切片的文档不在字典里，调用方回落 0"


@pytest.mark.asyncio
async def test_the_grouped_count_still_forces_the_tenant(drafts: AsyncSession) -> None:
    """租户过滤与当前稿条件同时生效：换个租户问同一批文档 id，一片都不给。

    ADR-0006：计数是强制带租户的查询。这里守的是「加了 JOIN 之后那条租户条件没被顺手
    丢掉」——JOIN 与 WHERE 都在同一条语句里，漏一处都不报错，只会数到别人的切片。
    """

    assert (
        await chunk_repository.count_by_document_ids(
            drafts,
            ["doc-1", "doc-2", "doc-3"],
            tenant_id=OTHER_TENANT,
        )
        == {}
    )


def test_the_statement_joins_the_document_row_and_compiles_the_draft_predicate() -> None:
    """结构的证据：当前稿指针在文档行上，不 JOIN 就没有「谁是当前稿」这个事实。

    行为断言（上面三条）已经能挡住写反的条件，这一条守的是**写法**——它让「把
    ``current_draft_filter()`` 换成别的条件」或「删掉 JOIN」在用例里立刻显形，而不是等到
    某次真库上多召回一稿。
    """

    sql = _sql(chunk_repository._count_by_document_ids_statement(["doc-1"], tenant_id=TENANT))
    assert "JOIN documents ON documents.id = chunks.document_id" in sql
    assert "chunks.doc_version = documents.current_version" in sql
    assert "chunks.generation = documents.current_index_generation" in sql, (
        "代那一条也要在 WHERE 里（两轴都要锁）"
    )
    assert "chunks.tenant_id = " in sql
    assert "chunks.index_version" not in sql, (
        "库级版本不再是可见性判据（现场九·第四步）：它一旦回到 WHERE 里，"
        "构建期间刚建好的那篇就会从计数里消失"
    )


def test_an_empty_document_list_does_not_send_a_statement() -> None:
    """空列表不发语句：这条老契约不能因为换语句实现而丢（``IN ()`` 是一次白跑）。"""

    class _NoStatementSession:
        async def execute(self, statement: Any) -> Any:  # pragma: no cover - 不许被调用
            raise AssertionError("空列表不该发语句")

    assert (
        asyncio.run(
            chunk_repository.count_by_document_ids(
                _NoStatementSession(),
                [],
                tenant_id=TENANT,  # type: ignore[arg-type]
            )
        )
        == {}
    )


@pytest.mark.asyncio
async def test_chunks_from_a_superseded_generation_are_not_counted(
    drafts: AsyncSession,
) -> None:
    """同一稿、**旧代**的切片不数：否则界面分块数 = 当前代 × 留存代数。

    现场就是这么爆的：一个建过 4 版的库，界面显示 152 片，而当前那一份只有 38 片——正好 4 倍。
    每次重建成功都会涨一份，且永不回落（旧代留着给回滚是对的，错的是把它们的切片算进
    「这份文档现在有多少内容」）。
    """

    counts = await _counts(drafts)

    assert counts == {"doc-1": 2, "doc-2": 1}, (
        "doc-1 当前稿 2 片（第 0 代那两片不算）、doc-2 一片（第 0 代那一片不算）"
    )


@pytest.mark.asyncio
async def test_a_document_without_any_generation_counts_zero(drafts: AsyncSession) -> None:
    """文档还没有任何一代产物（``current_index_generation`` 是 NULL）：不进计数。

    「还没有产物」与「第 1 代恰好零片」是两件事：前者这一篇根本不在服务集合里，后者是
    一次正常的空结果。写成同一个值就分不清了，而处置完全不同（一个要重建、一个只是空）。
    """

    await drafts.execute(
        text(
            f"INSERT INTO documents (id, tenant_id, kb_id, current_version, current_index_generation) "
            f"VALUES ('doc-nogen', '{TENANT}', '{KB}', 1, NULL)"
        )
    )
    await drafts.execute(
        text(
            f"INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, "
            f"index_version, doc_version) VALUES ('c-nogen', '{TENANT}', '{KB}', 'doc-nogen', "
            f"'有片但指针空', '[]', '{{}}', 'v1', 1)"
        )
    )

    counts = await chunk_repository.count_by_document_ids(drafts, ["doc-nogen"], tenant_id=TENANT)

    assert counts == {}, "指针是 NULL 的文档不该出现在计数里（它还没进服务集合）"
